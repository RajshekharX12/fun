# bot.py
# Unbeatable Tic-Tac-Toe Telegram bot using aiogram 3.x + Minimax.
# - Inline keyboard 3x3 board
# - Human = X, Bot = O (optimal play, cannot be beaten)
# - Works in private chats; safe in groups (ignores others' taps)
# - SQLite persistence so the game survives restarts
# - Added attitude, automatic message cleanup, and enhanced stability.

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
# CONFIGURATION
# -------------------------------------------------------------------

# Time in seconds before an expired game message is automatically deleted
MESSAGE_AUTODELETE_TIMEOUT = 300 # 5 minutes

# -------------------------------------------------------------------
# DB
# -------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "tictactoe.db")

def _check_column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    """Helper to check if a column exists in a table."""
    try:
        cursor = con.execute(f"PRAGMA table_info({table})")
        return any(col[1] == column for col in cursor.fetchall())
    except sqlite3.Error:
        return False

def db_init():
    """Initializes the SQLite database table and performs migrations."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            # 1. Create the base table (using the full desired schema)
            con.execute("""
            CREATE TABLE IF NOT EXISTS games (
                gid TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                board TEXT NOT NULL,
                turn TEXT NOT NULL,
                status TEXT NOT NULL,
                last_update INTEGER
            )
            """)
            
            # 2. Schema Migration: Add missing columns if the table pre-existed with an old schema
            
            # Add 'username' column
            if not _check_column_exists(con, 'games', 'username'):
                con.execute("ALTER TABLE games ADD COLUMN username TEXT")
                logger.info("DB Migration: Added 'username' column to 'games'.")

            # Add 'last_update' column
            if not _check_column_exists(con, 'games', 'last_update'):
                con.execute("ALTER TABLE games ADD COLUMN last_update INTEGER")
                # Populate existing rows with current timestamp
                con.execute("UPDATE games SET last_update = ?", (int(time.time()),))
                logger.info("DB Migration: Added and initialized 'last_update' column to 'games'.")
                
            logger.info(f"Database initialized and migrated at {DB_PATH}")
            
    except sqlite3.Error as e:
        logger.error(f"DB initialization/migration error: {e}")

def db_exec(query: str, params: tuple = (), fetch_one=False):
    """Executes a database query and returns results."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            cur = con.execute(query, params)
            if fetch_one:
                return cur.fetchone()
            return cur.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB execution error for query '{query}': {e}")
        return None if fetch_one else []

def db_get_game(gid: str) -> Optional[tuple]:
    """Retrieves a game by its ID (8 columns)."""
    # Explicitly selecting the 8 columns used in unpacking
    return db_exec("SELECT gid,chat_id,message_id,user_id,username,board,turn,status FROM games WHERE gid=?", (gid,), fetch_one=True)

def db_upsert_game(gid: str, chat_id: int, message_id: int, user_id: int, username: str, board: str, turn: str, status: str):
    """Inserts or updates a game record (9 columns)."""
    current_time = int(time.time())
    db_exec("""
    INSERT INTO games (gid,chat_id,message_id,user_id,username,board,turn,status,last_update)
    VALUES (?,?,?,?,?,?,?,?,?)
    ON CONFLICT(gid) DO UPDATE SET board=excluded.board, turn=excluded.turn, status=excluded.status,
                                  chat_id=excluded.chat_id, message_id=excluded.message_id, user_id=excluded.user_id,
                                  username=excluded.username, last_update=?
    """, (gid, chat_id, message_id, user_id, username, board, turn, status, current_time, current_time))

def db_get_expired_games(timeout: int) -> List[tuple]:
    """Retrieves games that haven't been updated for 'timeout' seconds."""
    cutoff = int(time.time()) - timeout
    return db_exec("SELECT gid, chat_id, message_id FROM games WHERE last_update < ?", (cutoff,))

def db_delete_game(gid: str):
    """Deletes a game record."""
    db_exec("DELETE FROM games WHERE gid=?", (gid,))


# -------------------------------------------------------------------
# Game logic (Minimax) - No change, logic is perfect
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
    
    # Prioritize moves: Center (4), Corners (0, 2, 6, 8), Edges (1, 3, 5, 7)
    ordered_cells = [i for i in [4, 0, 2, 6, 8, 1, 3, 5, 7] if board[i] == EMPTY]
    
    for i in ordered_cells:
        newb = board[:i] + BOT + board[i+1:]
        score = minimax(newb, False, 0) # False: next is min player (Human)
        
        if score > best_score:
            best_score = score
            best_idx = i
            
    if best_idx == -1:
        return empty_cells(board)[0]
    
    return best_idx

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

