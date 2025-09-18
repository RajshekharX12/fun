# bot.py
# PM Bot (aiogram v3) — inbox with pagination, reply, delete+block, reactions support
# Requirements: aiogram>=3.15, aiosqlite, python-dotenv
# ENV: BOT_TOKEN=...  (Owner auto-claimed by first /start)

import asyncio
import os
import time
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
PAGE_SIZE = 10

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment (.env)")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
r = Router()
dp.include_router(r)


# ------------------------- DB LAYER ------------------------- #
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
        await db.commit()


async def get_owner_id() -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key='owner_id'") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def set_owner_id(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('owner_id', ?)",
            (str(uid),),
        )
        await db.commit()


async def is_blocked(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM blocked WHERE user_id=?", (uid,)) as cur:
            return (await cur.fetchone()) is not None


async def block_and_delete_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO blocked(user_id) VALUES (?)", (uid,))
        await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.commit()


async def upsert_user_and_increment_unread(
    uid: int, username: Optional[str], first_name: Optional[str],
    last_name: Optional[str], last_message: str
):
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
        """, (uid, username, first_name, last_name, 1, last_message, now_ts))
        await db.commit()


async def mark_read(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET unread_count=0 WHERE user_id=?", (uid,))
        await db.commit()


async def fetch_inbox_page(page: int) -> Tuple[List[Tuple[int, str, str, str, int, str, int]], int]:
    """
    Returns (rows, total_pages)
    rows columns: user_id, username, first_name, last_name, unread_count, last_message, last_message_at
    """
    if page < 1:
        page = 1
    offset = (page - 1) * PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        # Count total
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


# ------------------------- UTIL & UI ------------------------- #
async def claim_owner_if_empty(message: Message) -> int:
    owner = await get_owner_id()
    if owner is None:
        await set_owner_id(message.from_user.id)
        return message.from_user.id
    return owner


async def require_owner(message: Message) -> bool:
    owner = await get_owner_id()
    return owner is not None and message.from_user.id == owner


def name_of(username: Optional[str], first: Optional[str], last: Optional[str], uid: int) -> str:
    if username:
        return f"@{username}"
    base = (first or "") + (" " + last if last else "")
    base = base.strip() or str(uid)
    return base


def inbox_keyboard(page: int, total_pages: int, rows) -> InlineKeyboardMarkup:
    buttons = []
    for (uid, username, first, last, unread, _, _) in rows:
        title = f"{name_of(username, first, last, uid)}"
        if unread > 0:
            title += f" • {unread} msgs"
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"open:{uid}:{page}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"inbox:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"Page {page}/{total_pages}", callback_data=f"inbox:{page}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"inbox:{page+1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=buttons + [nav])
    return kb


def chat_keyboard(uid: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Reply", callback_data=f"reply:{uid}:{page}"),
            InlineKeyboardButton(text="🗑️ Delete & Block", callback_data=f"block:{uid}:{page}"),
        ],
        [InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data=f"inbox:{page}")]
    ])


async def safe_react(chat_id: int, message_id: int, emoji: str = "👀", is_big: bool = False):
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
            is_big=is_big
        )
    except Exception:
        # Reactions can fail if disabled in chat or restricted; ignore silently
        pass


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
        return "Voice message"
    if msg.document:
        return "Document"
    text = (msg.text or msg.caption or "").strip()
    return text[:120] if text else "Message"


# ------------------------- FSM ------------------------- #
class ReplyFlow(StatesGroup):
    waiting = State()  # admin is typing the reply


# ------------------------- OWNER COMMANDS ------------------------- #
@r.message(CommandStart())
async def cmd_start(message: Message):
    owner_before = await get_owner_id()
    owner = await claim_owner_if_empty(message)
    if message.from_user.id == owner and owner_before is None:
        await message.answer("✅ You are set as the owner. Use /inbox to manage messages.")
    elif message.from_user.id == owner:
        await message.answer("Hi Boss. Use /inbox anytime.")
    else:
        await message.answer("Hello! Send your message here, the admin will get back to you.")


@r.message(Command("inbox"))
async def cmd_inbox(message: Message):
    if not await require_owner(message):
        return
    # Optional page arg: /inbox 2
    page = 1
    if message.text:
        parts = message.text.strip().split()
        if len(parts) >= 2 and parts[1].isdigit():
            page = max(1, int(parts[1]))
    rows, total = await fetch_inbox_page(page)
    if not rows:
        await message.answer("📭 Inbox is empty.")
        return
    text_lines = ["📥 *Inbox*"]
    for (uid, username, first, last, unread, last_msg, _) in rows:
        nm = name_of(username, first, last, uid)
        badge = f" — {unread} msgs" if unread else ""
        snippet = (last_msg or "")
        text_lines.append(f"• {nm}{badge}\n    _{snippet}_")
    await message.answer(
        "\n".join(text_lines),
        reply_markup=inbox_keyboard(page, total, rows),
        parse_mode="Markdown"
    )


@r.message(Command("open"))
async def cmd_open(message: Message):
    if not await require_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Usage: /open <user_id>")
        return
    uid = int(parts[1])
    row = await get_user(uid)
    if not row:
        await message.answer("User not found in inbox.")
        return
    await mark_read(uid)
    (_, username, first, last, unread, last_message, last_ts) = row
    nm = name_of(username, first, last, uid)
    page = 1
    txt = (f"👤 *{nm}* (`{uid}`)\n"
           f"Unread: {unread}\n"
           f"Last: _{last_message or '—'}_\n")
    await message.answer(
        txt, parse_mode="Markdown", reply_markup=chat_keyboard(uid, page)
    )


# ------------------------- CALLBACKS (Inbox & Chat) ------------------------- #
@r.callback_query(F.data.startswith("inbox:"))
async def cb_inbox(callback: CallbackQuery):
    if not await require_owner(callback.message):
        return await callback.answer()
    page = int(callback.data.split(":")[1])
    rows, total = await fetch_inbox_page(page)
    if not rows:
        await callback.message.edit_text("📭 Inbox is empty.")
        return await callback.answer()
    text_lines = ["📥 *Inbox*"]
    for (uid, username, first, last, unread, last_msg, _) in rows:
        nm = name_of(username, first, last, uid)
        badge = f" — {unread} msgs" if unread else ""
        text_lines.append(f"• {nm}{badge}\n    _{(last_msg or '')}_")
    await callback.message.edit_text(
        "\n".join(text_lines),
        reply_markup=inbox_keyboard(page, total, rows),
        parse_mode="Markdown"
    )
    await callback.answer()


@r.callback_query(F.data.startswith("open:"))
async def cb_open(callback: CallbackQuery):
    if not await require_owner(callback.message):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    uid = int(uid_str)
    page = int(page_str)
    row = await get_user(uid)
    if not row:
        await callback.answer("User removed.")
        return
    await mark_read(uid)
    (_, username, first, last, unread, last_message, last_ts) = row
    nm = name_of(username, first, last, uid)
    txt = (f"👤 *{nm}* (`{uid}`)\n"
           f"Unread: {unread}\n"
           f"Last: _{last_message or '—'}_\n")
    await callback.message.edit_text(
        txt, parse_mode="Markdown", reply_markup=chat_keyboard(uid, page)
    )
    await callback.answer()


@r.callback_query(F.data.startswith("block:"))
async def cb_block(callback: CallbackQuery):
    if not await require_owner(callback.message):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    uid = int(uid_str)
    page = int(page_str)
    await block_and_delete_user(uid)
    await callback.message.edit_text(
        f"🚫 User `{uid}` deleted from inbox and blocked.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data=f"inbox:{page}")]
        ])
    )
    await callback.answer("Blocked.")


@r.callback_query(F.data.startswith("reply:"))
async def cb_reply(callback: CallbackQuery, state: FSMContext):
    if not await require_owner(callback.message):
        return await callback.answer()
    _, uid_str, page_str = callback.data.split(":")
    uid = int(uid_str)
    await state.update_data(target_uid=uid, return_page=int(page_str))
    await state.set_state(ReplyFlow.waiting)
    await callback.message.answer(
        f"✍️ Reply mode for `{uid}`.\nSend *text* or *sticker* (photo/video/voice also supported).\nUse /cancel to exit.",
        parse_mode="Markdown"
    )
    await callback.answer("Reply mode on.")


@r.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not await require_owner(message):
        return
    if await state.get_state() is None:
        return
    data = await state.get_data()
    page = data.get("return_page", 1)
    await state.clear()
    await message.answer("❎ Reply cancelled.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data=f"inbox:{page}")]]
    ))


# ------------------------- OWNER REPLY HANDLER ------------------------- #
@r.message(ReplyFlow.waiting)
async def owner_send_reply(message: Message, state: FSMContext):
    if not await require_owner(message):
        return
    data = await state.get_data()
    target = data.get("target_uid")
    if not target:
        await message.answer("No target set. /cancel and try again.")
        return

    sent = None
    try:
        if message.sticker:
            sent = await bot.send_sticker(chat_id=target, sticker=message.sticker.file_id)
        elif message.photo:
            sent = await bot.send_photo(chat_id=target, photo=message.photo[-1].file_id, caption=message.caption)
        elif message.animation:
            sent = await bot.send_animation(chat_id=target, animation=message.animation.file_id, caption=message.caption)
        elif message.video:
            sent = await bot.send_video(chat_id=target, video=message.video.file_id, caption=message.caption)
        elif message.voice:
            sent = await bot.send_voice(chat_id=target, voice=message.voice.file_id, caption=message.caption)
        elif message.document:
            sent = await bot.send_document(chat_id=target, document=message.document.file_id, caption=message.caption)
        else:
            sent = await bot.send_message(chat_id=target, text=message.text or "(empty)")

        # small reaction to our own sent message (in recipient chat)
        if sent:
            await safe_react(chat_id=target, message_id=sent.message_id, emoji="✅", is_big=False)

        await message.answer("✅ Sent.")
    except Exception as e:
        await message.answer(f"⚠️ Failed to send: {e!r}")


# ------------------------- USER INBOUND ------------------------- #
@r.message(F.chat.type == "private")
async def inbound_from_user(message: Message):
    owner = await get_owner_id()
    uid = message.from_user.id

    # If owner writes here and not in reply flow, ignore as regular user path
    if owner and uid == owner:
        return

    if await is_blocked(uid):
        # Silently ignore blocked users
        return

    # Store/Update inbox row
    await upsert_user_and_increment_unread(
        uid=uid,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        last_message=summarize_last_message(message),
    )

    # Auto-ack reaction in user's chat (optional)
    await safe_react(chat_id=message.chat.id, message_id=message.message_id, emoji="👀", is_big=False)

    # Notify owner
    owner = await get_owner_id()
    if owner:
        nm = name_of(message.from_user.username, message.from_user.first_name, message.from_user.last_name, uid)
        open_btn = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Open {nm}", callback_data=f"open:{uid}:1")],
            [InlineKeyboardButton(text="📥 Inbox", callback_data="inbox:1")]
        ])
        preview = summarize_last_message(message)
        try:
            await bot.send_message(
                owner,
                f"🔔 New message from *{nm}* (`{uid}`)\n_{preview}_",
                parse_mode="Markdown",
                reply_markup=open_btn
            )
        except Exception:
            # If owner hasn't started the bot yet, we can't DM them; ignore.
            pass


# ------------------------- REACTIONS EVENTS (optional) ------------------------- #
# On modern aiogram (>=3.4), this observer exists. If not, it's just ignored by the runtime.
try:
    @r.message_reaction()
    async def on_reaction(update):
        # This will trigger when users react to the bot's messages where supported.
        # We don't need to do anything heavy — just ignore or log if you want.
        # Example of accessing fields:
        # chat = update.chat
        # msg_id = update.message_id
        # new_reaction = update.new_reaction
        pass
except Exception:
    # Older aiogram without reaction observer — safe to skip.
    pass


# ------------------------- BOOT ------------------------- #
async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
