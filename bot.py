# bot.py
# Unbeatable Tic-Tac-Toe Telegram bot using aiogram 3.x + Minimax.
# - Inline keyboard 3x3 board
# - Human = X, Bot = O (optimal play, cannot be beaten)
# - Works in private chats; safe in groups (ignores others' taps)
# - Game state is held in memory (in the Dispatcher's context).
# - Added attitude and message cleanup removal.

import asyncio
import os
import time
import logging
from typing import List, Optional, Tuple, Dict, Any

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
# TYPE DEFINITIONS & IN-MEMORY STATE
# -------------------------------------------------------------------

# Game State structure:
# gid: unique ID for the game
# chat_id: chat where the game is played
# message_id: the message containing the board
# user_id: the user who started the game (the only one who can play)
# username: the player's name (for attitude)
# board: 9 chars: ' ', 'X', 'O'
# status: 'PLAY', 'XWIN', 'OWIN', 'DRAW'
Game = Tuple[str, int, int, int, str, str, str] # (gid, chat_id, message_id, user_id, username, board, status)

# The memory store for active games: { message_id: Game }
# This will be stored in dp.workflow_data
GAME_STATE_KEY = "active_games"

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

# Minimax logic remains the same (it was correct)
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
    
    # Prioritize center, then corners, then sides for faster Minimax
    ordered_cells = [i for i in [4, 0, 2, 6, 8, 1, 3, 5, 7] if board[i] == EMPTY]
    
    for i in ordered_cells:
        newb = board[:i] + BOT + board[i+1:]
        score = minimax(newb, False, 0) # False: next is min player (Human)
        
        if score > best_score:
            best_score = score
            best_idx = i
            
    # Should always find a move if not draw/win
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
# UI helpers & Attitude
# -------------------------------------------------------------------
def cell_emoji(ch: str) -> str:
    """Converts internal char ('X', 'O', ' ') to emoji."""
    return "❌" if ch == "X" else ("⭕️" if ch == "O" else "•")

def is_human_turn(board: str) -> bool:
    """Checks if it's the Human's (X) turn."""
    x_count = board.count(HUMAN)
    o_count = board.count(BOT)
    return x_count == o_count # X always starts, so counts should be equal

def attitude_message(status: str, username: str) -> str:
    """Generates an attitude-filled message based on the game's final status."""
    user_name = f"@{username}" if username else "human"
    
    if status == "DRAW":
        # Attitude: Middle finger, jerk, can't beat me
        return f"🖕 | **{user_name}**, what kind of jerk are you? You can't even beat me... I'm perfect, and you're just... mediocre."
    elif status == "OWIN":
        # Attitude: Smug victory, total defeat
        return f"👑 | Hah! **{user_name}**, you really thought you could win? I calculate all possibilities. Your total defeat was inevitable. Bow down."
    elif status == "XWIN":
        # Attitude: Shock, impossible scenario (only happens if minimax is bugged or logic error, but respond anyway)
        return f"🤯 | IMPOSSIBLE! **{user_name}**, this must be a glitch in the Matrix! The universe is broken! I DEMAND A RECOUNT!"
    else: # Should not happen for a final message
        return "Game is over, stop distracting me with your silly feelings."


def render_text(board: str, status: str, username: Optional[str] = None) -> str:
    """Renders the game board and status message."""
    
    rows = [
        " ".join(cell_emoji(board[i]) for i in range(0,3)),
        " ".join(cell_emoji(board[i]) for i in range(3,6)),
        " ".join(cell_emoji(board[i]) for i in range(6,9)),
    ]
    
    # Use a generic heading for the board itself
    heading = "<b>Tic-Tac-Toe</b> (You: ❌  |  Bot: ⭕️)"
    board_str = "\n\n<code>" + "\n".join(rows) + "</code>"
    
    # Determine the status line
    if status == "PLAY":
        is_human = is_human_turn(board)
        if board.count(EMPTY) == 9:
            status_line = "Your move. Try not to embarrass yourself." # Human starts
        elif is_human:
             status_line = "Your turn. I'm waiting. Tick-tock."
        else:
             # This is displayed *before* the bot moves, giving the thinking effect
             status_line = "Bot is calculating your inevitable demise..." 
    else:
        # Game over - use the attitude message
        status_line = attitude_message(status, username)
        
    return heading + "\n\n" + status_line + board_str

def board_keyboard(gid: str, board: str, status: str) -> InlineKeyboardMarkup:
    """Generates the inline keyboard for the board."""
    kb_rows = []
    # Can play only if game is PLAYING AND it's the HUMAN's turn
    can_play = status == "PLAY" and is_human_turn(board)
    
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
                # All other states: noop button
                row.append(InlineKeyboardButton(text=text, callback_data=f"noop:{gid}"))
        kb_rows.append(row)

    bottom = []
    # Only offer "Bot starts" if the board is completely empty AND game is PLAYING
    if status == "PLAY" and board == EMPTY * 9:
        bottom.append(InlineKeyboardButton(text="🤖 Bot starts (I am the master)", callback_data=f"botstart:{gid}"))
        
    # The 'new' button is always available
    bottom.append(InlineKeyboardButton(text="⚔️ New game (Same result, probably)", callback_data="new"))
    kb_rows.append(bottom)
    
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# -------------------------------------------------------------------
# Bot wiring & Handlers
# -------------------------------------------------------------------
router = Router()