# -------------------------------------------------------------------
# UI helpers & Attitude - Minor cleanup/formatting
# -------------------------------------------------------------------
def cell_emoji(ch: str) -> str:
    """Converts internal char ('X', 'O', ' ') to emoji."""
    return "❌" if ch == "X" else ("⭕️" if ch == "O" else "•")

def HUMAN_turn(board: str) -> bool:
    """Checks if it's the Human's (X) turn."""
    x_count = board.count(HUMAN)
    o_count = board.count(BOT)
    return x_count == o_count

def attitude_message(status: str, username: str) -> str:
    """Generates an attitude-filled message based on the game's final status."""
    user_name = f"@{username}" if username and '@' in username else (username or "human")
    
    if status == "DRAW":
        return f"🖕 | {user_name}, what kind of jerk are you? You can't even beat me... I'm perfect, and you're just... mediocre."
    elif status == "OWIN":
        return f"👑 | Hah! {user_name}, you really thought you could win? I calculate all possibilities. Your total defeat was inevitable. Bow down."
    elif status == "XWIN":
        return f"🤯 | IMPOSSIBLE! {user_name}, this must be a glitch in the Matrix! The universe is broken! I DEMAND A RECOUNT!"
    else:
        return "You're still playing, stop distracting me with your silly feelings."


def render_text(board: str, status: str, username: Optional[str] = None) -> str:
    """Renders the game board and status message."""
    
    rows = [
        " ".join(cell_emoji(board[i]) for i in range(0,3)),
        " ".join(cell_emoji(board[i]) for i in range(3,6)),
        " ".join(cell_emoji(board[i]) for i in range(6,9)),
    ]
    
    heading = "<b>Tic-Tac-Toe</b> (You: ❌  |  Bot: ⭕️)"
    board_str = "\n\n<code>" + "\n".join(rows) + "</code>"
    
    if status == "PLAY":
        is_human_turn = HUMAN_turn(board)
        if board.count(EMPTY) == 9:
            status_line = "Your move. Try not to embarrass yourself."
        elif is_human_turn:
             status_line = "Your turn. I'm waiting. Tick-tock."
        else:
             status_line = "Bot is calculating your inevitable demise..." 
    else:
        status_line = attitude_message(status, username)
        
    return heading + "\n\n" + status_line + board_str

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
                row.append(InlineKeyboardButton(text=text, callback_data=f"mv:{gid}:{idx}"))
            else:
                row.append(InlineKeyboardButton(text=text, callback_data=f"noop:{gid}"))
        kb_rows.append(row)

    bottom = []
    if status == "PLAY" and board == EMPTY * 9:
        bottom.append(InlineKeyboardButton(text="🤖 Bot starts (I am the master)", callback_data=f"botstart:{gid}"))
        
    bottom.append(InlineKeyboardButton(text="⚔️ New game (Same result, probably)", callback_data="new"))
    kb_rows.append(bottom)
    
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# -------------------------------------------------------------------
# Bot wiring & Handlers - Stabilized asynchronous flow
# -------------------------------------------------------------------
router = Router()

@router.message(CommandStart())
async def on_start(m: Message):
    """Handles the /start command with the new bot bio/attitude."""
    username = m.from_user.username or m.from_user.first_name
    await m.answer(
        f"Hi, {username}. I’m an <b>unbeatable</b> Tic-Tac-Toe bot.\n"
        "• You are ❌, I am ⭕️\n"
        "• I play **perfectly** (Minimax) — so don't strain your tiny brain. You can only draw if you also play perfectly.\n\n"
        "Tap <b>New game</b> to start your inevitable loss.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎮 New game", callback_data="new")]
        ])
    )

@router.message(Command("newgame"))
async def on_newgame_cmd(m: Message, bot: Bot):
    """Handles the /newgame command."""
    username = m.from_user.username or m.from_user.first_name
    await start_new_game(m.chat.id, m.from_user.id, username, bot)

