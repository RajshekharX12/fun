#!/usr/bin/env python3
"""
Combined WireGuard + OpenVPN VPN helper bot with emojis and extra features.

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

BOT_TOKEN = "7780014048:AAGuVnYTxEyfaJdHNp0-Mw29q8tKdb5B3uU"  # <-- put your token here

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
            "last_cfg_file": "",
            "last_cfg_filename": "",
        }
    else:
        # ensure keys exist for older records
        if "last_cfg_file" not in data[uid]:
            data[uid]["last_cfg_file"] = ""
        if "last_cfg_filename" not in data[uid]:
            data[uid]["last_cfg_filename"] = ""
    return data[uid]


# ========================
# CONFIG GENERATION
# ========================

def get_user_ip_octet(user_id: int) -> int:
    # deterministic but bounded 10-230
    return (user_id % 221) + 10


def generate_wireguard_client_and_server(user_id, country_code, platform):
    """
    Returns (client_config_clean, server_peer_snippet).

    client_config_clean is minimal, good for WireGuard import / QR scanner.
    """
    profile = VPN_PROFILES[country_code]
    octet = get_user_ip_octet(user_id)
    client_ip = f"{profile['wg_subnet_prefix']}{octet}/32"

    client_cfg = (
        f"[Interface]\n"
        f"PrivateKey = REPLACE_WITH_CLIENT_PRIVATE_KEY\n"
        f"Address = {client_ip}\n"
        f"DNS = {WG_DNS}\n"
        f"\n"
        f"[Peer]\n"
        f"PublicKey = {profile['wg_server_public_key']}\n"
        f"PresharedKey = REPLACE_WITH_OPTIONAL_PRESHARED_KEY\n"
        f"AllowedIPs = {WG_ALLOWED_IPS}\n"
        f"Endpoint = {profile['wg_endpoint']}\n"
        f"PersistentKeepalive = 25\n"
    )

    server_snippet = (
        "[Peer]\n"
        "PublicKey = REPLACE_WITH_CLIENT_PUBLIC_KEY\n"
        f"AllowedIPs = {client_ip}\n"
    )

    return client_cfg.strip(), server_snippet.strip()


def generate_openvpn_client_config(user_id: int, country_code: str, platform: str) -> str:
    profile = VPN_PROFILES[country_code]
    text = []
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
    proto_label = "WireGuard 🛡️" if protocol == PROTOCOL_WG else "OpenVPN 🔐"
    return (
        "🛡️ *VPN Helper Bot*\n"
        "────────────────────\n"
        "This bot generates *clean VPN config templates* you can import into "
        "real VPN apps on Android / iOS / Desktop.\n\n"
        f"• Current protocol: *{proto_label}*\n"
        f"• Current country: *{get_country_label(country)}*\n\n"
        "Use the buttons below to choose protocol/country and get configs.\n"
        "_Remember to replace placeholders with your real keys and certificates._"
    )


def android_help_text() -> str:
    return (
        "📱 *Android setup*\n\n"
        "🛡️ WireGuard:\n"
        "1. Install the official *WireGuard* app from Google Play.\n"
        "2. Tap `+` → *Import from file or archive*.\n"
        "3. Choose the `.conf` file from this bot.\n"
        "4. Edit the placeholders with your real keys & endpoint if needed.\n"
        "5. Toggle the tunnel *ON*.\n\n"
        "🔐 OpenVPN:\n"
        "1. Install *OpenVPN for Android* or *OpenVPN Connect*.\n"
        "2. Import the `.ovpn` file from this bot.\n"
        "3. Paste your CA / client cert / key where marked.\n"
        "4. Connect and test your IP."
    )


def ios_help_text() -> str:
    return (
        "🍎 *iPhone / iOS setup*\n\n"
        "🛡️ WireGuard:\n"
        "1. Install *WireGuard* from the App Store.\n"
        "2. Send the `.conf` file to your iPhone (Telegram, AirDrop, etc.).\n"
        "3. Tap *Open in WireGuard* and allow VPN permission.\n"
        "4. Enable the tunnel.\n\n"
        "🔐 OpenVPN:\n"
        "1. Install *OpenVPN Connect* from the App Store.\n"
        "2. Send the `.ovpn` file to your iPhone.\n"
        "3. Import it into OpenVPN and add your certs/keys.\n"
        "4. Connect and verify your new IP."
    )


def faq_intro_text() -> str:
    return (
        "❓ *VPN FAQ*\n\n"
        "• This bot only builds *config templates* (WireGuard & OpenVPN).\n"
        "• You must run and configure your *own* VPN servers.\n"
        "• Use VPNs for privacy, public Wi-Fi security, and neutral browsing.\n"
        "• Always follow *local laws* and your provider's ToS."
    )


def faq_legal_text() -> str:
    return (
        "⚖️ *Legal & responsibility*\n\n"
        "• VPN use is legal in many countries, restricted or banned in some.\n"
        "• *You* are responsible for how you use these configs.\n"
        "• Do **not** use VPN for abuse, crime, or anything harmful.\n"
        "• This bot is for educational and personal privacy use only."
    )


def faq_privacy_text() -> str:
    return (
        "🔐 *Privacy & data*\n\n"
        "• This bot stores minimal data in `vpn_users.json`:\n"
        "  – Your Telegram ID\n"
        "  – Protocol & country choice\n"
        "  – Language preference\n"
        "  – Count of generated configs\n"
        "  – Last config file text + filename (for easy re-download)\n"
        "• It *does not* see your traffic after you connect to the VPN.\n"
        "• Real logs depend on your own VPN server, not this bot.\n"
        "• Use *Delete my data* to wipe your record from this bot."
    )


def faq_speed_text() -> str:
    return (
        "🚀 *Speed & latency*\n\n"
        "• Speed depends on distance to server, server resources, and your ISP.\n"
        "• 🇳🇱 Netherlands, 🇩🇪 Germany, 🇺🇸 US, 🇸🇬 Singapore usually have good connectivity.\n"
        "• Try different locations if one is slow.\n"
        "• Avoid overloading the same VPS with heavy apps + VPN at the same time."
    )


def faq_troubleshoot_text() -> str:
    return (
        "🛠️ *Troubleshooting*\n\n"
        "🛡️ WireGuard:\n"
        "• If tunnel will not connect:\n"
        "  – Check keys on both client and server.\n"
        "  – Confirm server `Endpoint` and port.\n"
        "  – Ensure firewall allows UDP on your WireGuard port.\n"
        "• Make sure server has a matching `[Peer]` entry with your client public key.\n\n"
        "🔐 OpenVPN:\n"
        "• Verify cipher/auth match between client and server.\n"
        "• Ensure CA, client cert, and client key are correct.\n"
        "• Use higher `verb` log level temporarily to debug."
    )


# ========================
# KEYBOARDS
# ========================

def main_menu_keyboard(user: dict) -> InlineKeyboardMarkup:
    protocol = user.get("protocol", DEFAULT_PROTOCOL)
    country = user.get("country", DEFAULT_COUNTRY)
    proto_label = "WireGuard 🛡️" if protocol == PROTOCOL_WG else "OpenVPN 🔐"
    lang = user.get("lang", "en")
    lang_label = "English 🇬🇧" if lang == "en" else "Hindi 🇮🇳"

    rows = [
        [
            InlineKeyboardButton("🛡️ Get VPN Config", callback_data="get_config"),
        ],
        [
            InlineKeyboardButton(f"⚙️ Protocol: {proto_label}", callback_data="choose_protocol"),
            InlineKeyboardButton(f"🌍 {get_country_label(country)}", callback_data="choose_country"),
        ],
        [
            InlineKeyboardButton("📱 Android help", callback_data="help_android"),
            InlineKeyboardButton("🍎 iPhone help", callback_data="help_ios"),
        ],
        [
            InlineKeyboardButton("❓ FAQ", callback_data="menu_faq"),
            InlineKeyboardButton("👤 My account", callback_data="menu_account"),
        ],
        [
            InlineKeyboardButton("🌐 Test IP", url="https://ipleak.net"),
            InlineKeyboardButton("🧪 DNS leak test", url="https://dnsleaktest.com"),
        ],
        [
            InlineKeyboardButton(f"🌏 Language: {lang_label}", callback_data="toggle_lang"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def protocol_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("WireGuard 🛡️", callback_data="set_proto_wg"),
            InlineKeyboardButton("OpenVPN 🔐", callback_data="set_proto_ovpn"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="menu_main"),
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
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def faq_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📚 Overview", callback_data="faq_overview")],
        [
            InlineKeyboardButton("⚖️ Legal", callback_data="faq_legal"),
            InlineKeyboardButton("🔐 Privacy", callback_data="faq_privacy"),
        ],
        [
            InlineKeyboardButton("🚀 Speed", callback_data="faq_speed"),
            InlineKeyboardButton("🛠️ Troubleshooting", callback_data="faq_troubleshoot"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(rows)


def account_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📥 Last config", callback_data="account_last_cfg"),
        ],
        [
            InlineKeyboardButton("🗑️ Delete my data", callback_data="account_delete"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="menu_main"),
        ],
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
        await update.message.reply_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )


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
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    # Protocol and country selection
    if cd == "choose_protocol":
        await query.edit_message_text(
            "⚙️ *Choose VPN protocol:*",
            reply_markup=protocol_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "choose_country":
        await query.edit_message_text(
            "🌍 *Choose VPN country:*",
            reply_markup=country_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "set_proto_wg":
        user["protocol"] = PROTOCOL_WG
        save_data(data)
        await query.edit_message_text(
            "✅ Protocol set to *WireGuard*.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "set_proto_ovpn":
        user["protocol"] = PROTOCOL_OVPN
        save_data(data)
        await query.edit_message_text(
            "✅ Protocol set to *OpenVPN*.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd.startswith("set_country_"):
        code = cd.split("_", maxsplit=2)[2]
        if code in VPN_PROFILES:
            user["country"] = code
            save_data(data)
            await query.edit_message_text(
                f"✅ Country set to *{get_country_label(code)}*.\n\n" + main_menu_text(user),
                reply_markup=main_menu_keyboard(user),
                disable_web_page_preview=True,
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "⚠️ Unknown country code.\n\n" + main_menu_text(user),
                reply_markup=main_menu_keyboard(user),
                disable_web_page_preview=True,
                parse_mode="Markdown",
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
                        InlineKeyboardButton("📱 Android", callback_data="wg_android"),
                        InlineKeyboardButton("🍎 iOS", callback_data="wg_ios"),
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
                ]
            )
            await query.edit_message_text(
                f"🛡️ *WireGuard config* for {get_country_label(country)}.\n"
                "Choose your platform:",
                reply_markup=keyboard,
                disable_web_page_preview=True,
                parse_mode="Markdown",
            )
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("📱 Android", callback_data="ovpn_android"),
                        InlineKeyboardButton("🍎 iOS", callback_data="ovpn_ios"),
                        InlineKeyboardButton("💻 Desktop", callback_data="ovpn_desktop"),
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
                ]
            )
            await query.edit_message_text(
                f"🔐 *OpenVPN config* for {get_country_label(country)}.\n"
                "Choose your platform:",
                reply_markup=keyboard,
                disable_web_page_preview=True,
                parse_mode="Markdown",
            )
        return

    # WireGuard platform-specific
    if cd in ("wg_android", "wg_ios"):
        country = user.get("country", DEFAULT_COUNTRY)
        platform = "android" if cd == "wg_android" else "ios"
        client_cfg, server_snippet = generate_wireguard_client_and_server(
            query.from_user.id, country, platform
        )

    # Text message with explanation + both client & server snippet
        msg_text = (
            f"🛡️ *WireGuard config* ({get_country_label(country)} – {platform})\n\n"
            "📱 *Client config (import / QR text)*:\n"
            "```ini\n"
            f"{client_cfg}\n"
            "```\n\n"
            "🖥️ *Server-side snippet* (add to your `wg0.conf`):\n"
            "```ini\n"
            f"{server_snippet}\n"
            "```\n\n"
            "_Import the `.conf` file into WireGuard, then replace_ "
            "`REPLACE_WITH_...` _with real keys & endpoint if needed._"
        )

        await query.message.reply_text(
            msg_text,
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )

        # Clean client config as file (good for WireGuard app / scanner)
        filename = f"{country}_wg_{platform}_{query.from_user.id}.conf"
        cfg_file = build_config_file_bytes(client_cfg, filename)
        await query.message.reply_document(
            document=cfg_file,
            filename=filename,
            caption="🛡️ Clean WireGuard client config (.conf) – import this into the WireGuard app.",
        )

        # store last config for quick re-download
        user["last_cfg_file"] = client_cfg
        user["last_cfg_filename"] = filename
        save_data(data)

        await query.edit_message_text(
            "✅ WireGuard config sent above.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
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

        cfg_text = generate_openvpn_client_config(
            query.from_user.id, country, platform
        )

        msg_text = (
            f"🔐 *OpenVPN config* ({get_country_label(country)} – {platform})\n\n"
            "Paste your real CA / client certificate / client key where marked.\n"
            "Then import into OpenVPN and connect.\n\n"
            "```conf\n"
            f"{cfg_text}\n"
            "```"
        )

        await query.message.reply_text(
            msg_text,
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )

        filename = f"{country}_ovpn_{platform}_{query.from_user.id}.ovpn"
        cfg_file = build_config_file_bytes(cfg_text, filename)
        await query.message.reply_document(
            document=cfg_file,
            filename=filename,
            caption="🔐 OpenVPN client config (.ovpn) – fill in your real certs/keys.",
        )

        user["last_cfg_file"] = cfg_text
        user["last_cfg_filename"] = filename
        save_data(data)

        await query.edit_message_text(
            "✅ OpenVPN config sent above.\n\n" + main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    # Help menus
    if cd == "help_android":
        await query.edit_message_text(
            android_help_text(),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]]
            ),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "help_ios":
        await query.edit_message_text(
            ios_help_text(),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]]
            ),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    # FAQ
    if cd == "menu_faq":
        await query.edit_message_text(
            faq_intro_text(),
            reply_markup=faq_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "faq_overview":
        await query.edit_message_text(
            faq_intro_text(),
            reply_markup=faq_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "faq_legal":
        await query.edit_message_text(
            faq_legal_text(),
            reply_markup=faq_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "faq_privacy":
        await query.edit_message_text(
            faq_privacy_text(),
            reply_markup=faq_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "faq_speed":
        await query.edit_message_text(
            faq_speed_text(),
            reply_markup=faq_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    if cd == "faq_troubleshoot":
        await query.edit_message_text(
            faq_troubleshoot_text(),
            reply_markup=faq_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return

    # Account
    if cd == "menu_account":
        text = (
            "👤 *Account info*\n\n"
            f"• Configs generated: `{user.get('profiles_created', 0)}`\n"
            f"• Protocol: `{user.get('protocol', DEFAULT_PROTOCOL)}`\n"
            f"• Country: `{get_country_label(user.get('country', DEFAULT_COUNTRY))}`\n"
            f"• Last config file: `{user.get('last_cfg_filename') or 'none'}`\n\n"
            "Use the buttons below to download your last config or delete your data."
        )
        await query.edit_message_text(
            text,
            reply_markup=account_keyboard(),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        save_data(data)
        return

    if cd == "account_last_cfg":
        if not user.get("last_cfg_file"):
            await query.answer(
                "No config stored yet. Generate one via “Get VPN Config”.",
                show_alert=True,
            )
            return

        filename = user.get("last_cfg_filename") or "vpn_last.conf"
        cfg_file = build_config_file_bytes(user["last_cfg_file"], filename)
        await query.message.reply_document(
            document=cfg_file,
            filename=filename,
            caption="📥 Your last generated config file.",
        )
        await query.answer("Last config sent.", show_alert=False)
        return

    if cd == "account_delete":
        uid = str(query.from_user.id)
        if uid in data:
            del data[uid]
            save_data(data)
        await query.edit_message_text(
            "🗑️ Your bot data has been deleted.\n\nYou can use /start again anytime."
        )
        return

    # Language toggle (text currently only in English, but flag changes)
    if cd == "toggle_lang":
        current = user.get("lang", "en")
        user["lang"] = "hi" if current == "en" else "en"
        save_data(data)
        await query.edit_message_text(
            main_menu_text(user),
            reply_markup=main_menu_keyboard(user),
            disable_web_page_preview=True,
            parse_mode="Markdown",
        )
        return


# ========================
# MAIN
# ========================

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(handle_callback))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