def get_game(message_id: int, workflow_data: Dict[str, Any]) -> Optional[Game]:
    """Retrieves a game state from memory."""
    return workflow_data.get(GAME_STATE_KEY, {}).get(message_id)

def set_game(game: Game, workflow_data: Dict[str, Any]):
    """Stores or updates a game state in memory."""
    gid, chat_id, message_id, user_id, username, board, status = game
    if GAME_STATE_KEY not in workflow_data:
        workflow_data[GAME_STATE_KEY] = {}
    
    # Store by message_id for easy retrieval/update via CallbackQuery
    workflow_data[GAME_STATE_KEY][message_id] = game

def delete_game(message_id: int, workflow_data: Dict[str, Any]):
    """Deletes a game state from memory."""
    if GAME_STATE_KEY in workflow_data and message_id in workflow_data[GAME_STATE_KEY]:
        del workflow_data[GAME_STATE_KEY][message_id]
        logger.info(f"Deleted game state for message {message_id}")

@router.message(CommandStart())
async def on_start(m: Message):
    """Handles the /start command with the new bot bio/attitude."""
    username = m.from_user.username or m.from_user.first_name
    await m.answer(
        f"Hi, **{username}**. I’m an <b>unbeatable</b> Tic-Tac-Toe bot.\n"
        "• You are ❌, I am ⭕️\n"
        "• I play **perfectly** (Minimax) — so don't strain your tiny brain. You can only draw if you also play perfectly.\n\n"
        "Tap <b>New game</b> to start your inevitable loss.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎮 New game", callback_data="new")]
        ],),
        parse_mode=ParseMode.HTML
    )

@router.message(Command("newgame"))
async def on_newgame_cmd(m: Message, bot: Bot, workflow_data: Dict[str, Any]):
    """Handles the /newgame command."""
    username = m.from_user.username or m.from_user.first_name
    await start_new_game(m.chat.id, m.from_user.id, username, bot, workflow_data)

@router.callback_query(F.data == "new")
async def on_new(cq: CallbackQuery, bot: Bot, workflow_data: Dict[str, Any]):
    """Handles the 'New game' inline button click."""
    await cq.answer()
    username = cq.from_user.username or cq.from_user.first_name
    
    # Optional: Delete the old game state if it exists
    if cq.message and cq.message.message_id in workflow_data.get(GAME_STATE_KEY, {}):
        delete_game(cq.message.message_id, workflow_data)
        
    await start_new_game(cq.message.chat.id, cq.from_user.id, username, bot, workflow_data)
    
async def start_new_game(chat_id: int, user_id: int, username: str, bot: Bot, workflow_data: Dict[str, Any]):
    """Starts a new game and sends the initial message."""
    # Game ID: hex-encoded timestamp for uniqueness
    gid = hex(int(time.time() * 1000))[2:]
    board = EMPTY * 9
    status = "PLAY"

    txt = render_text(board, status, username)
    kb = board_keyboard(gid, board, status)
    
    try:
        msg = await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
        
        # Store the game state in memory
        game = (gid, chat_id, msg.message_id, user_id, username, board, status)
        set_game(game, workflow_data)
        
        logger.info(f"New game {gid} started. Stored with message_id {msg.message_id}")
    except Exception as e:
        logger.error(f"Failed to send new game message to chat {chat_id}: {e}")

@router.callback_query(F.data.startswith("botstart:"))
async def on_bot_start(cq: CallbackQuery, bot: Bot, workflow_data: Dict[str, Any]):
    """Handles the 'Bot starts' button click (initial move O)."""
    await cq.answer()
    _, gid = cq.data.split(":")
    
    if not cq.message: return

    game = get_game(cq.message.message_id, workflow_data)
    
    # CRITICAL: Validate click is from the correct user and game exists
    if not game or cq.from_user.id != game[3]: # game[3] is user_id
        if game and cq.from_user.id != game[3]: # Not the player
            await cq.answer(f"Hush, {cq.from_user.first_name}. This is not your game! Go start a /newgame.", show_alert=True)
        return
        
    gid, chat_id, message_id, user_id, username, board, status = game
    
    # Must be the very start of the game
    if board != EMPTY * 9 or status != "PLAY":
        return
        
    # --- Bot move (O) ---
    idx = best_move(board)
    board = board[:idx] + BOT + board[idx+1:]
    
    # Status should still be PLAY after first move
    status = resolve_status(board)
    
    # Update game state in memory (turn is implicitly Human's next)
    set_game((gid, chat_id, message_id, user_id, username, board, status), workflow_data)
    
    await edit_board(bot, message_id, game) # Use the latest game state from memory

