# bot.py
# Unbeatable Tic-Tac-Toe Telegram bot using aiogram 3.x + Minimax.
# - Inline keyboard 3x3 board
# - Human = X, Bot = O (optimal play, cannot be beaten)
# - Works in private chats; safe in groups (ignores others' taps)
# - SQLite persistence so the game survives restarts

import asyncio
import os
import sqlite3
import time
import logging
from contextlib import closing
from typing import List, Optional, Tuple

# --- Env loader setup ---
from dotenv import load_dotenv

# Load environment variables from a .env file
load_dotenv()
# ------------------------

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN env var (or use a .env loader).")

# -------------------------------------------------------------------
# DB
# -------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "tictactoe.db")

def db_init():
    """Initializes the SQLite database table."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("""
            CREATE TABLE IF NOT EXISTS games (
                gid TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                board TEXT NOT NULL,           -- 9 chars: ' ', 'X', 'O'
                turn TEXT NOT NULL,            -- 'X' or 'O'
                status TEXT NOT NULL           -- 'PLAY', 'XWIN', 'OWIN', 'DRAW'
            )
            """)
        logger.info(f"Database initialized at {DB_PATH}")
    except sqlite3.Error as e:
        logger.error(f"DB initialization error: {e}")

def db_exec(query: str, params: tuple = ()):
    """Executes a database query and returns all results."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            cur = con.execute(query, params)
            return cur.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB execution error for query '{query}': {e}")
        return []

def db_get_game(gid: str) -> Optional[tuple]:
    """Retrieves a game by its ID."""
    rows = db_exec("SELECT gid,chat_id,message_id,user_id,board,turn,status FROM games WHERE gid=?", (gid,))
    return rows[0] if rows else None

def db_upsert_game(gid: str, chat_id: int, message_id: int, user_id: int, board: str, turn: str, status: str):
    """Inserts or updates a game record."""
    db_exec("""
    INSERT INTO games (gid,chat_id,message_id,user_id,board,turn,status)
    VALUES (?,?,?,?,?,?,?)
    ON CONFLICT(gid) DO UPDATE SET board=excluded.board, turn=excluded.turn, status=excluded.status,
                                  chat_id=excluded.chat_id, message_id=excluded.message_id, user_id=excluded.user_id
    """, (gid, chat_id, message_id, user_id, board, turn, status))

# db_delete_game is not used in the original logic, keeping it for completeness
# def db_delete_game(gid: str):
#     """Deletes a game record."""
#     db_exec("DELETE FROM games WHERE gid=?", (gid,))

# -------------------------------------------------------------------
# Game logic (Minimax)
# -------------------------------------------------------------------
HUMAN = "X"
BOT = "O"
EMPTY = " "

def lines() -> List[Tuple[int, int, int]]:
    """Returns all winning lines (rows, columns, diagonals)."""
    return [
        (0,1,2), (3,4,5), (6,7,8),  # rows
        (0,3,6), (1,4,7), (2,5,8),  # cols
        (0,4,8), (2,4,6)            # diags
    ]

def check_winner(board: str) -> Optional[str]:
    """Checks if there is a winner and returns 'X' or 'O', or None."""
    for a,b,c in lines():
        if board[a] != EMPTY and board[a] == board[b] == board[c]:
            return board[a]
    return None

def empty_cells(board: str) -> List[int]:
    """Returns a list of indices for empty cells."""
    return [i for i, ch in enumerate(board) if ch == EMPTY]

def is_draw(board: str) -> bool:
    """Checks if the game is a draw."""
    return (check_winner(board) is None) and (EMPTY not in board)

def minimax(board: str, is_max: bool, depth: int = 0) -> int:
    """The Minimax algorithm to determine the optimal score."""
    w = check_winner(board)
    if w == BOT:   # Bot (Maximizer) wins
        return 10 - depth
    if w == HUMAN: # Human (Minimizer) wins
        return depth - 10
    if EMPTY not in board:
        return 0 # Draw

    if is_max:
        best = -10**9
        for i in empty_cells(board):
            newb = board[:i] + BOT + board[i+1:]
            best = max(best, minimax(newb, False, depth + 1))
        return best
    else:
        best = 10**9
        for i in empty_cells(board):
            newb = board[:i] + HUMAN + board[i+1:]
            best = min(best, minimax(newb, True, depth + 1))
        return best

def best_move(board: str) -> int:
    """Finds the optimal move index for the BOT."""
    best_score = -10**9
    best_idx = -1
    # Check moves in a specific order (e.g., center, corners) to break ties
    # The default index order (0 to 8) already works well, but we can prioritize center (4)
    # The current implementation's tie-breaker is by lowest index (first encountered)
    
    # Iterate through all possible moves
    for i in empty_cells(board):
        newb = board[:i] + BOT + board[i+1:]
        # Calculate the score for this move
        score = minimax(newb, False, 0) # False: next is min player (Human)
        
        # Update best move. Prioritizes moves with higher score.
        # If scores are equal, the first move (lowest index) is chosen.
        if score > best_score:
            best_score = score
            best_idx = i
            
    # Fallback to a random move if no best move found (shouldn't happen)
    if best_idx == -1:
        return empty_cells(board)[0] # Safety: pick the first available cell
    
    return best_idx

