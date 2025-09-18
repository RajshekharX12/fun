import asyncio
import csv
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict
from html import escape as hesc

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dateutil import parser as dtparser
from dotenv import load_dotenv

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID_STR = os.getenv("OWNER_ID", "").strip()
try:
    OWNER_ID = int(OWNER_ID_STR)
except Exception:
    OWNER_ID = 0

TZ_LABEL = os.getenv("TZ", "Asia/Kolkata")
TRANSLATE_ENABLED = os.getenv("TRANSLATE_ENABLED", "false").lower() == "true"
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "en").strip().lower()
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
WHITELIST_MODE = os.getenv("WHITELIST_MODE", "false").lower() == "true"
AWAY_TEXT = os.getenv("AWAY_TEXT", "").strip()

if not BOT_TOKEN or not OWNER_ID:
    raise SystemExit("Please set BOT_TOKEN and OWNER_ID in .env")

# Optional translator (graceful fallback)
try:
    from deep_translator import GoogleTranslator  # type: ignore

    _translator_ok = True
except Exception:
    _translator_ok = False
    TRANSLATE_ENABLED = False

# ---------- TIMEZONE ----------
IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata fixed offset

# ---------- DB ----------
DB_PATH = "inbox.db"

PAGE_SIZE = 8  # users per inbox/contacts page
CHAT_PAGE_SIZE = 12  # messages per chat screen


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_whitelisted INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                tags TEXT DEFAULT '',
                note TEXT DEFAULT '',
                favorite INTEGER DEFAULT 0,
                last_seen TIMESTAMP,
                last_auto_reply_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                direction TEXT CHECK(direction IN ('in','out')) NOT NULL,
                content_type TEXT,
                text TEXT,
                file_id TEXT,
                date TIMESTAMP NOT NULL,
                admin_msg_id INTEGER,
                is_read INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS quick_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                response TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                send_at TIMESTAMP NOT NULL,
                sent INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            """
        )
        # --- lightweight migration: add 'archived' column if missing ---
        async with db.execute("PRAGMA table_info('users')") as cur:
            cols = [r[1] async for r in cur]
        if "archived" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN archived INTEGER DEFAULT 0")
        await db.commit()


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


# ---------- HELPERS ----------
def is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_ID


async def ensure_user(row_user) -> None:
    if not row_user:
        return
    uid = row_user.id
    uname = row_user.username or ""
    full = (row_user.full_name or "").strip()
    now_utc = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(user_id, username, full_name, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen
            """,
            (uid, uname, full, now_utc),
        )
        await db.commit()


async def get_user_flags(user_id: int) -> Tuple[bool, bool, bool, bool]:
    """returns (is_blocked, is_whitelisted, is_favorite, is_archived)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_blocked, is_whitelisted, favorite, archived FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return (False, False, False, False)
            return (bool(row[0]), bool(row[1]), bool(row[2]), bool(row[3]))


async def count_last_min_msgs(user_id: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id=? AND direction='in' AND date>=?",
            (user_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def save_message(
    user_id: int,
    direction: str,
    content_type: str,
    text: Optional[str],
    file_id: Optional[str],
    admin_msg_id: Optional[int],
    is_read: int,
):
    now_utc = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages(user_id, direction, content_type, text, file_id, date, admin_msg_id, is_read)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, direction, content_type, text, file_id, now_utc, admin_msg_id, is_read),
        )
        await db.commit()


async def admin_msg_map_to_user(admin_msg_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM messages WHERE admin_msg_id=? LIMIT 1", (admin_msg_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def mark_read_by_admin_msg(admin_msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET is_read=1 WHERE admin_msg_id=?", (admin_msg_id,)
        )
        await db.commit()


def fmt_user_link(user_id: int, username: Optional[str], full_name: str) -> str:
    link = f"<a href='tg://user?id={user_id}'>link</a>"
    u = f"@{username}" if username else "—"
    full = hesc(full_name or "—")
    return f"👤 <b>{full}</b> ({u}) • ID: <code>{user_id}</code> • {link}"


async def translate_if_enabled(text: str) -> str:
    if TRANSLATE_ENABLED and _translator_ok and text:
        try:
            return GoogleTranslator(source="auto", target=TRANSLATE_TO).translate(text)
        except Exception:
            return text
    return text


def kb_admin_for(user_id: int, admin_msg_id: int, blocked: bool, favorite: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💬 Reply", callback_data=f"act|reply|{admin_msg_id}")
    b.button(text="⚡ Quick Replies", callback_data=f"act|qr|{admin_msg_id}")
    b.button(text="ℹ️ Info", callback_data=f"act|info|{admin_msg_id}")
    b.button(text="📝 Note", callback_data=f"act|note|{user_id}|{admin_msg_id}")
    b.button(text=("⭐ Unfav" if favorite else "⭐ Fav"), callback_data=f"act|fav|{user_id}|{admin_msg_id}")
    b.button(text=("✅ Unblock" if blocked else "🚫 Block"), callback_data=f"act|block|{user_id}|{admin_msg_id}")
    b.button(text="✅ Mark read", callback_data=f"act|read|{admin_msg_id}")
    b.adjust(2, 2, 3)
    return b.as_markup()


def dt_ist(dt_utc: datetime) -> str:
    try:
        return dt_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_utc)


# ---------- STATES ----------
class ReplyState(StatesGroup):
    awaiting = State()  # waiting for owner to send a message to forward


class NoteState(StatesGroup):
    typing = State()


class TagState(StatesGroup):
    typing = State()


# ---------- ROUTERS ----------
admin = Router(name="admin")
public = Router(name="public")
cb = Router(name="callbacks")


# ---------- INBOX / CONTACTS HELPERS ----------
async def fetch_inbox_users(page: int, sort: int, archived: int) -> Tuple[List[Dict], int]:
    """
    sort: 0=last, 1=unread, 2=fav
    archived: 0 or 1
    returns (rows, total_count)
    """
    order_clause = "u.favorite DESC, last_date DESC"
    if sort == 1:
        order_clause = "unread DESC, u.favorite DESC, last_date DESC"
    elif sort == 2:
        order_clause = "u.favorite DESC, last_date DESC"

    offset = page * PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        # Count users that have any message
        async with db.execute(
            """
            SELECT COUNT(*) FROM users u
            WHERE u.archived=? AND EXISTS (SELECT 1 FROM messages m WHERE m.user_id=u.user_id)
            """,
            (archived,),
        ) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(
            f"""
            SELECT
              u.user_id, u.username, u.full_name, u.tags, u.favorite, u.is_blocked,
              (SELECT COUNT(*) FROM messages mi WHERE mi.user_id=u.user_id AND mi.direction='in' AND mi.is_read=0) AS unread,
              (SELECT text FROM messages ml WHERE ml.user_id=u.user_id ORDER BY ml.date DESC LIMIT 1) AS last_text,
              (SELECT content_type FROM messages ml WHERE ml.user_id=u.user_id ORDER BY ml.date DESC LIMIT 1) AS last_type,
              (SELECT date FROM messages ml WHERE ml.user_id=u.user_id ORDER BY ml.date DESC LIMIT 1) AS last_date
            FROM users u
            WHERE u.archived=?
              AND EXISTS (SELECT 1 FROM messages mx WHERE mx.user_id=u.user_id)
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            (archived, PAGE_SIZE, offset),
        ) as cur:
            rows = []
            async for r in cur:
                rows.append(
                    dict(
                        user_id=r[0],
                        username=r[1],
                        full_name=r[2] or "",
                        tags=r[3] or "",
                        favorite=bool(r[4]),
                        blocked=bool(r[5]),
                        unread=int(r[6] or 0),
                        last_text=r[7] or "",
                        last_type=r[8] or "",
                        last_date=r[9],
                    )
                )
    return rows, total


