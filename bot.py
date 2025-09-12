# bot.py
# Tic-Tac-Toe Telegram Bot (aiogram 3.7)
# - Hard-only AI (Minimax + alpha-beta; heuristic on larger boards)
# - Sizes: 3x3 (K=3), 4x4 (K=4), 5x5 (K=4)
# - All actions via inline buttons
# - Features: size chooser, bot starts, hint, undo(1), resign, rematch, theme toggle,
#             per-size stats & leaderboard, multi-game safe, stale-game cleanup

import asyncio
import logging
import os
import random
import sqlite3
import sys
import time
from contextlib import closing
from functools import lru_cache
from typing import List, Optional, Tuple, Dict

try:
    import uvloop  # optional
    uvloop.install()
except Exception:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.exceptions import TelegramBadRequest
from aiogram import __version__ as AIOGRAM_VERSION

# -----------------------------
# CONFIG
# -----------------------------
# Hardcode token for immediate run (you can change later to env usage).
BOT_TOKEN = "8401266233:AAGkm2s9GOg07Rh8ayZ4PTlc5PRFyh0LzaY"
DB_PATH = os.getenv("DB_PATH", "tact.db")

HUMAN, BOT_P, EMPTY = "X", "O", " "

# size -> win length
WIN_K: Dict[int, int] = {3: 3, 4: 4, 5: 4}

# search depths (ply) for "hard" by size (full on 3x3; bounded with heuristic otherwise)
SEARCH_DEPTH: Dict[int, int] = {3: 9, 4: 5, 5: 4}

# stale cleanup (seconds): delete games not updated beyond this
STALE_SECS = 24 * 3600