# CRITICAL FIX: The bot's turn should be executed as a separate, non-blocking task 
# after the message edit for the human's move is successful.
@router.callback_query(F.data.startswith("mv:"))
async def on_move(cq: CallbackQuery, bot: Bot, workflow_data: Dict[str, Any]):
    """Handles a human's move and schedules the bot's counter-move."""
    await cq.answer()
    _, gid, sidx = cq.data.split(":")
    idx = int(sidx)
    
    if not cq.message: return

    # Retrieve and validate game state
    game = get_game(cq.message.message_id, workflow_data)
    if not game or cq.from_user.id != game[3]:
        if game and cq.from_user.id != game[3]:
            await cq.answer(f"Hush, {cq.from_user.first_name}. This is not your game! Go start a /newgame.", show_alert=True)
        return

    gid, chat_id, message_id, user_id, username, board, status = game
    
    # Validate: still playing, human's turn, cell empty
    if status != "PLAY" or not is_human_turn(board) or board[idx] != EMPTY:
        return
    
    # --- 1. Human move (X) and initial status check ---
    board_after_human = board[:idx] + HUMAN + board[idx+1:]
    status_after_human = resolve_status(board_after_human)
    
    # Update game state in memory immediately with human's move and new status
    game_after_human = (gid, chat_id, message_id, user_id, username, board_after_human, status_after_human)
    set_game(game_after_human, workflow_data)
    
    if status_after_human != "PLAY":
        # Game over after human move (Win or Draw)
        await edit_board(bot, message_id, game_after_human)
        # Delete game state from memory
        delete_game(message_id, workflow_data)
        return

    # --- 2. Update board to 'Bot is thinking...' ---
    # This gives immediate visual feedback before the bot calculates and moves
    await edit_board(bot, message_id, game_after_human)

    # --- 3. Schedule Bot move (O) as a separate task ---
    # Pass the current (human's) board state and the dispatcher's workflow_data
    asyncio.create_task(
        bot_move_task(bot, message_id, board_after_human, workflow_data)
    )

async def bot_move_task(bot: Bot, message_id: int, board_before_bot: str, workflow_data: Dict[str, Any]):
    """Calculates and executes the bot's move."""
    try:
        # Give a small artificial delay for the 'thinking' effect
        await asyncio.sleep(0.5) 

        # Retrieve the latest game state again in case it was modified (unlikely but safe)
        game = get_game(message_id, workflow_data)
        if not game:
            logger.warning(f"Game {message_id} vanished before bot could move.")
            return

        gid, chat_id, message_id, user_id, username, current_board, status = game

        # CRITICAL: Only proceed if the board hasn't changed since the human move
        if current_board != board_before_bot:
             logger.warning(f"Bot move skipped: Board state changed for game {gid}")
             return

        # Recalculate best move on the latest board state
        bot_idx = best_move(current_board)
        board_after_bot = current_board[:bot_idx] + BOT + current_board[bot_idx+1:]
        status_after_bot = resolve_status(board_after_bot)
        
        # Update game state in memory
        game_after_bot = (gid, chat_id, message_id, user_id, username, board_after_bot, status_after_bot)
        set_game(game_after_bot, workflow_data)
        
        # Edit message to show final board state
        await edit_board(bot, message_id, game_after_bot)

        if status_after_bot != "PLAY":
            # Delete game state from memory after game ends
            delete_game(message_id, workflow_data)

    except Exception as e:
        logger.error(f"Bot move task failed for game {message_id}: {e}")

@router.callback_query(F.data.startswith("noop:"))
async def on_noop(cq: CallbackQuery):
    """Handles clicks on non-action buttons (taken cells, bot's turn, etc.)."""
    
    # Check if the click is from the player of the game
    if not cq.message: return
    game = get_game(cq.message.message_id, cq.bot.get_dispatcher().workflow_data)
    
    if game and cq.from_user.id != game[3]:
        # User who did not start the game: show an alert
        await cq.answer(f"Hush, {cq.from_user.first_name}. This is not your game! Go start a /newgame.", show_alert=True)
    else:
        # Player clicked a non-actionable button (e.g., taken cell, bot's turn): answer silently
        # This prevents the loading spinner from hanging
        await cq.answer()
    
# -------------------------------------------------------------------
# Message Management
# -------------------------------------------------------------------
async def edit_board(bot: Bot, message_id: int, game: Game):
    """Edits the board message with the current game state."""
    gid, chat_id, message_id, user_id, username, board, status = game
    
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
        # 'Message is not modified' is expected if no visual change occurs
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Failed to edit message {message_id} in chat {chat_id}: {e}")

async def main():
    """Main entry point for the bot."""
    # Ensure the BOT_TOKEN is set before proceeding
    if not BOT_TOKEN:
         raise SystemExit("BOT_TOKEN is not configured.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    
    # Initialize the in-memory game state store
    dp.workflow_data[GAME_STATE_KEY] = {}
    
    logger.info("Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user interrupt.")
    except Exception as e:
        logger.error(f"An unexpected error occurred in main: {e}")
