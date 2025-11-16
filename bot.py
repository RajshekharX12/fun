#!/usr/bin/env python3
"""
Combined WireGuard + OpenVPN VPN helper bot.

NOTE:
- This bot does NOT run a VPN server.
- It only generates client config templates and (for WireGuard) a server-side peer snippet.
- You must configure real servers and replace placeholders with your real data.
"""

import json
import logging
from io import BytesIO
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ========================
# BASIC CONFIG
# ========================

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

DATA_FILE = Path("vpn_users.json")

# Protocol / country choices
PROTOCOL_WG = "wireguard"
PROTOCOL_OVPN = "openvpn"

# Supported countries
VPN_PROFILES = {
    "nl": {
        "name": "Netherlands",
        "wg_endpoint": "nl1.yourvpn.example.com:51820",
        "wg_server_public_key": "REPLACE_WITH_NL_WG_SERVER_PUB",
        "wg_subnet_prefix": "10.8.0.",  # last octet per-user
        "ovpn_remote": "nl1.yourvpn.example.com 1194",
    },
    "de": {
        "name": "Germany",
        "wg_endpoint": "de1.yourvpn.example.com:51820",
        "wg_server_public_key": "REPLACE_WITH_DE_WG_SERVER_PUB",
        "wg_subnet_prefix": "10.9.0.",
        "ovpn_remote": "de1.yourvpn.example.com 1194",
    },
    "us": {
        "name": "United States",
        "wg_endpoint": "us1.yourvpn.example.com:51820",
        "wg_server_public_key": "REPLACE_WITH_US_WG_SERVER_PUB",
        "wg_subnet_prefix": "10.10.0.",
        "ovpn_remote": "us1.yourvpn.example.com 1194",
    },
    "sg": {
        "name": "Singapore",
        "wg_endpoint": "sg1.yourvpn.example.com:51820",
        "wg_server_public_key": "REPLACE_WITH_SG_WG_SERVER_PUB",
        "wg_subnet_prefix": "10.11.0.",
        "ovpn_remote": "sg1.yourvpn.example.com 1194",
    },
}

DEFAULT_COUNTRY = "nl"
DEFAULT_PROTOCOL = PROTOCOL_WG

WG_DNS = "1.1.1.1"
WG_ALLOWED_IPS = "0.0.0.0/0, ::/0"

# ========================
# LOGGING
# ========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ========================
# PERSISTENCE
# ========================

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load data file: %s", e)
            return {}
    return {}


def save_data(data: dict) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to save data file: %s", e)


def get_user_record(data: dict, user_id: int) -> dict:
    uid = str(user_id)
    if uid not in data:
        data[uid] = {
            "profiles_created": 0,
            "lang": "en",
            "protocol": DEFAULT_PROTOCOL,
            "country": DEFAULT_COUNTRY,
        }
    return data[uid]


# ========================
# CONFIG GENERATION
# ========================

def get_user_ip_octet(user_id: int) -> int:
    # deterministic but bounded 10-230
    return (user_id % 221) + 10


def generate_wireguard_client_config(user_id: int, country_code: str, platform: str) -> str:
    profile = VPN_PROFILES[country_code]
    octet = get_user_ip_octet(user_id)
    client_ip = f"{profile['wg_subnet_prefix']}{octet}/32"

    text = []
    text.append("WireGuard client config template")
    text.append(f"Country: {profile['name']}")
    text.append(f"Platform: {platform}")
    text.append("")
    text.append("IMPORTANT:")
    text.append("- Replace all REPLACE_WITH_... fields with your real keys.")
    text.append("- Add the server-side [Peer] snippet on your VPN server.")
    text.append("")
    text.append("[Interface]")
    text.append("PrivateKey = REPLACE_WITH_CLIENT_PRIVATE_KEY")
    text.append(f"Address = {client_ip}")
    text.append(f"DNS = {WG_DNS}")
    text.append("")
    text.append("[Peer]")
    text.append(f"PublicKey = {profile['wg_server_public_key']}")
    text.append("PresharedKey = REPLACE_WITH_OPTIONAL_PRESHARED_KEY")
    text.append(f"AllowedIPs = {WG_ALLOWED_IPS}")
    text.append(f"Endpoint = {profile['wg_endpoint']}")
    text.append("PersistentKeepalive = 25")
    text.append("")
    text.append("Server-side snippet (add to wg0.conf):")
    text.append("[Peer]")
    text.append("PublicKey = REPLACE_WITH_CLIENT_PUBLIC_KEY")
    text.append(f"AllowedIPs = {client_ip}")
    text.append("# Restart your WireGuard interface after adding this peer.")
    return "\n".join(text)