def build_inbox_text(rows: List[Dict], page: int, total: int, sort: int, archived: int) -> str:
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    hdr = (
        f"<b>Inbox{' (archived)' if archived else ''}</b> • Page {page+1}/{total_pages} • Sort: "
        f"{['Last','Unread','Fav'][sort]}\n"
    )
    if not rows:
        return hdr + "\nNo conversations yet."
    lines = [hdr, ""]
    for r in rows:
        name = hesc(r["full_name"] or "—")
        uname = f"@{r['username']}" if r["username"] else "—"
        tag = f"🏷️ {hesc(r['tags'])}" if r["tags"] else ""
        fav = "⭐" if r["favorite"] else ""
        blk = "🚫" if r["blocked"] else ""
        unread = f"• Unread: <b>{r['unread']}</b>" if r["unread"] else ""
        last = r["last_text"] or f"[{r['last_type']}]"
        last = hesc((last or "")[:120])
        dt = dt_ist(datetime.fromisoformat(r["last_date"])) if r["last_date"] else "—"
        lines.append(
            f"{fav}{blk} <b>{name}</b> ({uname}) • ID <code>{r['user_id']}</code>\n"
            f"  {tag}\n"
            f"  Last: {last} • {dt} {unread}"
        )
    return "\n".join(lines)