@router.callback_query(F.data == "new")
async def on_new(cq: CallbackQuery, bot: Bot):
    """Handles the 'New game' inline button click."""
    await cq.answer()
    username = cq.from_user.username or cq.from_user.first_name
    await start_new_game(cq.message.chat.id, cq.from_user.id, username, bot)

async def start_new_game(chat_id: int, user_id: int, username: str, bot: Bot):
    """Starts a new game and sends the initial message."""
    gid = hex(int(time.time() * 1000))[2:] + hex(user_id)[2:]
    board = EMPTY * 9
    status = "PLAY"
    turn = HUMAN # Human (X) starts by default

    txt = render_text(board, status, username)
    kb = board_keyboard(gid, board, status)
    
    try:
        msg = await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
        # Turn is explicitly HUMAN since X always starts
        db_upsert_game(gid, chat_id, msg.message_id, user_id, username, board, turn, status)
        logger.info(f"New game {gid} started.")
    except Exception as e:
        logger.error(f"Failed to send new game message to chat {chat_id}: {e}")

@router.callback_query(F.data.startswith("botstart:"))
async def on_bot_start(cq: CallbackQuery, bot: Bot):
    """Handles the 'Bot starts' button click."""
    await cq.answer()
    _, gid = cq.data.split(":")
    game = db_get_game(gid)
    
    if not game or not valid_game_click(cq, game):
        return
        
    gid, chat_id, message_id, user_id, username, board, turn, status = game
    
    if board != EMPTY * 9 or status != "PLAY":
        return
        
    # --- Bot move (O) ---
    idx = best_move(board)
    board = board[:idx] + BOT + board[idx+1:]
    
    status = resolve_status(board)
    turn = HUMAN # Next is human's turn
    
    db_upsert_game(gid, chat_id, message_id, user_id, username, board, turn, status)
    await edit_board(bot, chat_id, message_id, gid, user_id, username, board, status)

@router.callback_query(F.data.startswith("mv:"))
async def on_move(cq: CallbackQuery, bot: Bot):
    """Handles a human's move and schedules the bot's counter-move."""
    await cq.answer()
    _, gid, sidx = cq.data.split(":")
    idx = int(sidx)
    game = db_get_game(gid)
    
    if not game or not valid_game_click(cq, game):
        return

    # Unpack the 8 columns
    gid, chat_id, message_id, user_id, username, board, turn, status = game
    
    # Validate: still playing, human's turn, cell empty
    if status != "PLAY" or not HUMAN_turn(board) or board[idx] != EMPTY:
        return
    
    # --- 1. Human move (X) and initial status check ---
    board_after_human = board[:idx] + HUMAN + board[idx+1:]
    status_after_human = resolve_status(board_after_human)
    
    # Update DB immediately with human's move and new status
    # Next turn is BOT if PLAYING, else it doesn't matter (setting to BOT)
    db_upsert_game(gid, chat_id, message_id, user_id, username, board_after_human, BOT, status_after_human)
    
    if status_after_human != "PLAY":
        # Game over after human move (Win or Draw)
        await edit_board(bot, chat_id, message_id, gid, user_id, username, board_after_human, status_after_human)
        asyncio.create_task(delete_message_after_timeout(bot, chat_id, message_id, gid, MESSAGE_AUTODELETE_TIMEOUT))
        return

    # --- 2. Update board to 'Bot is thinking...' ---
    await edit_board(bot, chat_id, message_id, gid, user_id, username, board_after_human, status_after_human)

    # --- 3. Schedule Bot move (O) as a separate task ---
    asyncio.create_task(
        bot_move_task(bot, gid)
    )

