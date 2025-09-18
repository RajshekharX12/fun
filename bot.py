# bot.py — Personal Inbox Bot (Aiogram v3.7+)
# Copy-paste ready.

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
    FSInputFile,
    ReactionTypeEmoji,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

from dotenv import load_dotenv

# ---------------- ENV / CONFIG ----------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
TZ_LABEL = os.getenv("TZ", "Asia/Kolkata").strip()
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "100"))
WHITELIST_MODE_DEFAULT = os.getenv("WHITELIST_MODE", "false").lower() == "true"
AWAY_TEXT_DEFAULT = os.getenv("AWAY_TEXT", "").strip()
ACK_TEXT = os.getenv(
    "ACK_TEXT",
    "✅ Your message has been received. I’ll reply as soon as possible."
).strip()
DEFAULT_PFP_PATH = os.getenv("DEFAULT_PFP_PATH", "").strip()   # optional jpg/png path

if not BOT_TOKEN or not OWNER_ID:
    raise SystemExit("Please set BOT_TOKEN and OWNER_ID in .env")

# ---------------- TIME / DB ----------------
IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = "inbox.db"

PAGE_SIZE = 8       # users per inbox page
HIST_PAGE = 12     # history messages per page

# ---------------- STATES ----------------
class ReplyState(StatesGroup):
    awaiting = State()

class NoteState(StatesGroup):
    typing = State()

class TagState(StatesGroup):
    typing = State()

class AliasState(StatesGroup):
    typing = State()

class FindState(StatesGroup):
    typing = State()

# ---------------- ROUTERS ----------------
admin = Router(name="admin")
public = Router(name="public")
cb = Router(name="callbacks")
home = Router(name="home")

# ---------------- DB INIT / HELPERS ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            alias TEXT DEFAULT '',
            is_whitelisted INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            muted INTEGER DEFAULT 0,
            tags TEXT DEFAULT '',
            note TEXT DEFAULT '',
            favorite INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            priority_pin INTEGER DEFAULT 0,
            last_seen TIMESTAMP,
            last_auto_reply_at TIMESTAMP,
            last_ack_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            direction TEXT CHECK(direction IN ('in','out')) NOT NULL,
            content_type TEXT,
            text TEXT,
            file_id TEXT,
            date TIMESTAMP NOT NULL,
            admin_msg_id INTEGER,
            orig_msg_id INTEGER,
            is_read INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
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

def is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_ID

async def ensure_user(u) -> None:
    if not u:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(user_id,username,full_name,last_seen)
            VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen
            """,
            (u.id, u.username or "", (u.full_name or "").strip(), datetime.now(timezone.utc)),
        )
        await db.commit()

async def get_user_flags(user_id: int) -> Tuple[bool, bool, bool, bool, bool, bool]:
    """returns (blocked, whitelisted, favorite, archived, muted, priority_pin)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_blocked,is_whitelisted,favorite,archived,muted,priority_pin FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False, False, False, False, False, False
            return tuple(bool(x) for x in row)  # type: ignore