# -------------------------------------------------------------------
# UI helpers
# -------------------------------------------------------------------
def cell_emoji(ch: str) -> str:
    """Converts internal char ('X', 'O', ' ') to emoji."""
    return "❌" if ch == "X" else ("⭕️" if ch == "O" else "•") # Using • instead of · as it renders better

def render_text(board: str, status: str) -> str:
    """Renders the game board and status message."""
    rows = [
        " ".join(cell_emoji(board[i]) for i in range(0,3)),
        " ".join(cell_emoji(board[i]) for i in range(3,6)),
        " ".join(cell_emoji(board[i]) for i in range(6,9)),
    ]
    head = "<b>Tic-Tac-Toe</b> (You: ❌  |  Bot: ⭕️)\n"
    if status == "PLAY":
        # Check whose turn it is
        is_human_turn = HUMAN_turn(board)
        # Correctly determine the message based on who starts (X is human, always starts if board empty)
        if board.count(EMPTY) == 9:
            head += "Your move." # Human starts
        elif is_human_turn:
             head += "Your turn."
        else:
             head += "Bot is thinking..." # Should be displayed right before bot moves, but serves as a prompt otherwise

    elif status == "XWIN":
        head += "🎉 You win!"
    elif status == "OWIN":
        head += "🤖 Bot wins!"
    else: # DRAW
        head += "🤝 Draw."
        
    return head + "\n\n<code>" + "\n".join(rows) + "</code>"

def HUMAN_turn(board: str) -> bool:
    """Checks if it's the Human's (X) turn."""
    # X always starts; turns alternate. X has equal or one more move than O.
    x_count = board.count(HUMAN)
    o_count = board.count(BOT)
    return x_count == o_count

def board_keyboard(gid: str, board: str, status: str) -> InlineKeyboardMarkup:
    """Generates the inline keyboard for the board."""
    kb_rows = []
    can_play = status == "PLAY" and HUMAN_turn(board)
    
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            ch = board[idx]
            text = cell_emoji(ch)
            
            if can_play and ch == EMPTY:
                # Human's turn and cell is empty -> clickable move button
                row.append(InlineKeyboardButton(text=text, callback_data=f"mv:{gid}:{idx}"))
            else:
                # Cell is taken, or not human's turn, or game is over -> noop button
                row.append(InlineKeyboardButton(text=text, callback_data=f"noop:{gid}"))
        kb_rows.append(row)

    bottom = []
    # Only offer "Bot starts" if the board is completely empty
    if status == "PLAY" and board == EMPTY * 9:
        bottom.append(InlineKeyboardButton(text="🤖 Bot starts", callback_data=f"botstart:{gid}"))
        
    bottom.append(InlineKeyboardButton(text="↻ New game", callback_data="new"))
    kb_rows.append(bottom)
    
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# -------------------------------------------------------------------
# Bot wiring
# -------------------------------------------------------------------
router = Router()

@router.message(CommandStart())
async def on_start(m: Message):
    """Handles the /start command."""
    await m.answer(
        "Hi! I’m an <b>unbeatable</b> Tic-Tac-Toe bot.\n"
        "• You are ❌, I am ⭕️\n"
        "• I play perfectly (Minimax) — you can only draw if you also play perfectly.\n\n"
        "Tap <b>New game</b> to begin.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎮 New game", callback_data="new")]
        ])
    )

@router.message(Command("newgame"))
async def on_newgame_cmd(m: Message, bot: Bot):
    """Handles the /newgame command."""
    # Note: Using m.from_user.id for user_id, which is correct
    await start_new_game(m.chat.id, m.from_user.id, bot)

@router.callback_query(F.data == "new")
async def on_new(cq: CallbackQuery, bot: Bot):
    """Handles the 'New game' inline button click."""
    await cq.answer()
    # Note: Using cq.from_user.id for user_id, which is correct
    await start_new_game(cq.message.chat.id, cq.from_user.id, bot)

async def start_new_game(chat_id: int, user_id: int, bot: Bot):
    """Starts a new game and sends the initial message."""
    # Use current time + user ID to make the game ID more unique (especially in groups)
    gid = hex(int(time.time() * 1000))[2:] + hex(user_id)[2:]
    board = EMPTY * 9
    status = "PLAY"
    turn = HUMAN # Human (X) starts by default

    txt = render_text(board, status)
    kb = board_keyboard(gid, board, status)
    
    try:
        msg = await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
        # Store message_id for later edits
        db_upsert_game(gid, chat_id, msg.message_id, user_id, board, turn, status)
    except Exception as e:
        logger.error(f"Failed to send new game message to chat {chat_id}: {e}")

