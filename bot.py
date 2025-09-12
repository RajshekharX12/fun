# bot.py
# Unbeatable Tic-Tac-Toe (aiogram 3.7) with Minimax, difficulty levels, stats, and leaderboards.
# Features:
# - Inline 3x3 keyboard, Human = X, Bot = O
# - Minimax AI (Hard), Medium (50% optimal), Easy (random)
# - "🤖 Bot starts", "💡 Hint", "⚙️ Difficulty", "↻ New game", "🏁 Resign"
# - /start, /help, /newgame, /stats, /leaderboard, /ping, /version
# - Group-safe: only the user who started a game can play on that board
# - SQLite persistence with WAL & busy_timeout
# - Clean, defensive error handling; compatible with aiogram 3.7+

import asyncio
import logging
import os
import random
import sqlite3
import sys
import time
from contextlib import closing
from typing import List, Optional, Tuple

try:
    import uvloop  # optional but nice
    uvloop.install()
except Exception:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram import __version__ as AIOGRAM_VERSION
from aiogram.exceptions import TelegramBadRequest

# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("8401266233:AAGkm2s9GOg07Rh8ayZ4PTlc5PRFyh0LzaY", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN environment variable before running the bot.")

DB_PATH = os.getenv("DB_PATH", "tictactoe.db")

# Difficulty levels
EASY = "EASY"
MEDIUM = "MEDIUM"
HARD = "HARD"
VALID_LEVELS = (EASY, MEDIUM, HARD)
DEFAULT_LEVEL = HARD

# Game constants
HUMAN = "X"
BOT_P = "O"
EMPTY = " "

# -----------------------------
# DB
# -----------------------------
def db_conn():
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=15000;")
    return con