async def save_message(
    user_id: int,
    direction: str,
    content_type: str,
    text: Optional[str],
    file_id: Optional[str],
    admin_msg_id: Optional[int],
    orig_msg_id: Optional[int],
    is_read: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages(user_id, direction, content_type, text, file_id, date, admin_msg_id, orig_msg_id, is_read)
            VALUES (?,?,?,?,?,?,?, ?,?)
            """,
            (
                user_id,
                direction,
                content_type,
                text,
                file_id,
                datetime.now(timezone.utc),
                admin_msg_id,
                orig_msg_id,
                is_read,
            ),
        )
        await db.commit()

def dt_ist(dt_utc: datetime) -> str:
    try:
        return dt_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_utc)

async def count_last_min_msgs(user_id: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id=? AND direction='in' AND date>=?",
            (user_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def mark_all_read(uid: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if uid is None:
            await db.execute("UPDATE messages SET is_read=1 WHERE direction='in' AND is_read=0")
        else:
            await db.execute("UPDATE messages SET is_read=1 WHERE user_id=? AND direction='in' AND is_read=0", (uid,))
        await db.commit()

async def delete_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.commit()

async def clear_chat(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        await db.commit()

async def export_csv(uid: int, path: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT direction,date,content_type,text,file_id FROM messages WHERE user_id=? ORDER BY date",
            (uid,),
        ) as cur, open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["direction", "date(UTC)", "content_type", "text", "file_id"])
            async for d, dtv, ctype, txt, fid in cur:
                w.writerow([d, dtv, ctype or "", txt or "", fid or ""])

async def safe_edit_text(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        # Ignore "message is not modified"
        if "message is not modified" in (e.message or "").lower():
            return
        raise

def fmt_user_line(name, uname, uid, unread, total, fav, blk, arch, muted, tags) -> str:
    pfx = []
    if fav: pfx.append("⭐")
    if blk: pfx.append("🚫")
    if muted: pfx.append("🔕")
    if arch: pfx.append("📦")
    pfx = "".join(pfx)
    tgs = f" • 🏷️ {hesc(tags)}" if tags else ""
    return f"{pfx} <b>{hesc(name or '—')}</b> (@{uname or '—'}) • <code>{uid}</code> • {total} msgs / <b>{unread}</b> unread{tgs}"

# ---------------- INBOX ----------------
async def fetch_inbox(page: int, archived: int, sort_key: str) -> Tuple[List[Dict], int]:
    sort_sql = "last_date DESC"
    if sort_key == "unread":
        sort_sql = "unread DESC, last_date DESC"
    if sort_key == "fav":
        sort_sql = "favorite DESC, last_date DESC"

    offset = page * PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
        SELECT COUNT(*) FROM users u
        WHERE u.archived=? AND EXISTS (SELECT 1 FROM messages m WHERE m.user_id=u.user_id)
        """, (archived,)) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(f"""
        SELECT
          u.user_id, u.username, u.full_name, u.alias, u.favorite, u.is_blocked, u.archived, u.muted, u.tags,
          (SELECT COUNT(*) FROM messages i WHERE i.user_id=u.user_id) AS total_msgs,
          (SELECT COUNT(*) FROM messages i WHERE i.user_id=u.user_id AND i.direction='in' AND i.is_read=0) AS unread,
          (SELECT MAX(date) FROM messages i WHERE i.user_id=u.user_id) AS last_date
        FROM users u
        WHERE u.archived=?
          AND EXISTS (SELECT 1 FROM messages mx WHERE mx.user_id=u.user_id)
        ORDER BY {sort_sql}
        LIMIT ? OFFSET ?
        """, (archived, PAGE_SIZE, offset)) as cur:
            rows = []
            async for r in cur:
                rows.append(dict(
                    user_id=r[0], username=r[1], full_name=r[2] or "",
                    alias=r[3] or "", favorite=bool(r[4]), blocked=bool(r[5]),
                    archived=bool(r[6]), muted=bool(r[7]), tags=r[8] or "",
                    total=int(r[9] or 0), unread=int(r[10] or 0), last_date=r[11]
                ))
    return rows, total

def inbox_text(rows: List[Dict], page: int, total: int, archived: int, sort_key: str) -> str:
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    head = f"<b>{'Archived' if archived else 'Inbox'}</b> • Page {page+1}/{total_pages} • Sort: {hesc(sort_key.capitalize())}\n\n"
    if not rows:
        return head + "No conversations."
    lines = [head]
    for r in rows:
        display = r["alias"] or r["full_name"] or r["username"] or str(r["user_id"])
        lines.append("• " + fmt_user_line(display, r["username"], r["user_id"], r["unread"], r["total"], r["favorite"], r["blocked"], r["archived"], r["muted"], r["tags"]))
    return "\n".join(lines)

def kb_inbox(rows: List[Dict], page: int, total: int, archived: int, sort_key: str) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    b = InlineKeyboardBuilder()
    for r in rows:
        label = f"{('⭐ ' if r['favorite'] else '')}{r['alias'] or r['full_name'] or r['username'] or r['user_id']} • {r['total']}/{r['unread']}"
        b.button(text=label[:64], callback_data=f"chat|{r['user_id']}|{archived}|{sort_key}|{page}")
    # Controls
    controls = []
    if page > 0:
        b.button(text="⏮ Prev", callback_data=f"inb|page|{page-1}|{archived}|{sort_key}")
        controls.append(1)
    if page < total_pages - 1:
        b.button(text="⏭ Next", callback_data=f"inb|page|{page+1}|{archived}|{sort_key}")
        controls.append(1)
    b.button(text=f"🔁 Sort ({sort_key})", callback_data=f"inb|sort|{page}|{archived}|{sort_key}")
    b.button(text=("📦 Archived" if not archived else "📥 Inbox"), callback_data=f"inb|switch|{page}|{archived}|{sort_key}")
    b.button(text="✅ Mark all read", callback_data="inb|markall")
    b.adjust(1, 2, 2)
    return b.as_markup()