# Themes
THEMES = ("CLASSIC", "MIN")
DEFAULT_THEME = "CLASSIC"

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
            size INTEGER NOT NULL,
            k INTEGER NOT NULL,
            board TEXT NOT NULL,
            turn TEXT NOT NULL,          -- 'X' or 'O'
            status TEXT NOT NULL,        -- 'PLAY','XWIN','OWIN','DRAW'
            theme TEXT NOT NULL,
            undo_left INTEGER NOT NULL,  -- 1 per game for human
            hist TEXT NOT NULL,          -- comma-separated move indices
            bot_started INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_games_msg ON games(chat_id, message_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_games_user ON games(user_id)")
        con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            size INTEGER NOT NULL,
            x_wins INTEGER NOT NULL DEFAULT 0,
            o_wins INTEGER NOT NULL DEFAULT 0,
            draws  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, size)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS prefs (
            user_id INTEGER PRIMARY KEY,
            theme TEXT NOT NULL DEFAULT 'CLASSIC'
        )
        """)

def db_exec(q: str, p: tuple = ()):
    with closing(db_conn()) as con, con:
        cur = con.execute(q, p)
        return cur.fetchall()

def db_get_game(gid: str) -> Optional[tuple]:
    rows = db_exec("SELECT gid,chat_id,message_id,user_id,size,k,board,turn,status,theme,undo_left,hist,bot_started,started_at,updated_at FROM games WHERE gid=?", (gid,))
    return rows[0] if rows else None

def db_upsert_game(gid, chat_id, message_id, user_id, size, k, board, turn, status, theme, undo_left, hist, bot_started):
    now = int(time.time())
    db_exec("""
    INSERT INTO games (gid,chat_id,message_id,user_id,size,k,board,turn,status,theme,undo_left,hist,bot_started,started_at,updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(gid) DO UPDATE SET
      chat_id=excluded.chat_id,
      message_id=excluded.message_id,
      user_id=excluded.user_id,
      size=excluded.size,
      k=excluded.k,
      board=excluded.board,
      turn=excluded.turn,
      status=excluded.status,
      theme=excluded.theme,
      undo_left=excluded.undo_left,
      hist=excluded.hist,
      bot_started=excluded.bot_started,
      updated_at=excluded.updated_at
    """, (gid, chat_id, message_id, user_id, size, k, board, turn, status, theme, undo_left, hist, bot_started, now, now))

def db_delete_game(gid: str):
    db_exec("DELETE FROM games WHERE gid=?", (gid,))

def db_inc_stat(chat_id: int, user_id: int, size: int, result: str):
    db_exec("""
    INSERT INTO stats (chat_id,user_id,size,x_wins,o_wins,draws)
    VALUES (?,?,?,?,?,?)
    ON CONFLICT(chat_id,user_id,size) DO NOTHING
    """, (chat_id, user_id, size, 0, 0, 0))
    if result == "XWIN":
        db_exec("UPDATE stats SET x_wins = x_wins + 1 WHERE chat_id=? AND user_id=? AND size=?", (chat_id, user_id, size))
    elif result == "OWIN":
        db_exec("UPDATE stats SET o_wins = o_wins + 1 WHERE chat_id=? AND user_id=? AND size=?", (chat_id, user_id, size))
    else:
        db_exec("UPDATE stats SET draws  = draws  + 1 WHERE chat_id=? AND user_id=? AND size=?", (chat_id, user_id, size))

def db_get_stats(chat_id: int, user_id: int, size: int) -> tuple:
    rows = db_exec("SELECT x_wins,o_wins,draws FROM stats WHERE chat_id=? AND user_id=? AND size=?", (chat_id, user_id, size))
    return rows[0] if rows else (0,0,0)

def db_get_lb(chat_id: int, size: int, limit: int = 10) -> List[tuple]:
    return db_exec("""
    SELECT user_id, x_wins, o_wins, draws, (x_wins) AS score
    FROM stats
    WHERE chat_id=? AND size=?
    ORDER BY score DESC, draws DESC
    LIMIT ?
    """, (chat_id, size, limit))

def db_get_theme(user_id: int) -> str:
    rows = db_exec("SELECT theme FROM prefs WHERE user_id=?", (user_id,))
    if rows and rows[0][0] in THEMES:
        return rows[0][0]
    return DEFAULT_THEME

def db_set_theme(user_id: int, theme: str):
    if theme not in THEMES: return
    db_exec("""
    INSERT INTO prefs (user_id, theme) VALUES(?,?)
    ON CONFLICT(user_id) DO UPDATE SET theme=excluded.theme
    """, (user_id, theme))

def cleanup_stale():
    cutoff = int(time.time()) - STALE_SECS
    db_exec("DELETE FROM games WHERE updated_at < ?", (cutoff,))

# -----------------------------
# GAME LOGIC
# -----------------------------
def idx_rc(idx: int, n: int) -> Tuple[int,int]:
    return idx // n, idx % n

def rc_idx(r: int, c: int, n: int) -> int:
    return r * n + c

def empty_cells(board: str) -> List[int]:
    return [i for i,ch in enumerate(board) if ch == EMPTY]

def human_turn(board: str) -> bool:
    return board.count("X") == board.count("O")

def check_winner_generic(board: str, n: int, k: int) -> Optional[str]:
    # rows
    for r in range(n):
        for c in range(n - k + 1):
            seg = board[rc_idx(r,c,n):rc_idx(r,c+k,n):1]
            if all(board[rc_idx(r,c+i,n)] == "X" for i in range(k)): return "X"
            if all(board[rc_idx(r,c+i,n)] == "O" for i in range(k)): return "O"
    # cols
    for c in range(n):
        for r in range(n - k + 1):
            if all(board[rc_idx(r+i,c,n)] == "X" for i in range(k)): return "X"
            if all(board[rc_idx(r+i,c,n)] == "O" for i in range(k)): return "O"
    # diag down-right
    for r in range(n - k + 1):
        for c in range(n - k + 1):
            if all(board[rc_idx(r+i,c+i,n)] == "X" for i in range(k)): return "X"
            if all(board[rc_idx(r+i,c+i,n)] == "O" for i in range(k)): return "O"
    # diag up-right
    for r in range(k-1, n):
        for c in range(n - k + 1):
            if all(board[rc_idx(r-i,c+i,n)] == "X" for i in range(k)): return "X"
            if all(board[rc_idx(r-i,c+i,n)] == "O" for i in range(k)): return "O"
    return None

def is_draw(board: str, n: int, k: int) -> bool:
    return (check_winner_generic(board, n, k) is None) and (EMPTY not in board)

def windows(n: int, k: int):
    # yield all (start_r, start_c, dr, dc) windows of length k
    for r in range(n):
        for c in range(n - k + 1):
            yield (r, c, 0, 1)  # rows
    for c in range(n):
        for r in range(n - k + 1):
            yield (r, c, 1, 0)  # cols
    for r in range(n - k + 1):
        for c in range(n - k + 1):
            yield (r, c, 1, 1)  # diag down-right
    for r in range(k - 1, n):
        for c in range(n - k + 1):
            yield (r, c, -1, 1) # diag up-right

def heuristic(board: str, n: int, k: int) -> int:
    # Positive is good for BOT (O), negative for HUMAN (X).
    # Score potential windows; if both X and O present in a window -> 0
    score = 0
    for (sr, sc, dr, dc) in windows(n, k):
        xs = os_ = 0
        for i in range(k):
            ch = board[rc_idx(sr + dr*i, sc + dc*i, n)]
            xs += (ch == "X")
            os_ += (ch == "O")
        if xs and os_:
            continue
        if xs == 0 and os_ == 0:
            score += 1  # open window
        elif xs == 0:
            # favor higher counts exponentially
            score += 10 ** (os_ - 1)
        elif os_ == 0:
            score -= 10 ** (xs - 1)
    return score

def move_order(n: int) -> List[int]:
    # prefer center-ish cells first for better pruning
    cells = [(r,c) for r in range(n) for c in range(n)]
    center = (n-1)/2
    cells.sort(key=lambda rc: (abs(rc[0]-center)+abs(rc[1]-center), abs(rc[0]-center)*abs(rc[1]-center)))
    return [rc_idx(r,c,n) for r,c in cells]

@lru_cache(maxsize=200000)
def minimax_cached(board: str, n: int, k: int, depth: int, maximizing: bool, alpha: int, beta: int) -> int:
    winner = check_winner_generic(board, n, k)
    if winner == "O": return 10_000 + depth
    if winner == "X": return -10_000 - depth
    if EMPTY not in board or depth == 0:
        return heuristic(board, n, k)

    order = move_order(n)
    if maximizing:
        value = -1_000_000
        for i in order:
            if board[i] != EMPTY: continue
            nb = board[:i] + "O" + board[i+1:]
            val = minimax_cached(nb, n, k, depth-1, False, alpha, beta)
            value = max(value, val)
            alpha = max(alpha, value)
            if beta <= alpha: break
        return value
    else:
        value = 1_000_000
        for i in order:
            if board[i] != EMPTY: continue
            nb = board[:i] + "X" + board[i+1:]
            val = minimax_cached(nb, n, k, depth-1, True, alpha, beta)
            value = min(value, val)
            beta = min(beta, value)
            if beta <= alpha: break
        return value

def best_move(board: str, n: int, k: int) -> int:
    # BOT to move (O)
    depth = SEARCH_DEPTH.get(n, 4)
    best_val = -1_000_000
    best_idx = -1
    for i in move_order(n):
        if board[i] != EMPTY: continue
        nb = board[:i] + "O" + board[i+1:]
        val = minimax_cached(nb, n, k, depth-1, False, -1_000_000, 1_000_000)
        if val > best_val:
            best_val = val
            best_idx = i
    return best_idx

def best_hint(board: str, n: int, k: int) -> int:
    # HUMAN to move (X) — choose move minimizing bot's eval
    depth = SEARCH_DEPTH.get(n, 4)
    best_val = 1_000_000
    best_idx = -1
    for i in move_order(n):
        if board[i] != EMPTY: continue
        nb = board[:i] + "X" + board[i+1:]
        val = minimax_cached(nb, n, k, depth-1, True, -1_000_000, 1_000_000)
        if val < best_val:
            best_val = val
            best_idx = i
    return best_idx

def resolve_status(board: str, n: int, k: int) -> str:
    w = check_winner_generic(board, n, k)
    if w == "X": return "XWIN"
    if w == "O": return "OWIN"
    if EMPTY not in board: return "DRAW"
    return "PLAY"

# -----------------------------
# UI
# -----------------------------
def cells_for_theme(theme: str):
    if theme == "MIN":
        return {"X": "X", "O": "O", " ": "·"}
    return {"X": "❌", "O": "⭕️", " ": "·"}  # CLASSIC

def cell_label(ch: str, theme: str) -> str:
    return cells_for_theme(theme).get(ch, ch)

def render_text(size: int, k: int, board: str, status: str, theme: str, bot_started: int) -> str:
    grid = []
    for r in range(size):
        grid.append(" ".join(cell_label(board[rc_idx(r,c,size)], theme) for c in range(size)))
    title = f"<b>Tic-Tac-Toe</b> {size}×{size} (win {k}) | You: ❌ Bot: ⭕️"
    line = "Your move." if human_turn(board) and status=="PLAY" else ""
    if status == "XWIN": line = "🎉 <b>You win!</b>"
    elif status == "OWIN": line = "🤖 <b>Bot wins!</b>"
    elif status == "DRAW": line = "🤝 <b>Draw.</b>"
    return f"{title}\n{line}\n\n<code>" + "\n".join(grid) + "</code>"

def kb_size_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3×3 (win 3)", callback_data="size:3"),
         InlineKeyboardButton(text="4×4 (win 4)", callback_data="size:4"),
         InlineKeyboardButton(text="5×5 (win 4)", callback_data="size:5")],
    ])

def kb_board(gid: str, size: int, k: int, board: str, status: str, theme: str, at_start: bool, undo_left: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for r in range(size):
        row = []
        for c in range(size):
            idx = rc_idx(r,c,size)
            text = cell_label(board[idx], theme)
            if status == "PLAY" and board[idx] == EMPTY:
                row.append(InlineKeyboardButton(text=text, callback_data=f"m:{gid}:{idx}"))
            else:
                row.append(InlineKeyboardButton(text=text, callback_data=f"n:{gid}"))
        rows.append(row)
    bottom: List[InlineKeyboardButton] = []
    if status == "PLAY" and at_start and board == EMPTY * (size*size):
        bottom.append(InlineKeyboardButton(text="🤖 Bot starts", callback_data=f"bs:{gid}"))
    if status == "PLAY":
        bottom.append(InlineKeyboardButton(text="💡 Hint", callback_data=f"h:{gid}"))
        bottom.append(InlineKeyboardButton(text=f"↩️ Undo ({undo_left})", callback_data=f"u:{gid}"))
        bottom.append(InlineKeyboardButton(text="🏁 Resign", callback_data=f"r:{gid}"))
    bottom.append(InlineKeyboardButton(text="↻ Rematch", callback_data=f"re:{gid}"))
    bottom.append(InlineKeyboardButton(text="🎨 Theme", callback_data=f"t:{gid}"))
    bottom.append(InlineKeyboardButton(text="🗂 Size", callback_data="menu:size"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)

# -----------------------------
# BOT
# -----------------------------
router = Router()

@router.message(CommandStart())
async def start(m: Message):
    cleanup_stale()
    theme = db_get_theme(m.from_user.id)
    await m.answer(
        "Welcome! <b>Hard-only</b> Tic-Tac-Toe with sizes 3×3, 4×4, 5×5.\n"
        "Pick a board size to start:",
        reply_markup=kb_size_menu(),
        parse_mode=ParseMode.HTML
    )

@router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "<b>How to play</b>\n"
        "• Choose size: 3×3 (win 3), 4×4 (win 4), 5×5 (win 4)\n"
        "• All controls are inline under the board\n"
        "• Buttons: 🤖 Bot starts, 💡 Hint, ↩️ Undo(1), 🏁 Resign, ↻ Rematch, 🎨 Theme, 🗂 Size\n"
        "• /stats3 /stats4 /stats5 — your stats for that board size\n"
        "• /top3 /top4 /top5 — leaderboard for that size\n"
        f"aiogram {AIOGRAM_VERSION}",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("stats3"))
async def stats3(m: Message):
    xw, ow, dr = db_get_stats(m.chat.id, m.from_user.id, 3)
    await m.answer(f"<b>3×3 Stats</b>\nWins: {xw}\nLosses: {ow}\nDraws: {dr}", parse_mode=ParseMode.HTML)

@router.message(Command("stats4"))
async def stats4(m: Message):
    xw, ow, dr = db_get_stats(m.chat.id, m.from_user.id, 4)
    await m.answer(f"<b>4×4 Stats</b>\nWins: {xw}\nLosses: {ow}\nDraws: {dr}", parse_mode=ParseMode.HTML)

@router.message(Command("stats5"))
async def stats5(m: Message):
    xw, ow, dr = db_get_stats(m.chat.id, m.from_user.id, 5)
    await m.answer(f"<b>5×5 Stats</b>\nWins: {xw}\nLosses: {ow}\nDraws: {dr}", parse_mode=ParseMode.HTML)

@router.message(Command("top3"))
async def top3(m: Message, bot: Bot):
    await send_lb(m, bot, 3)

@router.message(Command("top4"))
async def top4(m: Message, bot: Bot):
    await send_lb(m, bot, 4)

@router.message(Command("top5"))
async def top5(m: Message, bot: Bot):
    await send_lb(m, bot, 5)

async def send_lb(m: Message, bot: Bot, size: int):
    rows = db_get_lb(m.chat.id, size, 10)
    if not rows:
        await m.answer(f"No games for {size}×{size} yet.")
        return
    lines = [f"<b>Leaderboard {size}×{size}</b>"]
    for i, (uid, xw, ow, dr, score) in enumerate(rows, start=1):
        try:
            u = await bot.get_chat(uid)
            name = u.full_name or (u.username and f"@{u.username}") or str(uid)
        except Exception:
            name = str(uid)
        lines.append(f"{i}. {name} — {xw}W / {ow}L / {dr}D")
    await m.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# ---- Menu: choose size
@router.callback_query(F.data == "menu:size")
async def menu_size(cq: CallbackQuery):
    await cq.answer()
    await cq.message.edit_text("Choose board size:", reply_markup=kb_size_menu())

@router.callback_query(F.data.startswith("size:"))
async def choose_size(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    size = int(cq.data.split(":")[1])
    k = WIN_K.get(size, 3)
    theme = db_get_theme(cq.from_user.id)
    await start_game(bot, cq.message.chat.id, cq.from_user.id, size, k, theme)

# ---- Board actions
@router.callback_query(F.data.startswith("bs:"))
async def bot_start(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    row = db_get_game(cq.data[3:])
    if not valid_click(cq, row): return
    gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started, *_ = row
    if status != "PLAY" or board.strip(EMPTY) != "": return
    i = best_move(board, size, k)
    if i < 0: return
    board = board[:i] + "O" + board[i+1:]
    status = resolve_status(board, size, k)
    hist = (hist + ("," if hist else "") + str(i))
    bot_started = 1
    db_upsert_game(gid, chat_id, mid, uid, size, k, board, HUMAN, status, theme, undo_left, hist, bot_started)
    await edit_board(bot, chat_id, mid, gid, size, k, board, status, theme, undo_left, at_start=False)
    if status in ("XWIN","OWIN","DRAW"):
        db_inc_stat(chat_id, uid, size, status)

@router.callback_query(F.data.startswith("m:"))
async def human_move(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    _, gid, sidx = cq.data.split(":")
    idx = int(sidx)
    row = db_get_game(gid)
    if not valid_click(cq, row): return
    gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started, *_ = row
    if status != "PLAY" or not human_turn(board) or board[idx] != EMPTY:
        return
    # Human move
    board = board[:idx] + "X" + board[idx+1:]
    status = resolve_status(board, size, k)
    hist = (hist + ("," if hist else "") + str(idx))
    if status != "PLAY":
        db_upsert_game(gid, chat_id, mid, uid, size, k, board, BOT_P, status, theme, undo_left, hist, bot_started)
        await edit_board(bot, chat_id, mid, gid, size, k, board, status, theme, undo_left, at_start=False)
        db_inc_stat(chat_id, uid, size, status)
        return
    # Bot reply
    bi = best_move(board, size, k)
    if bi >= 0:
        board = board[:bi] + "O" + board[bi+1:]
        status = resolve_status(board, size, k)
        hist = (hist + "," + str(bi))
    db_upsert_game(gid, chat_id, mid, uid, size, k, board, HUMAN, status, theme, undo_left, hist, bot_started)
    await edit_board(bot, chat_id, mid, gid, size, k, board, status, theme, undo_left, at_start=False)
    if status in ("XWIN","OWIN","DRAW"):
        db_inc_stat(chat_id, uid, size, status)

@router.callback_query(F.data.startswith("h:"))
async def hint(cq: CallbackQuery):
    row = db_get_game(cq.data[2:])
    if not valid_click(cq, row): return
    _, _, _, _, size, k, board, *_ = row
    if not human_turn(board):
        await cq.answer("Not your turn.")
        return
    idx = best_hint(board, size, k)
    if idx < 0:
        await cq.answer("No hint now.")
        return
    r,c = idx_rc(idx, size)
    await cq.answer(f"💡 Best move: row {r+1}, col {c+1}")

@router.callback_query(F.data.startswith("u:"))
async def undo(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    row = db_get_game(cq.data[2:])
    if not valid_click(cq, row): return
    gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started, *_ = row
    if status != "PLAY" or undo_left <= 0:
        await cq.answer("Undo unavailable.")
        return
    moves = [int(x) for x in hist.split(",")] if hist else []
    # We remove last two plies (bot + human) if possible; else one (human)
    if not moves:
        await cq.answer("No moves to undo.")
        return
    # undo bot move if it exists
    if len(moves) >= 2:
        last_bot = moves.pop()
        board = board[:last_bot] + EMPTY + board[last_bot+1:]
    # undo human move
    last_h = moves.pop() if moves else None
    if last_h is not None:
        board = board[:last_h] + EMPTY + board[last_h+1:]
    # rebuild hist
    new_hist = ",".join(map(str, moves))
    undo_left -= 1
    status = resolve_status(board, size, k)
    db_upsert_game(gid, chat_id, mid, uid, size, k, board, HUMAN, status, theme, undo_left, new_hist, bot_started)
    await edit_board(bot, chat_id, mid, gid, size, k, board, status, theme, undo_left, at_start=(board.strip(EMPTY)==""))

@router.callback_query(F.data.startswith("r:"))
async def resign(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    row = db_get_game(cq.data[2:])
    if not valid_click(cq, row): return
    gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started, *_ = row
    if status != "PLAY": return
    status = "OWIN"
    db_upsert_game(gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started)
    await edit_board(bot, chat_id, mid, gid, size, k, board, status, theme, undo_left, at_start=False)
    db_inc_stat(chat_id, uid, size, status)

@router.callback_query(F.data.startswith("re:"))
async def rematch(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    row = db_get_game(cq.data[3:])
    if not valid_click(cq, row): return
    _, chat_id, _, uid, size, k, *_ = row
    theme = db_get_theme(uid)
    await start_game(bot, chat_id, uid, size, k, theme)

@router.callback_query(F.data.startswith("t:"))
async def theme_toggle(cq: CallbackQuery, bot: Bot):
    await cq.answer()
    row = db_get_game(cq.data[2:])
    if not valid_click(cq, row): return
    gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started, *_ = row
    theme = "MIN" if theme == "CLASSIC" else "CLASSIC"
    db_set_theme(uid, theme)
    db_upsert_game(gid, chat_id, mid, uid, size, k, board, turn, status, theme, undo_left, hist, bot_started)
    await edit_board(bot, chat_id, mid, gid, size, k, board, status, theme, undo_left, at_start=(board.strip(EMPTY)==""))

@router.callback_query(F.data.startswith("n:"))
async def noop(cq: CallbackQuery):
    await cq.answer()

# -----------------------------
# HELPERS
# -----------------------------
def valid_click(cq: CallbackQuery, row: Optional[tuple]) -> bool:
    if not cq.message or not row: return False
    gid, chat_id, mid, user_id, *_ = row
    if cq.message.message_id != mid: return False
    return cq.from_user.id == user_id

async def start_game(bot: Bot, chat_id: int, user_id: int, size: int, k: int, theme: str):
    cleanup_stale()
    gid = hex(int(time.time()*1000))[2:]
    board = EMPTY * (size*size)
    status = "PLAY"
    undo_left = 1
    hist = ""
    bot_started = 0
    txt = render_text(size, k, board, status, theme, bot_started)
    kb = kb_board(gid, size, k, board, status, theme, at_start=True, undo_left=undo_left)
    msg = await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    db_upsert_game(gid, chat_id, msg.message_id, user_id, size, k, board, HUMAN, status, theme, undo_left, hist, bot_started)

async def edit_board(bot: Bot, chat_id: int, message_id: int, gid: str, size: int, k: int, board: str, status: str, theme: str, undo_left: int, at_start: bool):
    txt = render_text(size, k, board, status, theme, bot_started=0)
    kb = kb_board(gid, size, k, board, status, theme, at_start, undo_left)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

# -----------------------------
# MAIN
# -----------------------------
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
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