def kb_inbox(rows: List[Dict], page: int, total: int, sort: int, archived: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    b = InlineKeyboardBuilder()
    # one "Open" button per row
    for r in rows:
        label = f"📂 Open: {r['full_name'][:20] or r['username'] or r['user_id']}"
        b.button(text=label, callback_data=f"chat|{r['user_id']}|0|{sort}|{page}|{archived}")
    # row: sort + mark all read + prev/next
    b.button(text="🔁 Sort", callback_data=f"inb|sort|{sort}|{page}|{archived}")
    b.button(text="✅ Mark all read", callback_data=f"inb|markall|{sort}|{page}|{archived}")
    if page > 0:
        b.button(text="⏮ Prev", callback_data=f"inb|page|{page-1}|{sort}|{archived}")
    if page < total_pages - 1:
        b.button(text="⏭ Next", callback_data=f"inb|page|{page+1}|{sort}|{archived}")
    b.adjust(1, 2, 2)
    return b.as_markup()


async def fetch_chat_messages(uid: int, offset: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT direction, content_type, text, date
            FROM messages
            WHERE user_id=?
            ORDER BY date DESC
            LIMIT ? OFFSET ?
            """,
            (uid, CHAT_PAGE_SIZE, offset),
        ) as cur:
            rows = []
            async for d, ct, txt, dtv in cur:
                rows.append(dict(direction=d, content_type=ct or "", text=txt or "", date=dtv))
    return rows


def build_chat_text(uid: int, username: str, full_name: str, msgs: List[Dict], offset: int) -> str:
    head = f"<b>Chat with</b> {hesc(full_name or '—')} (@{username or '—'}) • ID <code>{uid}</code>\n"
    if not msgs:
        return head + "\nNo messages yet."
    lines = [head, ""]
    for m in msgs:
        tag = "⬅️ IN" if m["direction"] == "in" else "➡️ OUT"
        body = m["text"] or f"[{m['content_type']}]"
        body = hesc(body[:300])
        dt = dt_ist(datetime.fromisoformat(m["date"]))
        lines.append(f"{tag} • {dt}\n{body}")
    tail = f"\nPage offset: {offset}"
    lines.append(tail)
    return "\n".join(lines)


def kb_chat(
    uid: int, sort: int, page: int, offset: int, is_fav: bool, is_blocked: bool, archived: int
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # nav
    prev_off = max(0, offset + CHAT_PAGE_SIZE)
    next_off = max(0, offset - CHAT_PAGE_SIZE)
    b.button(text="⏮ Older", callback_data=f"chat|{uid}|{prev_off}|{sort}|{page}|{archived}")
    if offset > 0:
        b.button(text="⏭ Newer", callback_data=f"chat|{uid}|{next_off}|{sort}|{page}|{archived}")
    # actions
    b.button(text="💬 Reply", callback_data=f"chatreply|{uid}|{sort}|{page}|{offset}|{archived}")
    b.button(text="✅ Mark all read", callback_data=f"chatmr|{uid}|{sort}|{page}|{offset}|{archived}")
    b.button(text=("⭐ Unfav" if is_fav else "⭐ Fav"), callback_data=f"chatfav|{uid}|{sort}|{page}|{offset}|{archived}")
    b.button(
        text=("✅ Unblock" if is_blocked else "🚫 Block"),
        callback_data=f"chatblk|{uid}|{sort}|{page}|{offset}|{archived}",
    )
    b.button(
        text=("📦 Unarchive" if archived else "📦 Archive"),
        callback_data=f"chatarc|{uid}|{sort}|{page}|{offset}|{archived}",
    )
    b.button(text="📝 Note", callback_data=f"chatnote|{uid}|{sort}|{page}|{offset}|{archived}")
    b.button(text="🏷️ Tag", callback_data=f"chattag|{uid}|{sort}|{page}|{offset}|{archived}")
    # back
    b.button(text="⬅️ Back to Inbox", callback_data=f"inb|page|{page}|{sort}|{archived}")
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()


# ---------- ADMIN COMMANDS ----------
@admin.message(CommandStart())
async def owner_start(message: Message):
    if not is_owner(message):
        return
    await message.answer(
        "👋 <b>Personal Inbox Bot</b> is up.\n\n"
        "• Full Inbox: /inbox • Archived: /inbox_archived • Sort: /inbox_sort\n"
        "• Open chat: /open &lt;user_id&gt; • Contacts: /contacts [all|whitelist|blocked|favorites]\n"
        "• Mark all read: /mark_all_read\n"
        "Type /help for full commands.",
        parse_mode=ParseMode.HTML,
    )


@admin.message(Command("help"))
async def owner_help(message: Message):
    if not is_owner(message):
        return
    await message.answer(
        "<b>Commands</b>\n"
        "• /inbox – list conversations (paginated)\n"
        "• /inbox_archived – list archived conversations\n"
        "• /inbox_sort <last|unread|fav>\n"
        "• /open <user_id> – open chat view\n"
        "• /contacts [all|whitelist|blocked|favorites]\n"
        "• /archive <user_id>, /unarchive <user_id>, /archive_list\n"
        "• /mark_all_read – mark all inbox as read\n"
        "• /stats, /settings, /silent <minutes|off>, /away <text|off>\n"
        "• /whitelist_on, /whitelist_off, /wl_add <user_id>, /wl_del <user_id>\n"
        "• /block <user_id>, /unblock <user_id>, /fav <user_id>, /unfav <user_id>\n"
        "• /note_set <user_id> <text>, /note_get <user_id>\n"
        "• /tag_set <user_id> <tag1,tag2>, /tag_get <user_id>\n"
        "• /qr_add \"Title\" = \"Text\", /qr_list\n"
        "• /trigger_add key = response, /trigger_list\n"
        "• /search <text>, /unread, /export_csv\n"
        "• /schedule_reply <user_id> <in 10m|YYYY-MM-DD HH:MM> | <text>\n"
        "• Reply by: button <b>Reply</b> or reply to forwarded message.",
        parse_mode=ParseMode.HTML,
    )


@admin.message(Command("settings"))
async def owner_settings(message: Message):
    if not is_owner(message):
        return
    whitelist_mode = await get_setting("whitelist_mode", "1" if WHITELIST_MODE else "0")
    silent_until = await get_setting("silent_until", "")
    t_enabled = "on ✅" if TRANSLATE_ENABLED else "off"
    away = (await get_setting("away_text", AWAY_TEXT)) or "—"
    rl = await get_setting("rate_limit_per_min", str(RATE_LIMIT_PER_MIN))
    inbox_sort = await get_setting("inbox_sort", "last")
    await message.answer(
        f"<b>Settings</b>\n"
        f"• Whitelist mode: {'ON' if whitelist_mode=='1' else 'OFF'}\n"
        f"• Silent until: {silent_until or '—'}\n"
        f"• Translate: {t_enabled} → {TRANSLATE_TO}\n"
        f"• Away text: {away}\n"
        f"• Rate limit: {rl}/min\n"
        f"• Inbox sort: {inbox_sort}",
        parse_mode=ParseMode.HTML,
    )


@admin.message(Command("stats"))
async def owner_stats(message: Message):
    if not is_owner(message):
        return
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_utc = today_utc - timedelta(days=7)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND date>=?", (today_utc,)
        ) as cur:
            today = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND date>=?", (week_utc,)
        ) as cur:
            week = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT user_id, COUNT(*) AS c FROM messages WHERE direction='in' AND date>=? GROUP BY user_id ORDER BY c DESC LIMIT 5",
            (week_utc,),
        ) as cur:
            rows = await cur.fetchall()
    top = "\n".join([f"• {uid}: {c}" for uid, c in rows]) or "—"
    await message.answer(
        f"<b>Inbox</b>\nToday: <b>{today}</b>\nLast 7 days: <b>{week}</b>\nTop chatters:\n{top}",
        parse_mode=ParseMode.HTML,
    )


@admin.message(Command("silent"))
async def owner_silent(message: Message):
    if not is_owner(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) == 1:
        until = await get_setting("silent_until", "")
        return await message.answer(f"Silent until: {until or 'OFF'}")
    v = arg[1].strip().lower()
    if v == "off":
        await set_setting("silent_until", "")
        return await message.answer("🔔 Silent mode OFF")
    try:
        minutes = int(v.replace("m", ""))
        until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        await set_setting("silent_until", until)
        await message.answer(f"🔕 Silent for {minutes} min")
    except Exception:
        await message.answer("Usage: /silent <minutes|off>")


@admin.message(Command("away"))
async def owner_away(message: Message):
    if not is_owner(message):
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) == 1:
        cur = await get_setting("away_text", AWAY_TEXT or "")
        return await message.answer(f"Away text: {cur or 'OFF'}")
    v = arg[1].strip()
    if v.lower() == "off":
        await set_setting("away_text", "")
        return await message.answer("Away OFF")
    await set_setting("away_text", v)
    await message.answer("Away text updated.")


# ---------- NEW: INBOX / CONTACTS / ARCHIVE ----------
@admin.message(Command("inbox"))
async def cmd_inbox(message: Message):
    if not is_owner(message):
        return
    # default sort from settings
    sort_map = {"last": 0, "unread": 1, "fav": 2}
    sort_key = await get_setting("inbox_sort", "last")
    sort = sort_map.get(sort_key, 0)
    page = 0
    rows, total = await fetch_inbox_users(page, sort, archived=0)
    txt = build_inbox_text(rows, page, total, sort, archived=0)
    await message.answer(
        txt,
        reply_markup=kb_inbox(rows, page, total, sort, archived=0),
        parse_mode=ParseMode.HTML,
    )


@admin.message(Command("inbox_archived"))
async def cmd_inbox_arch(message: Message):
    if not is_owner(message):
        return
    sort_map = {"last": 0, "unread": 1, "fav": 2}
    sort_key = await get_setting("inbox_sort", "last")
    sort = sort_map.get(sort_key, 0)
    page = 0
    rows, total = await fetch_inbox_users(page, sort, archived=1)
    txt = build_inbox_text(rows, page, total, sort, archived=1)
    await message.answer(
        txt,
        reply_markup=kb_inbox(rows, page, total, sort, archived=1),
        parse_mode=ParseMode.HTML,
    )


@admin.message(Command("inbox_sort"))
async def cmd_inbox_sort(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /inbox_sort <last|unread|fav>")
    v = parts[1].lower()
    if v not in {"last", "unread", "fav"}:
        return await message.answer("Choose one: last | unread | fav")
    await set_setting("inbox_sort", v)
    await message.answer(f"Sort set to: {v}. Use /inbox again.")


@admin.message(Command("open"))
async def cmd_open(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /open <user_id>")
    uid = int(parts[1])
    # default nav context
    sort_map = {"last": 0, "unread": 1, "fav": 2}
    sort_key = await get_setting("inbox_sort", "last")
    sort = sort_map.get(sort_key, 0)
    page = 0
    offset = 0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, full_name, favorite, is_blocked, archived FROM users WHERE user_id=?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return await message.answer("User not found.")
    username, full_name, fav, blk, arch = row
    msgs = await fetch_chat_messages(uid, offset)
    txt = build_chat_text(uid, username or "", full_name or "", msgs, offset)
    kb = kb_chat(uid, sort, page, offset, bool(fav), bool(blk), int(arch))
    await message.answer(txt, reply_markup=kb, parse_mode=ParseMode.HTML)


@admin.message(Command("contacts"))
async def cmd_contacts(message: Message):
    if not is_owner(message):
        return
    # filter: all|whitelist|blocked|favorites
    parts = (message.text or "").split()
    flt = parts[1].lower() if len(parts) > 1 else "all"
    where = "1=1"
    if flt == "whitelist":
        where = "is_whitelisted=1"
    elif flt == "blocked":
        where = "is_blocked=1"
    elif flt == "favorites":
        where = "favorite=1"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT user_id,username,full_name,favorite,is_blocked FROM users WHERE {where} ORDER BY favorite DESC, last_seen DESC LIMIT 50"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer("No contacts found for that filter.")
    lines = ["<b>Contacts</b> (" + hesc(flt) + ")\n"]
    b = InlineKeyboardBuilder()
    for uid, uname, full, fav, blk in rows:
        lines.append(
            f"{'⭐' if fav else ''}{'🚫' if blk else ''} <b>{hesc(full or '—')}</b> (@{uname or '—'}) • <code>{uid}</code>"
        )
        b.button(text=f"📂 Open {full[:18] or uname or uid}", callback_data=f"chat|{uid}|0|0|0|0")
    b.adjust(1)
    await message.answer("\n".join(lines), reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)


@admin.message(Command("archive"))
async def cmd_archive(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /archive <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET archived=1 WHERE user_id=?", (uid,))
        await db.commit()
    await message.answer(f"📦 Archived {uid}")


@admin.message(Command("unarchive"))
async def cmd_unarchive(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /unarchive <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET archived=0 WHERE user_id=?", (uid,))
        await db.commit()
    await message.answer(f"📦 Unarchived {uid}")


@admin.message(Command("archive_list"))
async def cmd_archive_list(message: Message):
    if not is_owner(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id,username,full_name FROM users WHERE archived=1 ORDER BY last_seen DESC LIMIT 50"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer("No archived chats.")
    lines = ["<b>Archived Chats</b>\n"]
    b = InlineKeyboardBuilder()
    for uid, uname, full in rows:
        lines.append(f"• {hesc(full or '—')} (@{uname or '—'}) • <code>{uid}</code>")
        b.button(text=f"📂 Open {full[:18] or uname or uid}", callback_data=f"chat|{uid}|0|0|0|1")
    b.adjust(1)
    await message.answer("\n".join(lines), reply_markup=b.as_markup(), parse_mode=ParseMode.HTML)


@admin.message(Command("mark_all_read"))
async def cmd_mark_all_read(message: Message):
    if not is_owner(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE messages SET is_read=1 WHERE direction='in' AND is_read=0")
        await db.commit()
    await message.answer("✅ All incoming messages marked as read.")


# ---------- EXISTING ADMIN COMMANDS (whitelist/block/fav/notes/tags/qr/triggers/search/export/schedule) ----------
@admin.message(Command("whitelist_on"))
async def wl_on(message: Message):
    if not is_owner(message):
        return
    await set_setting("whitelist_mode", "1")
    await message.answer("✅ Whitelist mode ON")


@admin.message(Command("whitelist_off"))
async def wl_off(message: Message):
    if not is_owner(message):
        return
    await set_setting("whitelist_mode", "0")
    await message.answer("❌ Whitelist mode OFF")


@admin.message(Command("wl_add"))
async def wl_add(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /wl_add <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,is_whitelisted) VALUES(?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET is_whitelisted=1",
            (uid,),
        )
        await db.commit()
    await message.answer(f"✅ Whitelisted {uid}")


@admin.message(Command("wl_del"))
async def wl_del(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /wl_del <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,is_whitelisted) VALUES(?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET is_whitelisted=0",
            (uid,),
        )
        await db.commit()
    await message.answer(f"Removed from whitelist: {uid}")


@admin.message(Command("block"))
async def cmd_block(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /block <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,is_blocked) VALUES(?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET is_blocked=1",
            (uid,),
        )
        await db.commit()
    await message.answer(f"🚫 Blocked {uid}")


@admin.message(Command("unblock"))
async def cmd_unblock(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /unblock <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,is_blocked) VALUES(?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET is_blocked=0",
            (uid,),
        )
        await db.commit()
    await message.answer(f"Unblocked {uid}")


@admin.message(Command("fav"))
async def cmd_fav(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /fav <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,favorite) VALUES(?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET favorite=1",
            (uid,),
        )
        await db.commit()
    await message.answer(f"⭐ Favorited {uid}")


@admin.message(Command("unfav"))
async def cmd_unfav(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /unfav <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,favorite) VALUES(?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET favorite=0",
            (uid,),
        )
        await db.commit()
    await message.answer(f"Removed favorite {uid}")


@admin.message(Command("note_set"))
async def note_set(message: Message):
    if not is_owner(message):
        return
    try:
        _, uid_str, *rest = (message.text or "").split()
        uid = int(uid_str)
        note = " ".join(rest).strip()
    except Exception:
        return await message.answer("Usage: /note_set <user_id> <text>")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,note) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET note=excluded.note",
            (uid, note),
        )
        await db.commit()
    await message.answer("📝 Note saved.")


@admin.message(Command("note_get"))
async def note_get(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /note_get <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT note FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    await message.answer(f"Note: {hesc(row[0]) if row and row[0] else '—'}", parse_mode=ParseMode.HTML)


@admin.message(Command("tag_set"))
async def tag_set(message: Message):
    if not is_owner(message):
        return
    try:
        _, uid_str, *rest = (message.text or "").split()
        uid = int(uid_str)
        tags = " ".join(rest).replace(",", " ").split()
        tags = ",".join(sorted(set(tags)))
    except Exception:
        return await message.answer("Usage: /tag_set <user_id> <tag1,tag2>")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,tags) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET tags=excluded.tags",
            (uid, tags),
        )
        await db.commit()
    await message.answer(f"🏷️ Tags set: {hesc(tags) or '—'}", parse_mode=ParseMode.HTML)


@admin.message(Command("tag_get"))
async def tag_get(message: Message):
    if not is_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        return await message.answer("Usage: /tag_get <user_id>")
    uid = int(parts[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tags FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    await message.answer(f"Tags: {hesc(row[0]) if row and row[0] else '—'}", parse_mode=ParseMode.HTML)


@admin.message(Command("qr_add"))
async def qr_add(message: Message):
    if not is_owner(message):
        return
    text = (message.text or "").replace("\n", " ").strip()
    try:
        payload = text.split(" ", 1)[1]
        title, body = payload.split("=", 1)
        title = title.strip().strip('"').strip("'")
        body = body.strip().strip('"').strip("'")
    except Exception:
        return await message.answer('Usage: /qr_add "Title" = "Text"')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO quick_replies(title,text) VALUES(?,?)", (title, body))
        await db.commit()
    await message.answer(f"Added quick reply: <b>{hesc(title)}</b>", parse_mode=ParseMode.HTML)


@admin.message(Command("qr_list"))
async def qr_list(message: Message):
    if not is_owner(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id,title FROM quick_replies ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer("No quick replies yet. Use /qr_add.")
    lines = [f"{i}. {t}" for i, t in rows]
    await message.answer("Quick Replies:\n" + "\n".join(lines))


@admin.message(Command("trigger_add"))
async def trigger_add(message: Message):
    if not is_owner(message):
        return
    text = (message.text or "").replace("\n", " ").strip()
    try:
        payload = text.split(" ", 1)[1]
        key, resp = payload.split("=", 1)
        key = key.strip().lower()
        resp = resp.strip()
    except Exception:
        return await message.answer("Usage: /trigger_add keyword = response")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO triggers(keyword,response) VALUES(?,?)", (key, resp))
        await db.commit()
    await message.answer(f"Trigger added for <code>{hesc(key)}</code>", parse_mode=ParseMode.HTML)


@admin.message(Command("trigger_list"))
async def trigger_list(message: Message):
    if not is_owner(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id,keyword FROM triggers ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer("No triggers yet.")
    await message.answer("Triggers:\n" + "\n".join([f"{i}. {k}" for i, k in rows]))


@admin.message(Command("search"))
async def cmd_search(message: Message):
    if not is_owner(message):
        return
    q = (message.text or "").split(maxsplit=1)
    if len(q) < 2:
        return await message.answer("Usage: /search <text>")
    needle = f"%{q[1].strip()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id,user_id,direction,substr(text,1,80),date FROM messages "
            "WHERE text LIKE ? ORDER BY date DESC LIMIT 10",
            (needle,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer("No matches.")
    lines = []
    for mid, uid, d, t, dtv in rows:
        lines.append(f"#{mid} • {d} • u{uid} • {dtv} • {t or ''}")
    await message.answer("Results:\n" + "\n".join(lines))


@admin.message(Command("unread"))
async def cmd_unread(message: Message):
    if not is_owner(message):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT m.admin_msg_id, m.user_id, u.username, u.full_name, m.date "
            "FROM messages m JOIN users u ON u.user_id=m.user_id "
            "WHERE m.direction='in' AND m.is_read=0 ORDER BY m.date ASC LIMIT 20"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer("🎉 No unread.")
    text = "\n".join(
        [f"• {r[4]} | {r[1]} @{r[2] or '-'} ({hesc(r[3] or '-')}) • admin_msg_id={r[0]}" for r in rows]
    )
    await message.answer("Unread:\n" + text, parse_mode=ParseMode.HTML)


@admin.message(Command("export_csv"))
async def export_csv(message: Message, bot: Bot):
    if not is_owner(message):
        return
    fn = f"inbox_export_{int(datetime.now().timestamp())}.csv"
    with open(fn, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "id",
                "user_id",
                "direction",
                "content_type",
                "text",
                "file_id",
                "date",
                "admin_msg_id",
                "is_read",
            ]
        )
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id,user_id,direction,content_type,text,file_id,date,admin_msg_id,is_read FROM messages ORDER BY id ASC"
            ) as cur:
                async for row in cur:
                    writer.writerow(row)
    await bot.send_document(chat_id=OWNER_ID, document=open(fn, "rb"))
    os.remove(fn)


def parse_schedule_args(text: str) -> Optional[Tuple[int, datetime, str]]:
    """
    /schedule_reply <user_id> <in 10m|YYYY-MM-DD HH:MM> | <message>
    """
    try:
        body = text.split(maxsplit=1)[1]
        part, msg = body.split("|", 1)
        part = part.strip()
        msg = msg.strip()
        uid_str, time_str = part.split(maxsplit=1)
        uid = int(uid_str)
        time_str = time_str.strip()
        if time_str.lower().startswith("in "):
            amt = time_str[3:].strip()
            if amt.endswith("m"):
                delta = timedelta(minutes=int(amt[:-1]))
            elif amt.endswith("h"):
                delta = timedelta(hours=int(amt[:-1]))
            else:
                delta = timedelta(minutes=int(amt))
            send_at = datetime.now(timezone.utc) + delta
        else:
            dt_local = dtparser.parse(time_str)
            if dt_local.tzinfo is None:
                naive = dt_local.replace(tzinfo=IST)
                send_at = naive.astimezone(timezone.utc)
            else:
                send_at = dt_local.astimezone(timezone.utc)
        return uid, send_at, msg
    except Exception:
        return None


@admin.message(Command("schedule_reply"))
async def schedule_reply(message: Message):
    if not is_owner(message):
        return
    parsed = parse_schedule_args(message.text or "")
    if not parsed:
        return await message.answer(
            "Usage:\n/schedule_reply <user_id> <in 10m|YYYY-MM-DD HH:MM> | <message>"
        )
    uid, send_at, msg = parsed
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO scheduled_replies(user_id,text,send_at) VALUES(?,?,?)",
            (uid, msg, send_at),
        )
        await db.commit()
    await message.answer(f"🕒 Scheduled to {uid} at {send_at.isoformat()} (UTC)")


# ---------- OWNER SENDS A REPLY (two ways) ----------
@admin.message(F.reply_to_message)
async def owner_reply_to_forward(message: Message, bot: Bot):
    if not is_owner(message):
        return
    if not message.reply_to_message:
        return
    src_admin_msg_id = message.reply_to_message.message_id
    target_uid = await admin_msg_map_to_user(src_admin_msg_id)
    if not target_uid:
        return await message.answer("Can't map the replied message to a user.")
    try:
        await bot.copy_message(
            chat_id=target_uid, from_chat_id=OWNER_ID, message_id=message.message_id
        )
        await save_message(
            target_uid, "out", message.content_type.name, message.text or "", None, None, 1
        )
        await message.answer("✅ Sent.")
    except TelegramBadRequest as e:
        await message.answer(f"Failed: {e.message}")


# ---------- CALLBACKS (existing 'act|' and new inbox/chat) ----------
@cb.callback_query(F.data.startswith("act|"))
async def on_callback(call: CallbackQuery, bot: Bot, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    parts = call.data.split("|")
    action = parts[1]
    if action == "reply":
        admin_msg_id = int(parts[2])
        target_uid = await admin_msg_map_to_user(admin_msg_id)
        if not target_uid:
            return await call.answer("No mapping.", show_alert=True)
        await state.set_state(ReplyState.awaiting)
        await state.update_data(target_uid=target_uid)
        await call.message.answer(
            f"Reply mode ON → user {target_uid}. Send your message now."
        )
        return await call.answer()
    if action == "qr":
        admin_msg_id = int(parts[2])
        target_uid = await admin_msg_map_to_user(admin_msg_id)
        if not target_uid:
            return await call.answer("No mapping.", show_alert=True)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id,title FROM quick_replies ORDER BY id ASC LIMIT 12"
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await call.answer("No quick replies. Use /qr_add.", show_alert=True)
        kb = InlineKeyboardBuilder()
        for rid, title in rows:
            kb.button(text=title, callback_data=f"act|sendqr|{target_uid}|{rid}")
        kb.adjust(2)
        await call.message.answer(
            "Choose a quick reply:", reply_markup=kb.as_markup()
        )
        return await call.answer()
    if action == "sendqr":
        target_uid = int(parts[2])
        qr_id = int(parts[3])
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT text FROM quick_replies WHERE id=?", (qr_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return await call.answer("QR missing.")
        text = row[0]
        await bot.send_message(chat_id=target_uid, text=text)
        await save_message(target_uid, "out", "text", text, None, None, 1)
        await call.answer("Sent.")
        return
    if action == "info":
        admin_msg_id = int(parts[2])
        uid = await admin_msg_map_to_user(admin_msg_id)
        if not uid:
            return await call.answer("No mapping.")
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT username,full_name,is_whitelisted,is_blocked,favorite,tags,note FROM users WHERE user_id=?",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
        username, full_name, wl, bl, fav, tags, note = row or ("", "", 0, 0, 0, "", "")
        await call.message.answer(
            f"{fmt_user_link(uid, username, full_name)}\n"
            f"Whitelist: {bool(wl)} | Blocked: {bool(bl)} | Fav: {bool(fav)}\n"
            f"Tags: {hesc(tags) or '—'}\nNote: {hesc(note) or '—'}",
            parse_mode=ParseMode.HTML,
        )
        return await call.answer()
    if action == "note":
        uid = int(parts[2])
        await state.set_state(NoteState.typing)
        await state.update_data(note_uid=uid)
        await call.message.answer(f"Send note text for {uid}:")
        return await call.answer()
    if action == "fav":
        uid = int(parts[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET favorite=CASE WHEN favorite=1 THEN 0 ELSE 1 END WHERE user_id=?",
                (uid,),
            )
            await db.commit()
        await call.answer("Toggled favorite.")
        return
    if action == "block":
        uid = int(parts[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET is_blocked=CASE WHEN is_blocked=1 THEN 0 ELSE 1 END WHERE user_id=?",
                (uid,),
            )
            await db.commit()
        await call.answer("Toggled block.")
        return
    if action == "read":
        admin_msg_id = int(parts[2])
        await mark_read_by_admin_msg(admin_msg_id)
        await call.answer("Marked read.")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


# ---- NEW: Inbox & Chat callbacks ----
@cb.callback_query(F.data.startswith("inb|"))
async def on_inbox_cb(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, op, a, b, c = call.data.split("|")
    if op == "page":
        page = int(a)
        sort = int(b)
        archived = int(c)
        rows, total = await fetch_inbox_users(page, sort, archived)
        txt = build_inbox_text(rows, page, total, sort, archived)
        await call.message.edit_text(
            txt,
            reply_markup=kb_inbox(rows, page, total, sort, archived),
            parse_mode=ParseMode.HTML,
        )
        return await call.answer()
    if op == "sort":
        old_sort = int(a)
        page = int(b)
        archived = int(c)
        new_sort = (old_sort + 1) % 3
        rows, total = await fetch_inbox_users(0, new_sort, archived)
        txt = build_inbox_text(rows, 0, total, new_sort, archived)
        await call.message.edit_text(
            txt,
            reply_markup=kb_inbox(rows, 0, total, new_sort, archived),
            parse_mode=ParseMode.HTML,
        )
        return await call.answer(f"Sort: {['Last','Unread','Fav'][new_sort]}")
    if op == "markall":
        sort = int(a)
        page = int(b)
        archived = int(c)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE messages SET is_read=1 WHERE direction='in' AND is_read=0"
            )
            await db.commit()
        rows, total = await fetch_inbox_users(page, sort, archived)
        txt = build_inbox_text(rows, page, total, sort, archived)
        await call.message.edit_text(
            txt,
            reply_markup=kb_inbox(rows, page, total, sort, archived),
            parse_mode=ParseMode.HTML,
        )
        return await call.answer("All marked read.")


@cb.callback_query(F.data.startswith("chat|"))
async def on_chat_open(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    # chat|uid|offset|sort|page|archived
    _, uid_s, off_s, sort_s, page_s, arch_s = call.data.split("|")
    uid = int(uid_s)
    offset = int(off_s)
    sort = int(sort_s)
    page = int(page_s)
    archived = int(arch_s)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, full_name, favorite, is_blocked FROM users WHERE user_id=?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return await call.answer("User not found.", show_alert=True)
    username, full_name, fav, blk = row
    msgs = await fetch_chat_messages(uid, offset)
    txt = build_chat_text(uid, username or "", full_name or "", msgs, offset)
    kb = kb_chat(uid, sort, page, offset, bool(fav), bool(blk), archived)
    await call.message.edit_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    return await call.answer()


@cb.callback_query(F.data.startswith("chatreply|"))
async def on_chatreply(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, *_ = call.data.split("|")
    uid = int(uid_s)
    await state.set_state(ReplyState.awaiting)
    await state.update_data(target_uid=uid)
    await call.message.answer(
        f"Reply mode ON → user {uid}. Send your message now. (/cancel to stop)"
    )
    return await call.answer()


@cb.callback_query(F.data.startswith("chatmr|"))
async def on_chat_markread(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, sort_s, page_s, off_s, arch_s = call.data.split("|")
    uid = int(uid_s)
    sort = int(sort_s)
    page = int(page_s)
    offset = int(off_s)
    archived = int(arch_s)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET is_read=1 WHERE user_id=? AND direction='in' AND is_read=0",
            (uid,),
        )
        await db.commit()
        async with db.execute(
            "SELECT username,full_name,favorite,is_blocked FROM users WHERE user_id=?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
    username, full_name, fav, blk = row
    msgs = await fetch_chat_messages(uid, offset)
    txt = build_chat_text(uid, username or "", full_name or "", msgs, offset)
    kb = kb_chat(uid, sort, page, offset, bool(fav), bool(blk), archived)
    await call.message.edit_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    return await call.answer("Marked all read.")


@cb.callback_query(F.data.startswith("chatfav|"))
async def on_chat_fav(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, sort_s, page_s, off_s, arch_s = call.data.split("|")
    uid = int(uid_s)
    sort = int(sort_s)
    page = int(page_s)
    offset = int(off_s)
    archived = int(arch_s)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET favorite=CASE WHEN favorite=1 THEN 0 ELSE 1 END WHERE user_id=?",
            (uid,),
        )
        await db.commit()
        async with db.execute(
            "SELECT username,full_name,favorite,is_blocked FROM users WHERE user_id=?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
    username, full_name, fav, blk = row
    msgs = await fetch_chat_messages(uid, offset)
    txt = build_chat_text(uid, username or "", full_name or "", msgs, offset)
    kb = kb_chat(uid, sort, page, offset, bool(fav), bool(blk), archived)
    await call.message.edit_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    return await call.answer("Toggled favorite.")


@cb.callback_query(F.data.startswith("chatblk|"))
async def on_chat_block(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, sort_s, page_s, off_s, arch_s = call.data.split("|")
    uid = int(uid_s)
    sort = int(sort_s)
    page = int(page_s)
    offset = int(off_s)
    archived = int(arch_s)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_blocked=CASE WHEN is_blocked=1 THEN 0 ELSE 1 END WHERE user_id=?",
            (uid,),
        )
        await db.commit()
        async with db.execute(
            "SELECT username,full_name,favorite,is_blocked FROM users WHERE user_id=?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
    username, full_name, fav, blk = row
    msgs = await fetch_chat_messages(uid, offset)
    txt = build_chat_text(uid, username or "", full_name or "", msgs, offset)
    kb = kb_chat(uid, sort, page, offset, bool(fav), bool(blk), archived)
    await call.message.edit_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    return await call.answer("Toggled block.")


@cb.callback_query(F.data.startswith("chatarc|"))
async def on_chat_archive(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, sort_s, page_s, off_s, arch_s = call.data.split("|")
    uid = int(uid_s)
    sort = int(sort_s)
    page = int(page_s)
    offset = int(off_s)
    archived = int(arch_s)
    new_arch = 0 if archived else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET archived=? WHERE user_id=?", (new_arch, uid))
        await db.commit()
        async with db.execute(
            "SELECT username,full_name,favorite,is_blocked FROM users WHERE user_id=?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
    username, full_name, fav, blk = row
    msgs = await fetch_chat_messages(uid, offset)
    txt = build_chat_text(uid, username or "", full_name or "", msgs, offset)
    kb = kb_chat(uid, sort, page, offset, bool(fav), bool(blk), new_arch)
    await call.message.edit_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    return await call.answer("Archived." if new_arch else "Unarchived.")


@cb.callback_query(F.data.startswith("chatnote|"))
async def on_chat_note(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, *_ = call.data.split("|")
    uid = int(uid_s)
    await state.set_state(NoteState.typing)
    await state.update_data(note_uid=uid)
    await call.message.answer(f"Send note text for {uid}:")
    return await call.answer()


@cb.callback_query(F.data.startswith("chattag|"))
async def on_chat_tag(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid_s, *_ = call.data.split("|")
    uid = int(uid_s)
    await state.set_state(TagState.typing)
    await state.update_data(tag_uid=uid)
    await call.message.answer("Send tags for {uid} (space/comma separated):")
    return await call.answer()


@admin.message(TagState.typing)
async def tag_state_set(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("tag_uid")
    if not uid:
        await state.clear()
        return await message.answer("No target.")
    tags = " ".join((message.text or "").replace(",", " ").split())
    tags = ",".join(sorted(set(tags.split()))) if tags else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id,tags) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET tags=excluded.tags",
            (uid, tags),
        )
        await db.commit()
    await state.clear()
    await message.answer(f"🏷️ Tags set: {hesc(tags) or '—'}", parse_mode=ParseMode.HTML)


# ---------- OWNER REPLY MODE (via button) ----------
@admin.message(ReplyState.awaiting)
async def owner_reply_mode(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_uid = data.get("target_uid")
    if not target_uid:
        await state.clear()
        return await message.answer("No target.")
    try:
        await bot.copy_message(
            chat_id=target_uid, from_chat_id=OWNER_ID, message_id=message.message_id
        )
        await save_message(
            target_uid, "out", message.content_type.name, message.text or "", None, None, 1
        )
        await message.answer("✅ Sent. (Reply mode still ON, /cancel to stop)")
    except TelegramBadRequest as e:
        await message.answer(f"Failed: {e.message}")


@admin.message(Command("cancel"))
async def cancel_states(message: Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.clear()
    await message.answer("Cancelled.")


# ---------- PUBLIC HANDLERS ----------
@public.message(F.chat.type == ChatType.PRIVATE)
async def on_private_user_msg(message: Message, bot: Bot):
    if message.from_user.id == OWNER_ID:
        return
    await ensure_user(message.from_user)
    uid = message.from_user.id
    username = message.from_user.username
    full = message.from_user.full_name or ""

    # Flags & settings
    is_blocked, is_whitelisted, is_fav, _ = await get_user_flags(uid)
    if is_blocked:
        return  # ignore silently

    whitelist_mode = (
        await get_setting("whitelist_mode", "1" if WHITELIST_MODE else "0")
    ) == "1"
    if whitelist_mode and not is_whitelisted:
        try:
            await message.answer("Sorry, DMs are currently restricted. (Whitelist mode ON)")
        except TelegramBadRequest:
            pass
        await save_message(
            uid, "in", message.content_type.name, message.text or "", None, None, 1
        )
        return

    # Rate limit
    if await count_last_min_msgs(uid) >= int(
        await get_setting("rate_limit_per_min", str(RATE_LIMIT_PER_MIN))
    ):
        await message.answer("Please slow down; you're sending messages too quickly.")
        await save_message(
            uid, "in", message.content_type.name, message.text or "", None, None, 1
        )
        return

    # Triggers
    if message.text:
        txt_l = message.text.lower()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT keyword,response FROM triggers") as cur:
                trs = await cur.fetchall()
        for k, resp in trs or []:
            if k in txt_l:
                await bot.send_message(chat_id=uid, text=resp)
                await save_message(uid, "out", "text", resp, None, None, 1)

    # Translation preview (escape text for HTML)
    preview_text = ""
    if message.text:
        orig = hesc(message.text)
        if TRANSLATE_ENABLED:
            t = await translate_if_enabled(message.text)
            t = hesc(t)
            if t and t != message.text:
                preview_text = f"{orig}\n\n———\n🌐 Translation → {TRANSLATE_TO}:\n{t}"
            else:
                preview_text = orig
        else:
            preview_text = orig

    # Silent mode?
    silent_until_iso = await get_setting("silent_until", "")
    forward_now = True
    if silent_until_iso:
        try:
            silent_until = datetime.fromisoformat(silent_until_iso)
            if datetime.now(timezone.utc) < silent_until and not is_fav:
                forward_now = False
        except Exception:
            forward_now = True

    # Forward to OWNER
    admin_msg_id = None
    if forward_now:
        try:
            sent = await bot.copy_message(
                chat_id=OWNER_ID, from_chat_id=uid, message_id=message.message_id
            )
            admin_msg_id = sent.message_id
            # attach control kb
            blocked, _, fav, _ = await get_user_flags(uid)
            kb = kb_admin_for(uid, admin_msg_id, blocked, fav)
            info_line = fmt_user_link(uid, username, full)
            if preview_text:
                await bot.send_message(
                    OWNER_ID,
                    f"{info_line}\n\n{preview_text}",
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await bot.send_message(
                    OWNER_ID, info_line, reply_markup=kb, parse_mode=ParseMode.HTML
                )
        except TelegramBadRequest as e:
            await bot.send_message(
                OWNER_ID, f"⚠️ Failed to copy message from {uid}: {e.message}"
            )

        await save_message(
            uid, "in", message.content_type.name, message.text or "", None, admin_msg_id, 0
        )
    else:
        await save_message(
            uid, "in", message.content_type.name, message.text or "", None, None, 0
        )

    # Away auto-reply (1x/hour per user)
    away_text = (await get_setting("away_text", AWAY_TEXT or "")) or ""
    if away_text:
        should_send = True
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT last_auto_reply_at FROM users WHERE user_id=?", (uid,)
            ) as cur:
                row = await cur.fetchone()
        if row and row[0]:
            try:
                last = datetime.fromisoformat(row[0])
                if datetime.now(timezone.utc) - last < timedelta(hours=1):
                    should_send = False
            except Exception:
                pass
        if should_send:
            try:
                await bot.send_message(chat_id=uid, text=away_text)
                await save_message(uid, "out", "text", away_text, None, None, 1)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE users SET last_auto_reply_at=? WHERE user_id=?",
                        (datetime.now(timezone.utc).isoformat(), uid),
                    )
                    await db.commit()
            except TelegramBadRequest:
                pass


# ---------- SCHEDULER BACKGROUND TASK ----------
async def scheduler_task(bot: Bot):
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id,user_id,text FROM scheduled_replies WHERE sent=0 AND send_at<=?",
                    (now,),
                ) as cur:
                    rows = await cur.fetchall()
                for rid, uid, text in rows:
                    try:
                        await bot.send_message(chat_id=uid, text=text)
                        await save_message(uid, "out", "text", text, None, None, 1)
                        await db.execute(
                            "UPDATE scheduled_replies SET sent=1 WHERE id=?", (rid,)
                        )
                        await db.commit()
                    except TelegramBadRequest:
                        pass
        except Exception:
            pass
        await asyncio.sleep(30)


# ---------- APP ----------
async def main():
    await init_db()
    from aiogram.client.default import DefaultBotProperties

    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN missing in .env")
    if not OWNER_ID:
        raise RuntimeError("❌ OWNER_ID missing in .env")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.include_router(cb)
    dp.include_router(admin)
    dp.include_router(public)

    asyncio.create_task(scheduler_task(bot))

    logging.info("✅ Bot started. Owner ID: %s", OWNER_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