def generate_openvpn_client_config(user_id: int, country_code: str, platform: str) -> str:
    profile = VPN_PROFILES[country_code]
    text = []
    text.append("OpenVPN client config template")
    text.append(f"Country: {profile['name']}")
    text.append(f"Platform: {platform}")
    text.append("")
    text.append("IMPORTANT:")
    text.append("- Replace all certificate/key blocks with your real values.")
    text.append("- Match cipher/auth with your server configuration.")
    text.append("")
    text.append("client")
    text.append("dev tun")
    text.append("proto udp")
    text.append(f"remote {profile['ovpn_remote']}")
    text.append("resolv-retry infinite")
    text.append("nobind")
    text.append("persist-key")
    text.append("persist-tun")
    text.append("remote-cert-tls server")
    text.append("cipher AES-256-CBC")
    text.append("auth SHA256")
    text.append("verb 3")
    text.append("")
    text.append("<ca>")
    text.append("# Paste your CA certificate here")
    text.append("</ca>")
    text.append("")
    text.append("<cert>")
    text.append("# Paste your client certificate here")
    text.append("</cert>")
    text.append("")
    text.append("<key>")
    text.append("# Paste your client private key here")
    text.append("</key>")
    text.append("")
    text.append("# Optional tls-auth key")
    text.append("<tls-auth>")
    text.append("# Paste your tls-auth key here")
    text.append("</tls-auth>")
    text.append("key-direction 1")
    return "\n".join(text)


def build_config_file_bytes(config_text: str, filename: str) -> BytesIO:
    bio = BytesIO(config_text.encode("utf-8"))
    bio.name = filename
    return bio


# ========================
# TEXT BUILDERS
# ========================

def get_country_label(code: str) -> str:
    profile = VPN_PROFILES.get(code)
    if not profile:
        return "Unknown"
    flag = {
        "nl": "🇳🇱",
        "de": "🇩🇪",
        "us": "🇺🇸",
        "sg": "🇸🇬",
    }.get(code, "🌍")
    return f"{flag} {profile['name']}"


def main_menu_text(user: dict) -> str:
    protocol = user.get("protocol", DEFAULT_PROTOCOL)
    country = user.get("country", DEFAULT_COUNTRY)
    proto_label = "WireGuard" if protocol == PROTOCOL_WG else "OpenVPN"
    return (
        "VPN Helper Bot\n"
        "--------------------\n"
        "This bot generates templates for VPN configs.\n"
        "You can import them into real VPN apps on Android or iOS.\n\n"
        f"Current protocol: {proto_label}\n"
        f"Current country: {get_country_label(country)}\n\n"
        "Use the buttons below to choose protocol/country and get configs.\n"
        "Remember to replace all placeholders with your real keys and certificates."
    )


def android_help_text() -> str:
    return (
        "Android setup:\n\n"
        "WireGuard:\n"
        "1. Install the official WireGuard app from Google Play.\n"
        "2. Import the .conf file generated by this bot.\n"
        "3. Make sure you filled in real keys and server details.\n"
        "4. Toggle the tunnel ON.\n\n"
        "OpenVPN:\n"
        "1. Install OpenVPN for Android or OpenVPN Connect.\n"
        "2. Import the .ovpn file from this bot.\n"
        "3. Add your certificates/keys where required.\n"
        "4. Connect."
    )


def ios_help_text() -> str:
    return (
        "iPhone / iOS setup:\n\n"
        "WireGuard:\n"
        "1. Install WireGuard from the App Store.\n"
        "2. Share the .conf file to WireGuard (Open in WireGuard).\n"
        "3. Allow VPN permission and enable the tunnel.\n\n"
        "OpenVPN:\n"
        "1. Install OpenVPN Connect.\n"
        "2. Send the .ovpn file to your iPhone.\n"
        "3. Open with OpenVPN and import.\n"
        "4. Provide certs/keys if needed, then connect."
    )


def faq_intro_text() -> str:
    return (
        "VPN FAQ:\n\n"
        "• This bot only generates config templates (WireGuard and OpenVPN).\n"
        "• You must run and configure your own VPN servers.\n"
        "• Always follow local laws and your provider's terms.\n"
        "• Use VPNs for privacy, security on public Wi-Fi, and neutral browsing."
    )


