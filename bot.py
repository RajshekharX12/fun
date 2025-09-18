# bot.py
# PM Bot (aiogram v3) — Inbox (paged), Profile, History (paged, monospaced), Reply, Delete&Block, Reactions
# Requirements: aiogram>=3.15, aiosqlite, python-dotenv
# ENV: BOT_TOKEN=...   (Owner auto-claimed by first /start)

import asyncio
import os
import time
from datetime import datetime
import html
from typing import List, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)

from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

DB_PATH = "pm_bot.db"
PAGE_SIZE = 10              # users per inbox page
HISTORY_PAGE_SIZE = 10      # messages per history page
RECENT_SNIPPETS = 0         # 0 = don't expose previews in inbox/profile header

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment (.env)")

from aiogram.client.default import DefaultBotProperties

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

dp = Dispatcher()
r = Router()
dp.include_router(r)

# ------------------------- DB ------------------------- #
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                unread_count INTEGER DEFAULT 0,
                last_message TEXT,
                last_message_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,  -- 'in' or 'out'
                mtype TEXT NOT NULL,      -- 'text','sticker','photo','video','voice','document','animation','other'
                content TEXT,             -- text/caption or file_id note
                ts INTEGER NOT NULL
            )
        """)
        await db.commit()

async def get_owner_id() -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key='owner_id'") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None

async def set_owner_id(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('owner_id', ?)", (str(uid),))
        await db.commit()

async def is_owner(uid: int) -> bool:
    owner = await get_owner_id()
    return owner is not None and uid == owner

async def is_blocked(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM blocked WHERE user_id=?", (uid,)) as cur:
            return (await cur.fetchone()) is not None

async def block_and_delete_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO blocked(user_id) VALUES (?)", (uid,))
        await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        await db.commit()

async def upsert_user_and_increment_unread(uid: int, username: Optional[str], first: Optional[str],
                                           last: Optional[str], last_message: str):
    now_ts = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id, username, first_name, last_name, unread_count, last_message, last_message_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                unread_count=users.unread_count + 1,
                last_message=excluded.last_message,
                last_message_at=excluded.last_message_at
        """, (uid, username, first, last, 1, last_message, now_ts))
        await db.commit()