async def bot_move_task(bot: Bot, gid: str):
    """Calculates and executes the bot's move based on the stored game ID."""
    try:
        # Give a small artificial delay for the 'thinking' effect
        await asyncio.sleep(0.5) 

        # Retrieve the LATEST game state
        game = db_get_game(gid)
        if not game:
            logger.warning(f"Bot move task: Game {gid} disappeared from DB.")
            return
            
        gid, chat_id, message_id, user_id, username, board, turn, status = game
        
        # Double check state before moving
        if status != "PLAY" or HUMAN_turn(board):
             # Game might have been reset or ended by another process
             return
             
        # Recalculate best move on the latest board state
        bot_idx = best_move(board)
        board_after_bot = board[:bot_idx] + BOT + board[bot_idx+1:]
        status_after_bot = resolve_status(board_after_bot)
        
        # Next turn is Human (X), unless game ended
        next_turn = HUMAN if status_after_bot == "PLAY" else BOT 

        # Update DB with final state
        db_upsert_game(gid, chat_id, message_id, user_id, username, board_after_bot, next_turn, status_after_bot)
        
        # Edit message to show final board state
        await edit_board(bot, chat_id, message_id, gid, user_id, username, board_after_bot, status_after_bot)

        if status_after_bot != "PLAY":
            asyncio.create_task(delete_message_after_timeout(bot, chat_id, message_id, gid, MESSAGE_AUTODELETE_TIMEOUT))

    except Exception as e:
        logger.error(f"Bot move task failed for game {gid}: {e}")
        
@router.callback_query(F.data.startswith("noop:"))
async def on_noop(cq: CallbackQuery):
    """Handles clicks on non-action buttons (taken cells, bot's turn, etc.)."""
    await cq.answer("This button is not for you, or it's not your turn.")
    
# -------------------------------------------------------------------
# Message and Game Management
# -------------------------------------------------------------------

def valid_game_click(cq: CallbackQuery, game_row: Optional[tuple]) -> bool:
    """Validates if the button click is from the correct user and on the correct message."""
    if not cq.message or not game_row:
        return False
        
    # Unpack the 8 columns (DB_GET_GAME must return 8)
    gid, chat_id, message_id, user_id, username, board, turn, status = game_row
    
    if cq.message.message_id != message_id:
        return False
        
    if cq.from_user.id != user_id:
        # Only the player who started the game can play
        player_name = cq.from_user.first_name
        # The logic is correct here: a *different* user gets this message
        asyncio.create_task(cq.answer(f"Hush, {player_name}. This is not your game! Go start a /newgame.", show_alert=True))
        return False
        
    return True

async def edit_board(bot: Bot, chat_id: int, message_id: int, gid: str, user_id: int, username: str, board: str, status: str):
    """Edits the board message or sends a new one if editing fails."""
    new_text = render_text(board, status, username)
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
        if "message is not modified" in str(e).lower():
            return
            
        logger.warning(f"Failed to edit message {message_id} in chat {chat_id}: {e}. Sending new message as fallback.")
        
        # Fall back to sending a new message
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=new_text,
                reply_markup=new_keyboard,
                parse_mode=ParseMode.HTML
            )
            # IMPORTANT: Update message_id in the DB
            is_human_turn = HUMAN_turn(board)
            next_turn = HUMAN if is_human_turn and status == "PLAY" else BOT
            db_upsert_game(gid, chat_id, msg.message_id, user_id, username, board, next_turn, status)
            logger.info(f"New message sent with new message_id: {msg.message_id}")
        except Exception as e:
            logger.error(f"Failed to send NEW message for fallback in chat {chat_id}: {e}")

async def delete_message_after_timeout(bot: Bot, chat_id: int, message_id: int, gid: str, timeout: int):
    """Waits for the timeout and then deletes the message and the DB record."""
    if timeout > 0:
        await asyncio.sleep(timeout)
    
    try:
        await bot.delete_message(chat_id, message_id)
        logger.info(f"Deleted expired message {message_id} for game {gid}")
    except Exception as e:
        logger.debug(f"Could not delete message {message_id} in chat {chat_id}: {e}")
    finally:
        db_delete_game(gid)
        logger.info(f"Deleted game record {gid} from DB.")

async def periodic_cleanup(bot: Bot):
    """Periodically checks for and deletes expired game messages."""
    while True:
        await asyncio.sleep(300) 
        
        logger.info("Running periodic cleanup for expired games...")
        expired_games = db_get_expired_games(MESSAGE_AUTODELETE_TIMEOUT)
        
        for gid, chat_id, message_id in expired_games:
            asyncio.create_task(delete_message_after_timeout(bot, chat_id, message_id, gid, 0))

        if expired_games:
             logger.info(f"Cleaned up {len(expired_games)} expired game messages.")


async def main():
    """Main entry point for the bot."""
    db_init() 
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    
    # Start the periodic cleanup task
    asyncio.create_task(periodic_cleanup(bot))
    
    logger.info("Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user interrupt.")
    except Exception as e:
        logger.error(f"An unexpected error occurred in main: {e}")