def faq_legal_text() -> str:
    return (
        "Legal and responsibility:\n\n"
        "• VPN usage is legal in many countries, restricted or banned in others.\n"
        "• You are fully responsible for how you use VPN configs.\n"
        "• Do not use VPN for crime or harm.\n"
        "• This bot is for educational and personal privacy use only."
    )


def faq_privacy_text() -> str:
    return (
        "Privacy and data:\n\n"
        "• This bot stores only minimal data: your Telegram ID, protocol/country\n"
        "  choice, language preference, and count of generated configs.\n"
        "• It does not see your traffic after you connect to VPN.\n"
        "• Real logging depends on your VPN server configuration, not this bot.\n"
        "• You can wipe your record with 'Delete my data'."
    )


def faq_speed_text() -> str:
    return (
        "Speed and latency:\n\n"
        "• Speed depends on distance to server, server hardware, and your ISP.\n"
        "• Netherlands, Germany, United States, Singapore usually have good connectivity.\n"
        "• Try different locations if one is slow.\n"
        "• Avoid overloading the same VPS with heavy tasks and VPN together."
    )


def faq_troubleshoot_text() -> str:
    return (
        "Troubleshooting:\n\n"
        "WireGuard:\n"
        "• If tunnel will not connect, check keys, endpoint, and firewall.\n"
        "• Make sure server has a [Peer] entry for your client.\n\n"
        "OpenVPN:\n"
        "• Check that cipher/auth in client matches server.\n"
        "• Ensure correct certificates and keys.\n"
        "• Use logs (verb 3 or higher) to see where it fails."
    )


# ========================
# KEYBOARDS
# ========================