def db_init():
    with closing(db_conn()) as con, con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            gid TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            board TEXT NOT NULL,           -- nine chars: ' ', 'X', 'O'
            turn TEXT NOT NULL,            -- 'X' or 'O'
            status TEXT NOT NULL,          -- 'PLAY','XWIN','OWIN','DRAW'
            difficulty TEXT NOT NULL,      -- EASY/MEDIUM/HARD
            started_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_games_chat_msg ON games(chat_id, message_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_games_user ON games(user_id)")

        con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            x_wins INTEGER NOT NULL DEFAULT 0,   -- human wins
            o_wins INTEGER NOT NULL DEFAULT 0,   -- bot wins
            draws  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id INTEGER PRIMARY KEY,
            difficulty TEXT NOT NULL DEFAULT 'HARD'
        )
        """)

def db_get_game(gid: str) -> Optional[tuple]:
    with closing(db_conn()) as con, con:
        cur = con.execute("SELECT gid,chat_id,message_id,user_id,board,turn,status,difficulty,started_at,updated_at FROM games WHERE gid=?", (gid,))
        return cur.fetchone()

def db_upsert_game(gid: str, chat_id: int, message_id: int, user_id: int, board: str, turn: str, status: str, difficulty: str):
    now = int(time.time())
    with closing(db_conn()) as con, con:
        con.execute("""
        INSERT INTO games (gid,chat_id,message_id,user_id,board,turn,status,difficulty,started_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(gid) DO UPDATE SET
            chat_id=excluded.chat_id,
            message_id=excluded.message_id,
            user_id=excluded.user_id,
            board=excluded.board,
            turn=excluded.turn,
            status=excluded.status,
            difficulty=excluded.difficulty,
            updated_at=excluded.updated_at
        """, (gid, chat_id, message_id, user_id, board, turn, status, difficulty, now, now))

def db_delete_game(gid: str):
    with closing(db_conn()) as con, con:
        con.execute("DELETE FROM games WHERE gid=?", (gid,))

def db_inc_stat(chat_id: int, user_id: int, result: str):
    # result in 'XWIN','OWIN','DRAW'
    with closing(db_conn()) as con, con:
        con.execute("""
        INSERT INTO stats (chat_id,user_id,x_wins,o_wins,draws)
        VALUES (?,?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO NOTHING
        """, (chat_id, user_id, 0, 0, 0))
        if result == "XWIN":
            con.execute("UPDATE stats SET x_wins = x_wins + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        elif result == "OWIN":
            con.execute("UPDATE stats SET o_wins = o_wins + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        else:
            con.execute("UPDATE stats SET draws  = draws  + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))

def db_get_stats(chat_id: int, user_id: int) -> tuple:
    with closing(db_conn()) as con, con:
        cur = con.execute("SELECT x_wins,o_wins,draws FROM stats WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        return row if row else (0, 0, 0)

def db_get_leaderboard(chat_id: int, limit: int = 10) -> List[tuple]:
    with closing(db_conn()) as con, con:
        cur = con.execute("""
        SELECT user_id, x_wins, o_wins, draws, (x_wins) AS score
        FROM stats
        WHERE chat_id=?
        ORDER BY score DESC, draws DESC
        LIMIT ?
        """, (chat_id, limit))
        return list(cur.fetchall())

def db_get_pref(user_id: int) -> str:
    with closing(db_conn()) as con, con:
        cur = con.execute("SELECT difficulty FROM user_prefs WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] in VALID_LEVELS else DEFAULT_LEVEL

def db_set_pref(user_id: int, difficulty: str):
    if difficulty not in VALID_LEVELS:
        return
    with closing(db_conn()) as con, con:
        con.execute("""
        INSERT INTO user_prefs (user_id, difficulty)
        VALUES (?,?)
        ON CONFLICT(user_id) DO UPDATE SET difficulty=excluded.difficulty
        """, (user_id, difficulty))

# -----------------------------
# Game logic (Minimax)
# -----------------------------
def lines() -> List[Tuple[int, int, int]]:
    return [
        (0,1,2), (3,4,5), (6,7,8),  # rows
        (0,3,6), (1,4,7), (2,5,8),  # cols
        (0,4,8), (2,4,6)            # diags
    ]

def check_winner(board: str) -> Optional[str]:
    for a,b,c in lines():
        if board[a] != EMPTY and board[a] == board[b] == board[c]:
            return board[a]
    return None

def empty_cells(board: str) -> List[int]:
    return [i for i, ch in enumerate(board) if ch == EMPTY]

def is_draw(board: str) -> bool:
    return (check_winner(board) is None) and (EMPTY not in board)

def human_turn(board: str) -> bool:
    # X always starts; turn = X when counts equal
    return board.count("X") == board.count("O")

def _minimax(board: str, is_max: bool, depth: int = 0) -> int:
    # Bot (O) maximizes; Human (X) minimizes
    w = check_winner(board)
    if w == BOT_P:
        return 10 - depth
    if w == HUMAN:
        return depth - 10
    if EMPTY not in board:
        return 0

    if is_max:
        best = -10**9
        for i in empty_cells(board):
            nb = board[:i] + BOT_P + board[i+1:]
            best = max(best, _minimax(nb, False, depth + 1))
        return best
    else:
        best = 10**9
        for i in empty_cells(board):
            nb = board[:i] + HUMAN + board[i+1:]
            best = min(best, _minimax(nb, True, depth + 1))
        return best

def best_move_O(board: str) -> int:
    # Choose the move with the highest score; prefer center/corners by iteration order
    best_score = -10**9
    best_idx = -1
    # simple ordering preference: center, corners, edges
    ordering = [4,0,2,6,8,1,3,5,7]
    for i in ordering:
        if board[i] != EMPTY:
            continue
        score = _minimax(board[:i] + BOT_P + board[i+1:], False, 0)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx

def best_move_X(board: str) -> int:
    # Human's best (defensive) move: minimize score
    best_score = 10**9
    best_idx = -1
    ordering = [4,0,2,6,8,1,3,5,7]
    for i in ordering:
        if board[i] != EMPTY:
            continue
        score = _minimax(board[:i] + HUMAN + board[i+1:], True, 0)
        if score < best_score:
            best_score = score
            best_idx = i
    return best_idx

def bot_move_by_difficulty(board: str, level: str) -> int:
    empties = empty_cells(board)
    if not empties:
        return -1
    if level == EASY:
        return random.choice(empties)
    if level == MEDIUM:
        # 50% optimal, 50% random (feels human)
        return best_move_O(board) if random.random() < 0.5 else random.choice(empties)
    # HARD
    return best_move_O(board)

# -----------------------------
# UI helpers
# -----------------------------
def idx_to_rc(idx: int) -> Tuple[int, int]:
    return idx // 3, idx % 3

def fmt_cell(ch: str) -> str:
    return "❌" if ch == "X" else ("⭕️" if ch == "O" else "·")

def render_board_text(board: str) -> str:
    rows = [
        " ".join(fmt_cell(board[i]) for i in range(0,3)),
        " ".join(fmt_cell(board[i]) for i in range(3,6)),
        " ".join(fmt_cell(board[i]) for i in range(6,9)),
    ]
    return "<code>" + "\n".join(rows) + "</code>"

def render_header(status: str, difficulty: str, your_turn: bool) -> str:
    title = "<b>Tic-Tac-Toe</b> | You: ❌  Bot: ⭕️"
    diff = f"\n<b>Difficulty:</b> {difficulty}"
    if status == "PLAY":
        turn = "\nYour move." if your_turn else "\nBot moved. Your turn."
        return f"{title}{diff}{turn}"
    if status == "XWIN":
        return f"{title}{diff}\n🎉 <b>You win!</b>"
    if status == "OWIN":
        return f"{title}{diff}\n🤖 <b>Bot wins!</b>"
    return f"{title}{diff}\n🤝 <b>Draw.</b>"

def board_keyboard(gid: str, board: str, status: str, difficulty: str, at_start: bool) -> InlineKeyboardMarkup:
    grid: List[List[InlineKeyboardButton]] = []
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            ch = board[idx]
            text = fmt_cell(ch)
            if status == "PLAY" and ch == EMPTY:
                row.append(InlineKeyboardButton(text=text, callback_data=f"m:{gid}:{idx}"))
            else:
                # locked cell
                row.append(InlineKeyboardButton(text=text, callback_data=f"n:{gid}"))
        grid.append(row)

    bottom: List[InlineKeyboardButton] = []
    if status == "PLAY" and at_start and board == EMPTY * 9:
        bottom.append(InlineKeyboardButton(text="🤖 Bot starts", callback_data=f"bs:{gid}"))
    if status == "PLAY":
        bottom.append(InlineKeyboardButton(text="💡 Hint", callback_data=f"h:{gid}"))
        bottom.append(InlineKeyboardButton(text=f"⚙️ {difficulty}", callback_data=f"d:{gid}"))
    bottom.append(InlineKeyboardButton(text="↻ New game", callback_data="new"))
    if status == "PLAY":
        bottom.append(InlineKeyboardButton(text="🏁 Resign", callback_data=f"r:{gid}"))
    grid.append(bottom)

    return InlineKeyboardMarkup(inline_keyboard=grid)

def render_full_text(board: str, status: str, difficulty: str) -> str:
    return render_header(status, difficulty, your_turn=human_turn(board)) + "\n\n" + render_board_text(board)

# -----------------------------
# Bot wiring
# -----------------------------
router = Router()

@router.message(CommandStart())
async def cmd_start(m: Message):
    pref = db_get_pref(m.from_user.id)
    await m.answer(
        "Hi! I’m an <b>unbeatable</b> Tic-Tac-Toe bot.\n"
        "• You are ❌, I am ⭕️\n"
        "• I play perfectly on <b>Hard</b> (Minimax). Try <i>Medium</i> or <i>Easy</i> if you want.\n\n"
        "Use /newgame to begin, /help for commands.",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "<b>Commands</b>\n"
        "• /newgame — start a new game\n"
        "• /stats — your W/L/D in this chat\n"
        "• /leaderboard — top players in this chat (by wins)\n"
        "• /ping — quick health check\n"
        "• /version — aiogram version\n\n"
        "<b>In-game buttons</b>\n"
        "• 🤖 Bot starts — let the bot open\n"
        "• 💡 Hint — shows your best move\n"
        "• ⚙️ Difficulty — cycle or choose difficulty\n"
        "• ↻ New game — start over\n"
        "• 🏁 Resign — concede the current game",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong 🏓")

@router.message(Command("version"))
async def cmd_version(m: Message):
    await m.answer(f"aiogram {AIOGRAM_VERSION}")

@router.message(Command("newgame"))
async def cmd_newgame(m: Message, bot: Bot):
    await start_new_game(bot, chat_id=m.chat.id, user_id=m.from_user.id)

@router.message(Command("stats"))
async def cmd_stats(m: Message):
    xw, ow, dr = db_get_stats(m.chat.id, m.from_user.id)
    total = xw + ow + dr
    await m.answer(
        f"<b>Your stats here</b>\n"
        f"Wins: {xw}\nLosses: {ow}\nDraws: {dr}\nTotal: {total}",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("leaderboard"))
async def cmd_leaderboard(m: Message, bot: Bot):
    rows = db_get_leaderboard(m.chat.id, 10)
    if not rows:
        await m.answer("No games played here yet.")
        return
    lines = ["<b>Leaderboard (by wins)</b>"]
    for rank, (uid, xw, ow, dr, score) in enumerate(rows, start=1):
        try:
            u = await bot.get_chat(uid)
            name = u.full_name or (u.username and f"@{u.username}") or str(uid)
        except Exception:
            name = str(uid)
        lines.append(f"{rank}. {name} — {xw}W / {ow}L / {dr}D")
    await m.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# --- Callbacks ---

@router.callback_query(F.data == "new")
async def cb_new(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    await start_new_game(bot, chat_id=cq.message.chat.id, user_id=cq.from_user.id)

@router.callback_query(F.data.startswith("bs:"))  # Bot starts
async def cb_bot_start(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    gid = cq.data[3:]
    row = db_get_game(gid)
    if not valid_click(cq, row):
        return
    gid, chat_id, mid, uid, board, turn, status, diff, *_ = row
    if status != "PLAY" or board != EMPTY * 9:
        return
    idx = bot_move_by_difficulty(board, diff)
    if idx < 0:
        return
    board = board[:idx] + BOT_P + board[idx+1:]
    status = resolve_status(board)
    turn = HUMAN
    db_upsert_game(gid, chat_id, mid, uid, board, turn, status, diff)
    await edit_board(bot, chat_id, mid, gid, board, status, diff, at_start=False)
    if status in ("XWIN", "OWIN", "DRAW"):
        db_inc_stat(chat_id, uid, status)

@router.callback_query(F.data.startswith("m:"))  # Human move
async def cb_move(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    # m:<gid>:<idx>
    try:
        _, gid, sidx = cq.data.split(":")
        idx = int(sidx)
    except Exception:
        return

    row = db_get_game(gid)
    if not valid_click(cq, row):
        return
    gid, chat_id, mid, uid, board, turn, status, diff, *_ = row

    # Validate it's playable and human turn and cell empty
    if status != "PLAY" or not human_turn(board) or board[idx] != EMPTY:
        return

    # Human place X
    board = board[:idx] + HUMAN + board[idx+1:]
    status = resolve_status(board)
    if status != "PLAY":
        db_upsert_game(gid, chat_id, mid, uid, board, BOT_P, status, diff)
        await edit_board(bot, chat_id, mid, gid, board, status, diff, at_start=False)
        db_inc_stat(chat_id, uid, status)
        return

    # Bot responds by difficulty
    bidx = bot_move_by_difficulty(board, diff)
    if bidx >= 0:
        board = board[:bidx] + BOT_P + board[bidx+1:]

    status = resolve_status(board)
    db_upsert_game(gid, chat_id, mid, uid, board, HUMAN, status, diff)
    await edit_board(bot, chat_id, mid, gid, board, status, diff, at_start=False)
    if status in ("XWIN", "OWIN", "DRAW"):
        db_inc_stat(chat_id, uid, status)

@router.callback_query(F.data.startswith("h:"))  # Hint
async def cb_hint(cq: CallbackQuery):
    gid = cq.data[2:]
    row = db_get_game(gid)
    if not valid_click(cq, row):
        return
    _, _, _, _, board, _, status, _, *_ = row
    if status != "PLAY" or not human_turn(board):
        await cq.answer("Not your turn.", show_alert=False)
        return
    idx = best_move_X(board)
    if idx < 0:
        await cq.answer("No hint available.", show_alert=False)
        return
    r, c = idx_to_rc(idx)
    await cq.answer(f"💡 Best move: row {r+1}, col {c+1}", show_alert=False)

@router.callback_query(F.data.startswith("d:"))  # Difficulty
async def cb_diff(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    gid = cq.data[2:]
    row = db_get_game(gid)
    if not valid_click(cq, row):
        return
    gid, chat_id, mid, uid, board, turn, status, diff, *_ = row
    # Cycle difficulty for the player preference, and reflect on current game (if still PLAY)
    next_level = {EASY: MEDIUM, MEDIUM: HARD, HARD: EASY}[diff]
    db_set_pref(uid, next_level)
    # Apply to current running game too
    diff = next_level
    db_upsert_game(gid, chat_id, mid, uid, board, turn, status, diff)
    await edit_board(bot, chat_id, mid, gid, board, status, diff, at_start=(board == EMPTY*9))

@router.callback_query(F.data.startswith("r:"))  # Resign
async def cb_resign(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    gid = cq.data[2:]
    row = db_get_game(gid)
    if not valid_click(cq, row):
        return
    gid, chat_id, mid, uid, board, turn, status, diff, *_ = row
    if status != "PLAY":
        return
    # Resignation counts as bot win
    status = "OWIN"
    db_upsert_game(gid, chat_id, mid, uid, board, turn, status, diff)
    await edit_board(bot, chat_id, mid, gid, board, status, diff, at_start=False)
    db_inc_stat(chat_id, uid, status)

@router.callback_query(F.data.startswith("n:"))
async def cb_noop(cq: CallbackQuery):
    # Just clear the loader
    await cq.answer()

# -----------------------------
# Helpers
# -----------------------------
def resolve_status(board: str) -> str:
    w = check_winner(board)
    if w == HUMAN:
        return "XWIN"
    if w == BOT_P:
        return "OWIN"
    if is_draw(board):
        return "DRAW"
    return "PLAY"

def valid_click(cq: CallbackQuery, game_row: Optional[tuple]) -> bool:
    if not cq.message or not game_row:
        return False
    gid, chat_id, message_id, user_id, board, turn, status, *_ = game_row
    if cq.message.message_id != message_id:
        return False
    return cq.from_user.id == user_id

async def start_new_game(bot: Bot, chat_id: int, user_id: int):
    gid = hex(int(time.time() * 1000))[2:]
    board = EMPTY * 9
    status = "PLAY"
    turn = HUMAN
    difficulty = db_get_pref(user_id)

    text = render_full_text(board, status, difficulty)
    kb = board_keyboard(gid, board, status, difficulty, at_start=True)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    db_upsert_game(gid, chat_id, msg.message_id, user_id, board, turn, status, difficulty)

async def edit_board(bot: Bot, chat_id: int, message_id: int, gid: str, board: str, status: str, difficulty: str, at_start: bool):
    text = render_full_text(board, status, difficulty)
    kb = board_keyboard(gid, board, status, difficulty, at_start=at_start)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        # Avoid crash on "message is not modified" or old client issues
        if "message is not modified" in str(e).lower():
            return
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            # We won't rewire game message_id here to avoid mid-game confusion.
        except Exception:
            pass

# -----------------------------
# Main
# -----------------------------
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