# ---------------- CHAT & HISTORY ----------------
async def fetch_history(uid: int, offset: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT direction, content_type, text, date FROM messages
            WHERE user_id=? ORDER BY date DESC LIMIT ? OFFSET ?
        """, (uid, HIST_PAGE, offset)) as cur:
            rows = []
            async for d, c, t, dtv in cur:
                rows.append(dict(direction=d, content_type=c or "", text=t or "", date=dtv))
    return rows

def history_text(uid: int, name: str, uname: str, msgs: List[Dict], offset: int) -> str:
    head = f"<b>History</b> with <b>{hesc(name)}</b> (@{uname or '—'}) • ID <code>{uid}</code>\n\n"
    if not msgs:
        return head + "No messages yet."
    lines = [head]
    for m in msgs:
        tag = "⬅️ IN" if m["direction"] == "in" else "➡️ OUT"
        body = m["text"] or f"[{m['content_type']}]"
        body = hesc(body[:300])
        dt = dt_ist(datetime.fromisoformat(m["date"]))
        lines.append(f"{tag} • {dt}\n{body}")
    lines.append(f"\nPage offset: {offset}")
    return "\n".join(lines)

def kb_history(uid: int, archived: int, sort_key: str, page_back: int, offset: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    prev_off = max(0, offset + HIST_PAGE)
    next_off = max(0, offset - HIST_PAGE)
    b.button(text="⏮ Older", callback_data=f"hist|{uid}|{prev_off}|{archived}|{sort_key}|{page_back}")
    if offset > 0:
        b.button(text="⏭ Newer", callback_data=f"hist|{uid}|{next_off}|{archived}|{sort_key}|{page_back}")
    b.button(text="⬅️ Back", callback_data=f"chat|{uid}|{archived}|{sort_key}|{page_back}")
    b.adjust(2,1)
    return b.as_markup()

def kb_chat(uid: int, fav: bool, blk: bool, arch: bool, muted: bool, page_back: int, archived: int, sort_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💬 Reply", callback_data=f"chatreply|{uid}")
    b.button(text="🗂 History", callback_data=f"hist|{uid}|0|{archived}|{sort_key}|{page_back}")
    b.button(text="✅ Mark read", callback_data=f"chatmr|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text=("⭐ Unfav" if fav else "⭐ Fav"), callback_data=f"chatfav|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text=("🚫 Unblock" if blk else "🚫 Block"), callback_data=f"chatblk|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text=("🔕 Unmute" if muted else "🔕 Mute"), callback_data=f"chatmute|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text=("📦 Unarchive" if arch else "📦 Archive"), callback_data=f"chatarc|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="📝 Note", callback_data=f"chatnote|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="🏷️ Tags", callback_data=f"chattag|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="❤️ React", callback_data=f"react|menu|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="✨ Extras", callback_data=f"extras|menu|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="⬅️ Back to Inbox", callback_data=f"inb|page|{page_back}|{archived}|{sort_key}")
    b.adjust(2,2,2,2,2,2,1)
    return b.as_markup()

def chat_text(uid: int, username: str, name: str) -> str:
    return f"<b>Chat with</b> {hesc(name)} (@{username or '—'}) • ID <code>{uid}</code>"

# ---------------- START / HELP ----------------
@home.message(CommandStart())
async def start(message: Message):
    if not is_owner(message):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Inbox", callback_data="inb|page|0|0|last")
    kb.button(text="📦 Archived", callback_data="inb|page|0|1|last")
    kb.button(text="👥 Contacts", callback_data="home|contacts")
    kb.button(text="❓ Help", callback_data="home|help")
    kb.adjust(2,2)
    await message.answer(
        "👋 <b>Personal Inbox Bot</b> is up.\n\n"
        "• Full Inbox: /inbox • Archived: /inbox_archived • Sort: /inbox_sort\n"
        "• Open chat: /open &lt;user_id&gt; • Contacts: /contacts [all|whitelist|blocked|favorites]\n"
        "• Mark all read: /mark_all_read\n"
        "Type /help for full commands.",
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

@admin.message(Command("help"))
async def owner_help(message: Message):
    if not is_owner(message):
        return
    await message.answer(
        "<b>Commands</b>\n"
        "• /inbox • /inbox_archived • /inbox_sort &lt;last|unread|fav&gt;\n"
        "• /open &lt;user_id&gt; • /mark_all_read\n"
        "• /alias &lt;user_id&gt; &lt;name&gt;\n"
        "• /contacts [all|whitelist|blocked|favorites]\n",
        parse_mode=ParseMode.HTML
    )

# ---------------- COMMANDS (OWNER) ----------------
@admin.message(Command("inbox"))
async def cmd_inbox(message: Message):
    if not is_owner(message): return
    rows, total = await fetch_inbox(page=0, archived=0, sort_key="last")
    await message.answer(inbox_text(rows, 0, total, 0, "last"),
                         reply_markup=kb_inbox(rows, 0, total, 0, "last"),
                         parse_mode=ParseMode.HTML)

@admin.message(Command("inbox_archived"))
async def cmd_inbox_archived(message: Message):
    if not is_owner(message): return
    rows, total = await fetch_inbox(page=0, archived=1, sort_key="last")
    await message.answer(inbox_text(rows, 0, total, 1, "last"),
                         reply_markup=kb_inbox(rows, 0, total, 1, "last"),
                         parse_mode=ParseMode.HTML)

@admin.message(Command("inbox_sort"))
async def cmd_inbox_sort(message: Message):
    if not is_owner(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1] not in ("last", "unread", "fav"):
        return await message.answer("Usage: /inbox_sort &lt;last|unread|fav&gt;", parse_mode=ParseMode.HTML)
    key = parts[1]
    rows, total = await fetch_inbox(page=0, archived=0, sort_key=key)
    await message.answer(inbox_text(rows, 0, total, 0, key),
                         reply_markup=kb_inbox(rows, 0, total, 0, key),
                         parse_mode=ParseMode.HTML)

@admin.message(Command("open"))
async def cmd_open(message: Message, bot: Bot):
    if not is_owner(message): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer("Usage: /open &lt;user_id&gt;", parse_mode=ParseMode.HTML)
    uid = int(parts[1])
    # fetch user
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username,full_name,alias,favorite,is_blocked,archived,muted FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    if not row:
        return await message.answer("User not found.")
    username, full_name, alias, fav, blk, arch, muted = row
    name = alias or full_name or username or str(uid)

    # Try to send avatar (fallback to default)
    sent_photo = False
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        if photos and photos.total_count:
            fid = photos.photos[0][0].file_id
            await bot.send_photo(OWNER_ID, fid, caption=chat_text(uid, username, name), reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), 0, int(arch), "last"), parse_mode=ParseMode.HTML)
            sent_photo = True
    except Exception:
        pass
    if not sent_photo and DEFAULT_PFP_PATH and os.path.exists(DEFAULT_PFP_PATH):
        await bot.send_photo(OWNER_ID, FSInputFile(DEFAULT_PFP_PATH), caption=chat_text(uid, username, name), reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), 0, int(arch), "last"), parse_mode=ParseMode.HTML)
    else:
        if not sent_photo:
            await message.answer(chat_text(uid, username, name), reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), 0, int(arch), "last"), parse_mode=ParseMode.HTML)

@admin.message(Command("mark_all_read"))
async def cmd_mark_all_read(message: Message):
    if not is_owner(message): return
    await mark_all_read()
    await message.answer("✅ Marked all as read.")

@admin.message(Command("alias"))
async def cmd_alias(message: Message):
    if not is_owner(message): return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await message.answer("Usage: /alias &lt;user_id&gt; &lt;new_name&gt;", parse_mode=ParseMode.HTML)
    uid = int(parts[1]); alias = parts[2].strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET alias=? WHERE user_id=?", (alias, uid))
        await db.commit()
    await message.answer("✅ Alias updated.")

# ---------------- CALLBACKS: INBOX NAV ----------------
@cb.callback_query(F.data.startswith("inb|"))
async def on_inbox_cb(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    parts = call.data.split("|")
    action = parts[1]
    if action == "markall":
        await mark_all_read()
        return await call.answer("Marked all read.")
    if action == "page":
        page, archived, sort_key = int(parts[2]), int(parts[3]), parts[4]
    elif action == "sort":
        page, archived, sort_key = int(parts[2]), int(parts[3]), parts[4]
        # rotate sort
        sort_key = {"last":"unread","unread":"fav","fav":"last"}[sort_key]
    elif action == "switch":
        page, archived, sort_key = int(parts[2]), int(parts[3]), parts[4]
        archived = 0 if archived else 1
    else:
        return

    rows, total = await fetch_inbox(page=page, archived=archived, sort_key=sort_key)
    await safe_edit_text(
        call.message,
        inbox_text(rows, page, total, archived, sort_key),
        reply_markup=kb_inbox(rows, page, total, archived, sort_key)
    )
    await call.answer()

# ---------------- CALLBACKS: CHAT PANEL ----------------
@cb.callback_query(F.data.startswith("chat|"))
async def open_chat(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Not for you.")
    _, uid, archived, sort_key, page_back = call.data.split("|")
    uid = int(uid); archived = int(archived); page_back = int(page_back)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username,full_name,alias,favorite,is_blocked,archived,muted FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    if not row:
        return await call.answer("User not found.", show_alert=True)
    username, full_name, alias, fav, blk, arch, muted = row
    name = alias or full_name or username or str(uid)

    await safe_edit_text(
        call.message,
        chat_text(uid, username, name),
        reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), page_back, archived, sort_key)
    )
    await call.answer()

@cb.callback_query(F.data.startswith("chatmr|"))
async def chat_mark_read(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer()
    _, uid, archived, sort_key, page_back = call.data.split("|")
    await mark_all_read(int(uid))
    await call.answer("Marked as read.")

@cb.callback_query(F.data.startswith("chatfav|"))
async def chat_fav(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET favorite = 1 - favorite WHERE user_id=?", (uid,))
        await db.commit()
        async with db.execute("SELECT username,full_name,alias,favorite,is_blocked,archived,muted FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    username, full_name, alias, fav, blk, arch, muted = row
    name = alias or full_name or username or str(uid)
    await safe_edit_text(call.message, chat_text(uid, username, name),
                         reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), page_back, archived, sort_key))
    await call.answer("Updated.")

@cb.callback_query(F.data.startswith("chatblk|"))
async def chat_block(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked = 1 - is_blocked WHERE user_id=?", (uid,))
        await db.commit()
        async with db.execute("SELECT username,full_name,alias,favorite,is_blocked,archived,muted FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    username, full_name, alias, fav, blk, arch, muted = row
    name = alias or full_name or username or str(uid)
    await safe_edit_text(call.message, chat_text(uid, username, name),
                         reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), page_back, archived, sort_key))
    await call.answer("Updated.")

@cb.callback_query(F.data.startswith("chatmute|"))
async def chat_mute(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET muted = 1 - muted WHERE user_id=?", (uid,))
        await db.commit()
        async with db.execute("SELECT username,full_name,alias,favorite,is_blocked,archived,muted FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    username, full_name, alias, fav, blk, arch, muted = row
    name = alias or full_name or username or str(uid)
    await safe_edit_text(call.message, chat_text(uid, username, name),
                         reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), page_back, archived, sort_key))
    await call.answer("Updated.")

@cb.callback_query(F.data.startswith("chatarc|"))
async def chat_archive(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET archived = 1 - archived WHERE user_id=?", (uid,))
        await db.commit()
        async with db.execute("SELECT username,full_name,alias,favorite,is_blocked,archived,muted FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    username, full_name, alias, fav, blk, arch, muted = row
    name = alias or full_name or username or str(uid)
    await safe_edit_text(call.message, chat_text(uid, username, name),
                         reply_markup=kb_chat(uid, bool(fav), bool(blk), bool(arch), bool(muted), page_back, archived, sort_key))
    await call.answer("Updated.")

# Note / Tag editors
@cb.callback_query(F.data.startswith("chatnote|"))
async def chat_note_begin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID: return
    _, uid, archived, sort_key, page_back = call.data.split("|")
    await state.set_state(NoteState.typing)
    await state.update_data(uid=int(uid))
    await call.message.answer("📝 Send note text (or /cancel).")

@admin.message(NoteState.typing)
async def chat_note_save(message: Message, state: FSMContext):
    if not is_owner(message): return
    data = await state.get_data()
    uid = data.get("uid")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET note=? WHERE user_id=?", (message.text or "", uid))
        await db.commit()
    await state.clear()
    await message.answer("✅ Note saved.")

@cb.callback_query(F.data.startswith("chattag|"))
async def chat_tag_begin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID: return
    _, uid, archived, sort_key, page_back = call.data.split("|")
    await state.set_state(TagState.typing)
    await state.update_data(uid=int(uid))
    await call.message.answer("🏷️ Send tags (comma separated).")

@admin.message(TagState.typing)
async def chat_tag_save(message: Message, state: FSMContext):
    if not is_owner(message): return
    data = await state.get_data()
    uid = data.get("uid")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET tags=? WHERE user_id=?", (message.text or "", uid))
        await db.commit()
    await state.clear()
    await message.answer("✅ Tags saved.")

# Reply
@cb.callback_query(F.data.startswith("chatreply|"))
async def chat_reply_begin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID: return
    _, uid = call.data.split("|")
    await state.set_state(ReplyState.awaiting)
    await state.update_data(uid=int(uid))
    await call.message.answer("💬 Send your reply (text). /cancel to stop.")

@admin.message(ReplyState.awaiting)
async def chat_reply_send(message: Message, state: FSMContext, bot: Bot):
    if not is_owner(message): return
    data = await state.get_data()
    uid = int(data["uid"])
    txt = message.text or ""
    # send to user
    sent = await bot.send_message(uid, txt)
    await save_message(uid, "out", "text", txt, None, message.message_id, sent.message_id, 1)
    await state.clear()
    await message.answer("✅ Sent.")

# History
@cb.callback_query(F.data.startswith("hist|"))
async def open_history(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _, uid, offset, archived, sort_key, page_back = call.data.split("|")
    uid = int(uid); offset=int(offset); archived=int(archived); page_back=int(page_back)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username,full_name,alias FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
    uname, fulln, alias = row if row else ("", "", "")
    name = alias or fulln or uname or str(uid)
    msgs = await fetch_history(uid, offset)
    await safe_edit_text(
        call.message,
        history_text(uid, name, uname, msgs, offset),
        reply_markup=kb_history(uid, archived, sort_key, page_back, offset)
    )
    await call.answer()

# Reactions
def kb_react(uid:int, archived:int, sort_key:str, page_back:int) -> InlineKeyboardMarkup:
    b=InlineKeyboardBuilder()
    for emo in ["👍","❤️","🔥","😄","🙏"]:
        b.button(text=emo, callback_data=f"react|set|{uid}|{emo}|{archived}|{sort_key}|{page_back}")
    b.button(text="⬅️ Back", callback_data=f"chat|{uid}|{archived}|{sort_key}|{page_back}")
    b.adjust(5,1)
    return b.as_markup()

@cb.callback_query(F.data.startswith("react|menu|"))
async def react_menu(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _,__, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)
    await safe_edit_text(call.message, "Choose a reaction:", reply_markup=kb_react(uid, archived, sort_key, page_back))
    await call.answer()

@cb.callback_query(F.data.startswith("react|set|"))
async def react_set(call: CallbackQuery, bot: Bot):
    if call.from_user.id != OWNER_ID: return
    _,__, uid, emoji, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid)
    # last incoming message id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT orig_msg_id FROM messages WHERE user_id=? AND direction='in' AND orig_msg_id IS NOT NULL ORDER BY date DESC LIMIT 1",
            (uid,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return await call.answer("No message to react to.", show_alert=True)
    try:
        await bot.set_message_reaction(chat_id=uid, message_id=int(row[0]), reaction=[ReactionTypeEmoji(emoji=emoji)], is_big=False)
        await call.answer("Reacted.")
    except Exception:
        await call.answer("Reaction failed.", show_alert=True)

# Extras menu
def kb_extras(uid:int, archived:int, sort_key:str, page_back:int) -> InlineKeyboardMarkup:
    b=InlineKeyboardBuilder()
    b.button(text="🔄 Refresh avatar", callback_data=f"x|avatar|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="✏️ Set alias", callback_data=f"x|alias|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="🗑 Clear chat", callback_data=f"x|clear|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="❌ Delete user", callback_data=f"x|delete|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="📤 Export CSV", callback_data=f"x|export|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="📌 Toggle priority", callback_data=f"x|prio|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="🖼 Media gallery", callback_data=f"x|media|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="🔎 Find in chat", callback_data=f"x|find|{uid}|{archived}|{sort_key}|{page_back}")
    b.button(text="⬅️ Back", callback_data=f"chat|{uid}|{archived}|{sort_key}|{page_back}")
    b.adjust(2,2,2,2,1)
    return b.as_markup()

@cb.callback_query(F.data.startswith("extras|menu|"))
async def extras_menu(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _,__, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)
    await safe_edit_text(call.message, "✨ Extras", reply_markup=kb_extras(uid, archived, sort_key, page_back))
    await call.answer()

@cb.callback_query(F.data.startswith("x|"))
async def extras_action(call: CallbackQuery, state: FSMContext, bot: Bot):
    if call.from_user.id != OWNER_ID: return
    _, act, uid, archived, sort_key, page_back = call.data.split("|")
    uid=int(uid); archived=int(archived); page_back=int(page_back)

    if act == "avatar":
        sent = False
        try:
            photos = await bot.get_user_profile_photos(uid, limit=1)
            if photos and photos.total_count:
                fid = photos.photos[0][0].file_id
                await bot.send_photo(OWNER_ID, fid, caption="Profile photo")
                sent = True
        except Exception:
            pass
        if not sent and DEFAULT_PFP_PATH and os.path.exists(DEFAULT_PFP_PATH):
            await bot.send_photo(OWNER_ID, FSInputFile(DEFAULT_PFP_PATH), caption="Default profile photo")
        return await call.answer("Done.")
    if act == "alias":
        await state.set_state(AliasState.typing)
        await state.update_data(uid=uid)
        return await call.message.answer("Send new alias for this user.")
    if act == "clear":
        await clear_chat(uid)
        return await call.answer("Chat cleared.")
    if act == "delete":
        await delete_user(uid)
        # go back to inbox
        rows, total = await fetch_inbox(0, 0, "last")
        await safe_edit_text(call.message, inbox_text(rows, 0, total, 0, "last"), reply_markup=kb_inbox(rows, 0, total, 0, "last"))
        return await call.answer("User deleted.")
    if act == "export":
        path = f"chat_{uid}.csv"
        await export_csv(uid, path)
        if os.path.exists(path):
            await bot.send_document(OWNER_ID, FSInputFile(path))
            os.remove(path)
        return await call.answer("Exported.")
    if act == "prio":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET priority_pin = 1 - priority_pin WHERE user_id=?", (uid,))
            await db.commit()
        return await call.answer("Priority toggled.")
    if act == "media":
        # naive: list last 10 media messages
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT file_id,content_type,date FROM messages WHERE user_id=? AND file_id IS NOT NULL ORDER BY date DESC LIMIT 10",
                (uid,)
            ) as cur:
                items = await cur.fetchall()
        if not items:
            await call.message.answer("No media found.")
        else:
            for fid, ctype, _ in items:
                try:
                    if ctype in ("photo","sticker"):
                        await bot.send_photo(OWNER_ID, fid)
                    elif ctype in ("video",):
                        await bot.send_video(OWNER_ID, fid)
                    else:
                        await bot.send_document(OWNER_ID, fid)
                except Exception:
                    pass
        return await call.answer()
    if act == "find":
        await state.set_state(FindState.typing)
        await state.update_data(uid=uid)
        return await call.message.answer("Send a search phrase.")

@admin.message(AliasState.typing)
async def alias_save(message: Message, state: FSMContext):
    if not is_owner(message): return
    data = await state.get_data()
    uid = data.get("uid")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET alias=? WHERE user_id=?", (message.text or "", uid))
        await db.commit()
    await state.clear()
    await message.answer("✅ Alias updated.")

@admin.message(FindState.typing)
async def find_in_chat(message: Message, state: FSMContext):
    if not is_owner(message): return
    data = await state.get_data()
    uid = data.get("uid")
    q = (message.text or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT direction,text,date FROM messages WHERE user_id=? AND text LIKE ? ORDER BY date DESC LIMIT 20",
            (uid, f"%{q}%"),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("No matches.")
    else:
        out = [f"🔎 Results for <b>{hesc(q)}</b>:"]
        for d, t, dtv in rows:
            out.append(f"{'⬅️ IN' if d=='in' else '➡️ OUT'} • {dt_ist(datetime.fromisoformat(dtv))}\n{hesc((t or '')[:150])}")
        await message.answer("\n".join(out), parse_mode=ParseMode.HTML)
    await state.clear()

# ---------------- PUBLIC: USER MESSAGES ----------------
@public.message(F.chat.type == ChatType.PRIVATE)
async def on_user_message(message: Message, bot: Bot):
    # Save/ensure user
    await ensure_user(message.from_user)

    uid = message.from_user.id
    # Check blocked
    blocked, whitelisted, favorite, archived, muted, prio = await get_user_flags(uid)
    if blocked:
        return  # silently ignore

    # Rate limit basic
    if await count_last_min_msgs(uid) > RATE_LIMIT_PER_MIN:
        return

    # Persist message
    text = message.text or message.caption or ""
    content_type = "text"
    file_id: Optional[str] = None

    if message.photo:
        content_type = "photo"; file_id = message.photo[-1].file_id
    elif message.sticker:
        content_type = "sticker"; file_id = message.sticker.file_id
    elif message.video:
        content_type = "video"; file_id = message.video.file_id
    elif message.document:
        content_type = "document"; file_id = message.document.file_id
    elif message.voice:
        content_type = "voice"; file_id = message.voice.file_id

    await save_message(uid, "in", content_type, text, file_id, None, message.message_id, 0)

    # Auto-ack once/day
    ack_ok = True
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT last_ack_at FROM users WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
        last_ack = datetime.fromisoformat(row[0]) if row and row[0] else None
        if last_ack and datetime.now(timezone.utc) - last_ack < timedelta(hours=24):
            ack_ok = False
        if ack_ok:
            await bot.send_message(uid, ACK_TEXT)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET last_ack_at=? WHERE user_id=?", (datetime.now(timezone.utc), uid))
                await db.commit()
    except Exception:
        pass

    # If muted, don't notify owner; still store
    if muted:
        return

    # Forward user message to OWNER (copy)
    try:
        if content_type == "text":
            forwarded = await bot.send_message(OWNER_ID, text)
        elif content_type == "photo":
            forwarded = await bot.send_photo(OWNER_ID, file_id, caption=text or None)
        elif content_type == "video":
            forwarded = await bot.send_video(OWNER_ID, file_id, caption=text or None)
        elif content_type == "document":
            forwarded = await bot.send_document(OWNER_ID, file_id, caption=text or None)
        elif content_type == "sticker":
            forwarded = await bot.send_sticker(OWNER_ID, file_id)
        elif content_type == "voice":
            forwarded = await bot.send_voice(OWNER_ID, file_id, caption=text or None)
        else:
            forwarded = await bot.send_message(OWNER_ID, f"[{content_type}]")
        admin_msg_id = forwarded.message_id
    except Exception:
        admin_msg_id = None

    # Save admin mapping
    await save_message(uid, "in", content_type, text, file_id, admin_msg_id, message.message_id, 0)

    # Send control card WITHOUT repeating the text
    name = (message.from_user.full_name or "").strip() or message.from_user.username or str(uid)
    uname = message.from_user.username or ""
    header = f"👤 <b>{hesc(name)}</b> (@{uname or '—'}) • ID: <code>{uid}</code>"
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Reply", callback_data=f"chatreply|{uid}")
    kb.button(text="⚡ Quick Replies", callback_data=f"chatreply|{uid}")
    kb.button(text="ℹ️ Info", callback_data=f"openinfo|{uid}")
    kb.button(text="📝 Note", callback_data=f"chatnote|{uid}|0|last|0")
    kb.button(text="⭐ Fav", callback_data=f"chatfav|{uid}|0|last|0")
    kb.button(text="🚫 Block", callback_data=f"chatblk|{uid}|0|last|0")
    kb.button(text="✅ Mark read", callback_data=f"chatmr|{uid}|0|last|0")
    kb.adjust(2,2,3)
    try:
        await bot.send_message(OWNER_ID, header, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ---------------- MISC ----------------
@cb.callback_query(F.data.startswith("openinfo|"))
async def open_info(call: CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    _, uid = call.data.split("|")
    uid = int(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username,full_name,alias,tags,note,favorite,is_blocked,archived,muted FROM users WHERE user_id=?",
            (uid,)
        ) as cur:
            r = await cur.fetchone()
    if not r:
        return await call.answer("No info.", show_alert=True)
    username, full_name, alias, tags, note, fav, blk, arc, mut = r
    txt = (
        f"<b>User</b>: {hesc(alias or full_name or username or str(uid))} (@{username or '—'}) • <code>{uid}</code>\n"
        f"⭐ Fav: {bool(fav)} • 🚫 Blocked: {bool(blk)} • 📦 Archived: {bool(arc)} • 🔕 Muted: {bool(mut)}\n"
        f"🏷️ Tags: {hesc(tags or '-')}\n"
        f"📝 Note: {hesc(note or '-')}"
    )
    await call.message.answer(txt, parse_mode=ParseMode.HTML)
    await call.answer()

# ---------------- MAIN ----------------
async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.include_router(home)
    dp.include_router(admin)
    dp.include_router(cb)
    dp.include_router(public)

    print("Bot started. Owner:", OWNER_ID)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