def main_menu_keyboard(user: dict) -> InlineKeyboardMarkup:
    protocol = user.get("protocol", DEFAULT_PROTOCOL)
    country = user.get("country", DEFAULT_COUNTRY)
    proto_label = "WireGuard" if protocol == PROTOCOL_WG else "OpenVPN"
    lang = user.get("lang", "en")
    lang_label = "English" if lang == "en" else "Hindi"

    rows = [
        [
            InlineKeyboardButton("Get VPN Config", callback_data="get_config"),
        ],
        [
            InlineKeyboardButton(f"Protocol: {proto_label}", callback_data="choose_protocol"),
            InlineKeyboardButton(get_country_label(country), callback_data="choose_country"),
        ],
        [
            InlineKeyboardButton("Android help", callback_data="help_android"),
            InlineKeyboardButton("iPhone help", callback_data="help_ios"),
        ],
        [
            InlineKeyboardButton("FAQ", callback_data="menu_faq"),
            InlineKeyboardButton("My account", callback_data="menu_account"),
        ],
        [
            InlineKeyboardButton("Test IP", url="https://ipleak.net"),
            InlineKeyboardButton("DNS leak test", url="https://dnsleaktest.com"),
        ],
        [
            InlineKeyboardButton(f"Language: {lang_label}", callback_data="toggle_lang"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def protocol_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("WireGuard", callback_data="set_proto_wg"),
            InlineKeyboardButton("OpenVPN", callback_data="set_proto_ovpn"),
        ],
        [
            InlineKeyboardButton("Back", callback_data="menu_main"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def country_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for code in VPN_PROFILES.keys():
        rows.append(
            [
                InlineKeyboardButton(
                    get_country_label(code),
                    callback_data=f"set_country_{code}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def faq_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Overview", callback_data="faq_overview")],
        [
            InlineKeyboardButton("Legal", callback_data="faq_legal"),
            InlineKeyboardButton("Privacy", callback_data="faq_privacy"),
        ],
        [
            InlineKeyboardButton("Speed", callback_data="faq_speed"),
            InlineKeyboardButton("Troubleshooting", callback_data="faq_troubleshoot"),
        ],
        [InlineKeyboardButton("Back", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(rows)


def account_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Delete my data", callback_data="account_delete")],
        [InlineKeyboardButton("Back", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(rows)


# ========================#
# HANDLERS
# ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = get_user_record(data, update.effective_user.id)
    save_data(data)

    text = main_menu_text(user)
    keyboard = main_menu_keyboard(user)

    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = load_data()
    user = get_user_record(data, query.from_user.id)
    cd = query.data

    await query.answer()

    # Main menu
    if cd == "menu_main":
        save_data(data)
        await query.edit_message_text(
            main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
        )
        return

    # Protocol and country selection
    if cd == "choose_protocol":
        await query.edit_message_text(
            "Choose VPN protocol:",
            reply_markup=protocol_keyboard(),
        )
        return

    if cd == "choose_country":
        await query.edit_message_text(
            "Choose VPN country:",
            reply_markup=country_keyboard(),
        )
        return

    if cd == "set_proto_wg":
        user["protocol"] = PROTOCOL_WG
        save_data(data)
        await query.edit_message_text(
            "Protocol set to WireGuard.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
        )
        return

    if cd == "set_proto_ovpn":
        user["protocol"] = PROTOCOL_OVPN
        save_data(data)
        await query.edit_message_text(
            "Protocol set to OpenVPN.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
        )
        return

    if cd.startswith("set_country_"):
        code = cd.split("_", maxsplit=2)[2]
        if code in VPN_PROFILES:
            user["country"] = code
            save_data(data)
            await query.edit_message_text(
                f"Country set to {get_country_label(code)}.\n\n" + main_menu_text(user),
                reply_markup=main_menu_keyboard(user),
            )
        else:
            await query.edit_message_text(
                "Unknown country code.\n\n" + main_menu_text(user),
                reply_markup=main_menu_keyboard(user),
            )
        return

    # Get config flow
    if cd == "get_config":
        proto = user.get("protocol", DEFAULT_PROTOCOL)
        country = user.get("country", DEFAULT_COUNTRY)
        user["profiles_created"] += 1
        save_data(data)

        if proto == PROTOCOL_WG:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Android", callback_data="wg_android"),
                        InlineKeyboardButton("iOS", callback_data="wg_ios"),
                    ],
                    [InlineKeyboardButton("Back", callback_data="menu_main")],
                ]
            )
            await query.edit_message_text(
                f"WireGuard config for {get_country_label(country)}.\n"
                "Choose your platform:",
                reply_markup=keyboard,
            )
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Android", callback_data="ovpn_android"),
                        InlineKeyboardButton("iOS", callback_data="ovpn_ios"),
                        InlineKeyboardButton("Desktop", callback_data="ovpn_desktop"),
                    ],
                    [InlineKeyboardButton("Back", callback_data="menu_main")],
                ]
            )
            await query.edit_message_text(
                f"OpenVPN config for {get_country_label(country)}.\n"
                "Choose your platform:",
                reply_markup=keyboard,
            )
        return

    # WireGuard platform-specific
    if cd in ("wg_android", "wg_ios"):
        country = user.get("country", DEFAULT_COUNTRY)
        platform = "android" if cd == "wg_android" else "ios"
        cfg_text = generate_wireguard_client_config(query.from_user.id, country, platform)

        await query.message.reply_text(
            f"WireGuard config template ({get_country_label(country)} - {platform}):\n\n"
            f"{cfg_text}"
        )

        filename = f"{country}_wg_{platform}_{query.from_user.id}.conf"
        cfg_file = build_config_file_bytes(cfg_text, filename)
        await query.message.reply_document(
            document=cfg_file,
            filename=filename,
            caption="Import this file into WireGuard and replace placeholders with real keys.",
        )

        await query.edit_message_text(
            "WireGuard config sent above.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
        )
        save_data(data)
        return

    # OpenVPN platform-specific
    if cd in ("ovpn_android", "ovpn_ios", "ovpn_desktop"):
        country = user.get("country", DEFAULT_COUNTRY)
        if cd == "ovpn_android":
            platform = "android"
        elif cd == "ovpn_ios":
            platform = "ios"
        else:
            platform = "desktop"

        cfg_text = generate_openvpn_client_config(query.from_user.id, country, platform)

        await query.message.reply_text(
            f"OpenVPN config template ({get_country_label(country)} - {platform}):\n\n"
            f"{cfg_text}"
        )

        filename = f"{country}_ovpn_{platform}_{query.from_user.id}.ovpn"
        cfg_file = build_config_file_bytes(cfg_text, filename)
        await query.message.reply_document(
            document=cfg_file,
            filename=filename,
            caption="Import this file into OpenVPN and replace placeholders with real certs and keys.",
        )

        await query.edit_message_text(
            "OpenVPN config sent above.\n\n" + main_menu_text(user),
            r
