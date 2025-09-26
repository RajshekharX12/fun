import os
import subprocess
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuration ---
# Your credentials
TOKEN = "8287015753:AAEKTs-RERTK869d1gAZ_l2DKdwHJmBJhrM"
ALLOWED_USER_ID = 7940894807  # Your owner chat ID, used for security

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

def get_spec(command, fallback="N/A"):
    """Runs a shell command and returns a clean string, or a fallback value."""
    try:
        # Use shell=True carefully here as commands are predefined and input is restricted
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=True,
            timeout=5 # Added timeout for safety
        )
        # Use .strip() to remove leading/trailing whitespace/newlines
        return result.stdout.strip()
    except Exception:
        # Return fallback on error (e.g., command not found, timeout)
        return fallback

async def check_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple authentication check to restrict commands to the owner."""
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        logging.warning(f"Unauthorized access attempt by user ID: {user_id}")
        await update.message.reply_text("⛔️ **ACCESS DENIED**. This bot is restricted to the owner only.", parse_mode='Markdown')
        return False
    return True

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greets the user and gives instructions."""
    user = update.effective_user
    if user.id == ALLOWED_USER_ID:
        msg = (
            f"👋 Hello, **{user.first_name}** (Owner).\n"
            f"I am ready to check your VPS specs.\n\n"
            f"Use the command: **/specs**"
        )
    else:
        msg = "👋 Hello. This bot is for the server owner's use only."
        
    await update.message.reply_text(msg, parse_mode='Markdown')

async def specs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /specs command to get and send VPS information."""
    if not await check_auth(update, context):
        return
    
    await update.message.reply_text("🔎 Gathering system information... please wait a moment.")

    # --- Run Spec Commands (All calls moved to variables for clean f-string use) ---
    
    # OS Information
    os_info = get_spec("cat /etc/os-release 2>/dev/null | grep '^NAME=' | cut -d'\"' -f2")
    kernel_version = get_spec("uname -r")
    
    # CPU and Core Count
    cpu_model = get_spec("lscpu 2>/dev/null | grep 'Model name' | awk -F': ' '{print $2}' | head -n 1")
    cpu_cores = get_spec("nproc --all")

    # Memory
    ram_total = get_spec("free -h | awk '/Mem:/ {print $2}'")
    ram_used = get_spec("free -h | awk '/Mem:/ {print $3}'")
    # FIX: Get RAM free in MB and assign to a variable
    ram_free_mb = get_spec("free -m | awk '/Mem:/ {print $4}'") 

    # Disk
    disk_total = get_spec("df -h / | awk 'NR==2 {print $2}'")
    disk_used = get_spec("df -h / | awk 'NR==2 {print $3}'")
    disk_free = get_spec("df -h / | awk 'NR==2 {print $4}'")

    # Uptime
    uptime = get_spec("uptime -p | cut -d' ' -f2-")

    # --- Create Emoji Breakdown (All variables are clean and ready) ---
    specs_message = (
        f"🤖 **VPS Status Report**\n"
        f"─────────────────────\n"
        f"🌐 **OS:** {os_info} \n"
        f"🛠️ **Kernel:** {kernel_version}\n\n"
        
        f"🧠 **CPU:** {cpu_cores} Cores\n"
        f"  *Model:* {cpu_model}\n\n"
        
        f"💾 **RAM:** {ram_used} / {ram_total} Used\n"
        f"  *Free Space (MB):* {ram_free_mb}\n\n"
        
        f"💽 **Disk:** {disk_used} / {disk_total} Used\n"
        f"  *Free Space:* {disk_free}\n\n"
        
        f"⏰ **Uptime:** {uptime}"
    )

    await update.message.reply_text(specs_message, parse_mode='Markdown')


def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("specs", specs_command))

    # Run the bot
    print("Bot is running and listening for commands...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