async def mark_read(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET unread_count=0 WHERE user_id=?", (uid,))
        await db.commit()

async def insert_message(uid: int, direction: str, mtype: str, content: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages(user_id, direction, mtype, content, ts) VALUES(?,?,?,?,?)",
            (uid, direction, mtype, content, int(time.time()))
        )
        await db.commit()

async def fetch_inbox_page(page: int) -> Tuple[List[Tuple[int, str, str, str, int, str, int]], int]:
    if page < 1:
        page = 1
    offset = (page - 1) * PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total = (await cur.fetchone())[0]
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        async with db.execute("""
            SELECT user_id, username, first_name, last_name, unread_count, last_message, last_message_at
            FROM users
            ORDER BY last_message_at DESC
            LIMIT ? OFFSET ?
        """, (PAGE_SIZE, offset)) as cur:
            rows = await cur.fetchall()
    return rows, total_pages

async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, username, first_name, last_name, unread_count, last_message, last_message_at
            FROM users WHERE user_id=?
        """, (uid,)) as cur:
            return await cur.fetchone()

async def fetch_history(uid: int, page: int) -> Tuple[List[Tuple[int, str, str, str, int]], int]:
    """Return messages of a user newest first, and total pages."""
    if page < 1:
        page = 1
    offset = (page - 1) * HISTORY_PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (uid,)) as cur:
            total = (await cur.fetchone())[0]
        total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        async with db.execute("""
            SELECT id, direction, mtype, content, ts
            FROM messages
            WHERE user_id=?
            ORDER BY ts DESC
            LIMIT ? OFFSET ?
        """, (uid, HISTORY_PAGE_SIZE, offset)) as cur:
            rows = await cur.fetchall()
    return rows, total_pages

# ------------------------- UI Helpers ------------------------- #
def name_of(username: Optional[str], first: Optional[str], last: Optional[str], uid: int) -> str:
    if username:
        # safe HTML
        return f"@{html.escape(username)}"
    # clickable mention if no username
    display = (first or "") + (" " + last if last else "")
    display = display.strip() or str(uid)
    return f'<a href="tg://user?id={uid}">{html.escape(display)}</a>'

def inbox_keyboard(page: int, total_pages: int, rows) -> InlineKeyboardMarkup:
    buttons = []
    for (uid, username, first, last, unread, _, _) in rows:
        title = f"{name_of(username, first, last, uid)}"
        if unread > 0:
            title += f" • {unread} msgs"
        buttons.append([InlineKeyboardButton(text=strip_html(title), callback_data=f"open:{uid}:{page}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"inbox:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"Page {page}/{total_pages}", callback_data=f"inbox:{page}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"inbox:{page+1}"))

    # Always append Inbox button row? (here it's the inbox itself, skip)
    return InlineKeyboardMarkup(inline_keyboard=buttons + [nav])

def profile_keyboard(uid: int, page_back: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Reply", callback_data=f"reply:{uid}:{page_back}"),
            InlineKeyboardButton(text="📜 History", callback_data=f"hist:{uid}:1"),
        ],
        [InlineKeyboardButton(text="🗑️ Delete & Block", callback_data=f"block:{uid}:{page_back}")],
        [InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data=f"inbox:{page_back}")],
    ])

def with_inbox_button(kb: Optional[InlineKeyboardMarkup] = None, target_page: int = 1) -> InlineKeyboardMarkup:
    """Append a universal 📥 Inbox button at the bottom for the owner."""
    if kb is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[])
    kb.inline_keyboard.append([InlineKeyboardButton(text="📥 Inbox", callback_data=f"inbox:{target_page}")])
    return kb

def strip_html(text: str) -> str:
    # For button labels we need plain text
    return html.unescape(html.escape(text, quote=False))

def safe_react(chat_id: int, message_id: int, emoji: str = "👀", is_big: bool = False):
    async def _do():
        try:
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
                is_big=is_big
            )
        except Exception:
            pass
    return asyncio.create_task(_do())

def summarize_last_message(msg: Message) -> str:
    if msg.sticker:
        return "Sticker"
    if msg.photo:
        return "Photo"
    if msg.animation:
        return "Animation"
    if msg.video:
        return "Video"
    if msg.voice:
        return "Voice"
    if msg.document:
        return "Document"
    text = (msg.text or msg.caption or "").strip()
    return text[:120] if text else "Message"

def format_history_rows(rows) -> str:
    # Newest first; render as <pre> monospaced
    lines: List[str] = []
    for (_id, direction, mtype, content, ts) in rows:
        when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        who = "Me" if direction == "out" else "User"
        body = content or ""
        if not body:
            body = f"[{mtype.capitalize()}]"
        # Escape HTML inside pre
        lines.append(f"{when}  {who}: {body}")
    body = html.escape("\n".join(lines))
    return f"<b>History</b>\n<pre>{body}</pre>"

# ------------------------- FSM ------------------------- #
class ReplyFlow(StatesGroup):
    waiting = State()

# ------------------------- Commands ------------------------- #
@r.message(CommandStart())
async def cmd_start(message: Message):
    owner_before = await get_owner_id()
    if owner_before is None:
        await set_owner_id(message.from_user.id)
        await message.answer("✅ You are set as the owner.", reply_markup=with_inbox_button())
        return

    if await is_owner(message.from_user.id):
        await message.answer("Hi Boss. Use /inbox anytime.", reply_markup=with_inbox_button())
    else:
        await message.answer("Hello! Send your message here and the admin will reply.")

@r.message(Command("inbox"))
async def cmd_inbox(message: Message):
    if not await is_owner(message.from_user.id):
        return
    page = 1
    if message.text:
        parts = message.text.strip().split()
        if len(parts) >= 2 and parts[1].isdigit():
            page = max(1, int(parts[1]))
    rows, total = await fetch_inbox_page(page)
    if not rows:
        await message.answer("📭 Inbox is empty.", reply_markup=with_inbox_button(target_page=page))
        return

    # Clean list — no previews
    header = "📥 <b>Inbox</b>"
    items = []
    for (uid, username, first, last, unread, _last_msg, _ts) in rows:
        nm = name_of(username, first, last, uid)
        badge = f" — {unread} msgs" if unread else ""
        items.append(f"• {nm}{badge}")
    await message.answer(
        f"{header}\n\n" + "\n".join(items),
        reply_markup=with_inbox_button(inbox_keyboard(page, total, rows), target_page=page)
    )

@r.message(Command("open"))
async def cmd_open(message: Message):
    if not await is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Usage: /open &lt;user_id&gt;", reply_markup=with_inbox_button())
        return
    uid = int(parts[1])
    await show_profile(message.chat.id, uid, page_back=1)

# ------------------------- Profile & History Views ------------------------- #
async def show_profile(chat_id: int, uid: int, page_back: int):
    row = await get_user(uid)
    if not row:
        await bot.send_message(chat_id, "User not found.", reply_markup=with_inbox_button())
        return
    await mark_read(uid)
    (_uid, username, first, last, unread, last_message, last_ts) = row
    nm = name_of(username, first, last, uid)
    lines = [f"👤 <b>{nm}</b> (<code>{uid}</code>)"]
    lines.append(f"Unread: {unread}")
    if RECENT_SNIPPETS and last_message:
        lines.append(f"Last: <i>{html.escape(last_message)}</i>")
    await bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=with_inbox_button(profile_keyboard(uid, page_back), target_page=page_back)
    )

async def show_history(message_or_cb, uid: int, page: int):
    rows, total = await fetch_history(uid, page)
    if not rows:
        text = "<b>History</b>\n(no messages yet)"
    else:
        text = format_history_rows(rows)
    kb_rows = []
    if page > 1:
        kb_rows.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"hist:{uid}:{page-1}"))
    kb_rows.append(InlineKeyboardButton(text=f"Page {page}/{total}", callback_data=f"hist:{uid}:{page}"))
    if page < total:
        kb_rows.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"hist:{uid}:{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[kb_rows, [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data=f"open:{uid}:1")]])
    kb = with_inbox_button(kb)
    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.edit_text(text, reply_markup=kb)
        await message_or_cb.answer()
    else:
        await message_or_cb.answer(text, reply_markup=kb)

# ------------------------- Callbacks ------------------------- #
@r.callback_query(F.data.startswith("inbox:"))
async def cb_inbox(callback: CallbackQuery):
    if not await is_owner(callback.from_user.id):
        return await callback.answer()
    page = int(callback.data.split(":")[1])
    rows, total = await fetch_inbox_page(page)
    if not rows:
        await callback.message.edit_text("📭 Inbox is empty.", reply_markup=with_inbox_button(target_page=page))
        return await callback.answer()
    header = "📥 <b>Inbox</b>"
    items = []
    for (uid, username, first, last, unread, _last_msg, _ts) in rows:
        nm = name_of(username, first, last, uid)
        badge = f" — {unread} msgs" if unread else ""
        items.append(f"• {nm}{badge}")
    await callback.message.edit_text(
        f"{header}\n\n" + "\n".join(items),
        reply_markup=with_inbox_button(inbox_keyboard(page, total, rows), target_page=page)
    )
    await callback.answer()

@r.callback_query(F.data.startswith("open:"))
async def cb_open(callback: CallbackQuery):
    if not await is_owner(callback.from_user.id):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    await show_profile(callback.message.chat.id, int(uid_str), page_back=int(page_str))
    await callback.answer()

@r.callback_query(F.data.startswith("block:"))
async def cb_block(callback: CallbackQuery):
    if not await is_owner(callback.from_user.id):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    uid = int(uid_str)
    page = int(page_str)
    await block_and_delete_user(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data=f"inbox:{page}")]])
    await callback.message.edit_text(f"🚫 User <code>{uid}</code> deleted and blocked.", reply_markup=with_inbox_button(kb, target_page=page))
    await callback.answer("Blocked.")

@r.callback_query(F.data.startswith("reply:"))
async def cb_reply(callback: CallbackQuery, state: FSMContext):
    if not await is_owner(callback.from_user.id):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    await state.update_data(target_uid=int(uid_str), return_page=int(page_str))
    await state.set_state(ReplyFlow.waiting)
    await callback.message.answer("✍️ Reply mode ON.\nSend text/sticker/photo/video/voice/document.\nUse /cancel to exit.",
                                  reply_markup=with_inbox_button())
    await callback.answer("Reply mode")

@r.callback_query(F.data.startswith("hist:"))
async def cb_hist(callback: CallbackQuery):
    if not await is_owner(callback.from_user.id):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    await show_history(callback, int(uid_str), int(page_str))

@r.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not await is_owner(message.from_user.id):
        return
    if await state.get_state() is None:
        return
    data = await state.get_data()
    page = data.get("return_page", 1)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data=f"inbox:{page}")]])
    await message.answer("❎ Reply cancelled.", reply_markup=with_inbox_button(kb, target_page=page))

# ------------------------- Owner Reply ------------------------- #
@r.message(ReplyFlow.waiting)
async def owner_send_reply(message: Message, state: FSMContext):
    if not await is_owner(message.from_user.id):
        return
    data = await state.get_data()
    target = data.get("target_uid")
    if not target:
        await message.answer("No target set. /cancel and try again.", reply_markup=with_inbox_button())
        return

    sent = None
    mtype = "text"
    content_for_log = None
    try:
        if message.sticker:
            mtype, content_for_log = "sticker", message.sticker.file_id
            sent = await bot.send_sticker(chat_id=target, sticker=message.sticker.file_id)
        elif message.photo:
            mtype, content_for_log = "photo", message.photo[-1].file_id
            sent = await bot.send_photo(chat_id=target, photo=message.photo[-1].file_id, caption=message.caption)
        elif message.animation:
            mtype, content_for_log = "animation", message.animation.file_id
            sent = await bot.send_animation(chat_id=target, animation=message.animation.file_id, caption=message.caption)
        elif message.video:
            mtype, content_for_log = "video", message.video.file_id
            sent = await bot.send_video(chat_id=target, video=message.video.file_id, caption=message.caption)
        elif message.voice:
            mtype, content_for_log = "voice", message.voice.file_id
            sent = await bot.send_voice(chat_id=target, voice=message.voice.file_id, caption=message.caption)
        elif message.document:
            mtype, content_for_log = "document", message.document.file_id
            sent = await bot.send_document(chat_id=target, document=message.document.file_id, caption=message.caption)
        else:
            mtype, content_for_log = "text", (message.text or "(empty)")
            sent = await bot.send_message(chat_id=target, text=message.text or "(empty)")

        # Log our outgoing message
        await insert_message(target, "out", mtype, content_for_log if mtype != "text" else (message.text or "(empty)"))

        if sent:
            safe_react(target, sent.message_id, emoji="✅", is_big=False)

        await message.answer("✅ Sent.", reply_markup=with_inbox_button())
    except Exception as e:
        await message.answer(f"⚠️ Failed to send: <code>{html.escape(repr(e))}</code>", reply_markup=with_inbox_button())


# ------------------------- Inbound from users ------------------------- #
@r.message(F.chat.type == "private")
async def inbound_from_user(message: Message):
    owner = await get_owner_id()
    uid = message.from_user.id

    # If owner sends a DM to the bot (outside reply flow), ignore this inbound path
    if owner and uid == owner:
        return

    if await is_blocked(uid):
        return  # silent

    # Log full message
    summary = summarize_last_message(message)
    mtype = (
        "sticker" if message.sticker else
        "photo" if message.photo else
        "animation" if message.animation else
        "video" if message.video else
        "voice" if message.voice else
        "document" if message.document else
        "text"
    )
    content_for_log = (
        message.text or message.caption or
        (message.sticker.file_id if message.sticker else None) or
        (message.photo[-1].file_id if message.photo else None) or
        (message.animation.file_id if message.animation else None) or
        (message.video.file_id if message.video else None) or
        (message.voice.file_id if message.voice else None) or
        (message.document.file_id if message.document else None)
    )
    await insert_message(uid, "in", mtype, content_for_log)

    # Update inbox row
    await upsert_user_and_increment_unread(
        uid=uid,
        username=message.from_user.username,
        first=message.from_user.first_name,
        last=message.from_user.last_name,
        last_message=summary,
    )

    # React in user's chat
    safe_react(message.chat.id, message.message_id, emoji="👀", is_big=False)

    # Notify owner
    if owner:
        nm = name_of(message.from_user.username, message.from_user.first_name, message.from_user.last_name, uid)
        open_btn = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Open {strip_html(nm)}", callback_data=f"open:{uid}:1")],
            [InlineKeyboardButton(text="📥 Inbox", callback_data="inbox:1")]
        ])
        preview = html.escape(summary)
        try:
            await bot.send_message(owner, f"🔔 <b>New message</b> from {nm} (<code>{uid}</code>)\n<i>{preview}</i>",
                                   reply_markup=open_btn)
        except Exception:
            pass

# ------------------------- Optional reactions observer ------------------------- #
try:
    @r.message_reaction()
    async def on_reaction(update):
        pass
except Exception:
    pass

# ------------------------- Boot ------------------------- #
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