@router.callback_query(F.data.startswith("botstart:"))
async def on_bot_start(cq: CallbackQuery, bot: Bot):
    """Handles the 'Bot starts' button click."""
    await cq.answer()
    _, gid = cq.data.split(":")
    game = db_get_game(gid)
    
    if not valid_game_click(cq, game):
        return
        
    gid, chat_id, message_id, user_id, board, turn, status = game
    
    # Must be the very start of the game
    if board != EMPTY * 9 or status != "PLAY":
        return
        
    # Bot makes the first move (O)
    idx = best_move(board)
    board = board[:idx] + BOT + board[idx+1:]
    
    # Check status after bot's move (should still be PLAY)
    status = resolve_status(board)
    turn = HUMAN # Next is human's turn
    
    db_upsert_game(gid, chat_id, message_id, user_id, board, turn, status)
    await edit_board(bot, chat_id, message_id, gid, user_id, board, status)

@router.callback_query(F.data.startswith("mv:"))
async def on_move(cq: CallbackQuery, bot: Bot):
    """Handles a human's move."""
    await cq.answer()
    _, gid, sidx = cq.data.split(":")
    idx = int(sidx)
    game = db_get_game(gid)
    
    if not valid_game_click(cq, game):
        return

    gid, chat_id, message_id, user_id, board, turn, status = game
    
    # Validate: still playing, human's turn, cell empty
    if status != "PLAY" or not HUMAN_turn(board) or board[idx] != EMPTY:
        return
    
    # --- Human move (X) ---
    board_after_human = board[:idx] + HUMAN + board[idx+1:]
    status_after_human = resolve_status(board_after_human)
    
    if status_after_human != "PLAY":
        # Game over after human move (Win or Draw)
        db_upsert_game(gid, chat_id, message_id, user_id, board_after_human, BOT, status_after_human)
        await edit_board(bot, chat_id, message_id, gid, user_id, board_after_human, status_after_human)
        return

    # --- Bot move (O) ---
    # Bot move is made immediately after human's move if the game continues
    bot_idx = best_move(board_after_human)
    board_after_bot = board_after_human[:bot_idx] + BOT + board_after_human[bot_idx+1:]
    status_after_bot = resolve_status(board_after_bot)
    
    # Human's turn next, regardless of status (unless game ended)
    db_upsert_game(gid, chat_id, message_id, user_id, board_after_bot, HUMAN, status_after_bot)
    await edit_board(bot, chat_id, message_id, gid, user_id, board_after_bot, status_after_bot)

@router.callback_query(F.data.startswith("noop:"))
async def on_noop(cq: CallbackQuery):
    """Handles clicks on non-action buttons (taken cells, bot's turn, etc.)."""
    # Just remove the spinner and do nothing else
    await cq.answer()

def resolve_status(board: str) -> str:
    """Determines the current game status."""
    w = check_winner(board)
    if w == HUMAN:
        return "XWIN"
    if w == BOT:
        return "OWIN"
    if is_draw(board):
        return "DRAW"
    return "PLAY"

def valid_game_click(cq: CallbackQuery, game_row: Optional[tuple]) -> bool:
    """Validates if the button click is from the correct user and on the correct message."""
    if not cq.message or not game_row:
        # Should not happen if DB is consistent, but a safety check
        logger.warning(f"Callback query {cq.id} failed basic validation (message/game row missing).")
        return False
        
    gid, chat_id, message_id, user_id, board, turn, status = game_row
    
    if cq.message.message_id != message_id:
        # Ignore clicks on old messages
        logger.debug(f"Ignoring click on old message_id {cq.message.message_id} for game {gid}")
        return False
        
    if cq.from_user.id != user_id:
        # Only the player who started the game can play
        logger.info(f"Unauthorized click from user {cq.from_user.id} on game {gid} (owner: {user_id})")
        # We can answer to let them know it's not their game
        asyncio.create_task(cq.answer("This is not your game! Start a /newgame.", show_alert=True))
        return False
        
    return True

async def edit_board(bot: Bot, chat_id: int, message_id: int, gid: str, user_id: int, board: str, status: str):
    """Edits the board message or sends a new one if editing fails."""
    new_text = render_text(board, status)
    new_keyboard = board_keyboard(gid, board, status)
    
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=new_text,
            reply_markup=new_keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"Failed to edit message {message_id} in chat {chat_id}: {e}. Sending new message as fallback.")
        
        # Fall back to sending a new message
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=new_text,
                reply_markup=new_keyboard,
                parse_mode=ParseMode.HTML
            )
            # IMPORTANT: Update message_id in the DB so further moves continue on the new message
            db_upsert_game(gid, chat_id, msg.message_id, user_id, board, HUMAN if status == "PLAY" else BOT, status)
            logger.info(f"New message sent with new message_id: {msg.message_id}")
        except Exception as e:
            logger.error(f"Failed to send NEW message for fallback in chat {chat_id}: {e}")

async def main():
    """Main entry point for the bot."""
    db_init()
    # Ensure all required imports are used in the main logic (ParseMode.HTML is default)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user interrupt.")
    except Exception as e:
        logger.error(f"An unexpected error occurred in main: {e}")

