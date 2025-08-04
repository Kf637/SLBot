import os
import discord
from discord import app_commands
from dotenv import load_dotenv, find_dotenv
import subprocess
import asyncio
import socket
import re  # for regex
import io
import requests
from datetime import datetime, timezone
import discord.ui
import json
import sys
import logging
import tempfile  # for creating temporary log file
from discord.ext import tasks

# Load environment variables
load_dotenv(find_dotenv())
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = os.getenv('GUILD_ID')
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
restart_in_progress = False  # Prevent overlapping restarts

# Feature flags: commands disabled by default unless explicitly set to 'false'
disable_console = os.getenv('DISABLE_CONSOLE', 'true') == 'true'
disable_fetchlogs = os.getenv('DISABLE_FETCHLOGS', 'true') == 'true'
disable_commands_usage_logging = os.getenv('DISABLE_COMMANDS_USAGE_LOGGING', 'true') == 'true'
# Toggle to disable Discord player status updates (set to 'true' to disable)
disable_player_update = os.getenv('DISABLE_DISCORD_PLAYERUPDATE', 'false').lower() == 'true'
# Environment variable for status embed channel ID
status_var = os.getenv('STATUS_CHANNEL_ID')
if not status_var:
    # Only require STATUS_CHANNEL_ID if player updates are enabled
    if not disable_player_update:
        logger.error("Missing required environment variable: STATUS_CHANNEL_ID\nIf you don't need player updates, set DISABLE_DISCORD_PLAYERUPDATE to 'true' in your .env file.")
        sys.exit(1)
    # Player updates disabled, no channel needed
    STATUS_CHANNEL_ID = None
else:
    STATUS_CHANNEL_ID = int(status_var)

# Set up intents for slash commands only
intents = discord.Intents.default()
intents.message_content = False  # Not needed for slash commands
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# Configure logging to match discord.py style and ensure early messages appear
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    force=True
)
logger = logging.getLogger(__name__)
command_logger = logging.getLogger('commandsusage')
command_logger.setLevel(logging.INFO)

# Setup commands usage logging if not disabled
if not disable_commands_usage_logging:
    # Define command usage log file path in script directory
    log_file_path = os.path.join(os.path.dirname(__file__), 'commandsusage.log')
    # Ensure command usage log file exists
    if not os.path.exists(log_file_path):
        with open(log_file_path, 'w') as f:
            f.write("")
    # Add file handler for command usage logging
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'))
    # Attach handler to command_logger so only command usage entries are recorded
    command_logger.addHandler(file_handler)
else:
    logger.info('Commands usage logging is disabled.')

# Prevent duplicate logs from discord.py by clearing its default handlers
discord_logger = logging.getLogger('discord')
discord_logger.handlers.clear()
# Raise discord logger level to WARNING to avoid duplicate INFO messages
discord_logger.setLevel(logging.WARNING)
# Prevent discord logger messages from propagating to root logger
discord_logger.propagate = False

# Validate required environment variables
_required = {
    "DISCORD_TOKEN": TOKEN,
    "GUILD_ID": GUILD_ID,
}
_missing = [name for name, val in _required.items() if not val]
if _missing:
    logger.error("Missing required environment variable(s): %s", ", ".join(_missing))
    sys.exit(1)

# Warn about disabled or unset optional features
if not WEBHOOK_URL:
    logger.warning("WEBHOOK_URL not set; command logging disabled.")

# Log feature flag states
if disable_console:
    logger.info("Console command is disabled.")
else:
    logger.info("Console command is enabled.")

if disable_fetchlogs:
    logger.info("Fetchlog command is disabled.")
else:
    logger.info("Fetchlog command is enabled.")

# Log player update feature flag state
if disable_player_update:
    logger.info("Discord player status updates are disabled.")
else:
    logger.info("Discord player status updates are enabled.")

try:
    perms_path = os.path.join(os.path.dirname(__file__), 'permission.json')
    with open(perms_path, 'r') as f:
        # Load mapping of command names to list of allowed role IDs
        COMMAND_PERMISSIONS = json.load(f)
except Exception as e:
    # Fallback to empty permissions on error
    COMMAND_PERMISSIONS = {}
    logger.error(f"Error loading permissions: {e}")

def has_permission(member: discord.Member, cmd_name: str) -> bool:
    """Check if member has roles allowed for this command"""
    allowed = COMMAND_PERMISSIONS.get(cmd_name, [])
    return isinstance(member, discord.Member) and any(r.id in allowed for r in member.roles)

# Catch unhandled exceptions in asyncio event loop
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

def handle_loop_exception(loop, context):
    import traceback
    logger.error("Uncaught exception in asyncio loop: %s", context)
    traceback.print_exc()

loop.set_exception_handler(handle_loop_exception)

# Global error handler for events
@client.event
async def on_error(event, *args, **kwargs):
    import traceback
    logger.error(f"Error in event handler {event}:")
    traceback.print_exc()

# Helper to check if any process is using a given TCP port
def is_port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('0.0.0.0', port))
        s.close()
        return False
    except OSError:
        return True
    
# Helper to check if the SCPSL server process is running
def is_scpsl_process_running() -> bool:
    """Return True if the SCPSL.x86_64 process is active."""
    return subprocess.run(
        ["pgrep", "-f", "SCPSL.x86_64"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode == 0
def is_port_bound(port: int) -> bool:
    """Return True if TCP port is bound (in use) on this host."""
    # Attempt to bind the port; if binding fails, the port is already bound by another process.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('0.0.0.0', port))
        s.close()
        return False
    except OSError:
        return True

async def log_command(interaction: discord.Interaction):
    """Log slash command usage to webhook"""
    # Only log slash commands (ignore component interactions)
    if not getattr(interaction, 'command', None) or not getattr(interaction.command, 'name', None):
        return
    if not WEBHOOK_URL:
        return
    
    user = interaction.user
    cmd_name = interaction.command.name
    
    # Determine user roles that match configured permissions for this command
    allowed_ids = COMMAND_PERMISSIONS.get(cmd_name, [])
    roles = [r.name for r in getattr(user, 'roles', []) if r.id in allowed_ids]
    role_str = ', '.join(roles) if roles else 'none'
    
    embed = {
        "title": "Command Used",
        "color": 0x00ff00,
        "fields": [
            {"name": "User", "value": f"{user} ({user.id})", "inline": True},
            {"name": "Command", "value": cmd_name, "inline": True},
            {"name": "Access Granted By Role", "value": role_str, "inline": True}
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    payload = {"embeds": [embed]}
    
    try:
        await asyncio.to_thread(requests.post, WEBHOOK_URL, json=payload)
    except Exception as e:
        logger.error(f"Failed to send command log webhook: {e}")
    
    # Also log to file via dedicated command_logger
    command_logger.info("Command used: %s by %s (%s); Roles: %s", cmd_name, user.name, user.id, role_str)

async def log_denied(interaction: discord.Interaction):
    """Log unauthorized attempts to webhook"""
    if not WEBHOOK_URL:
        return
    
    user = interaction.user
    cmd_name = getattr(interaction.command, 'name', 'unknown') if hasattr(interaction, 'command') else 'unknown'
    
    # Determine user roles for logging (roles that the user has)
    roles = [r.name for r in getattr(user, 'roles', [])]
    role_str = ', '.join(roles) if roles else 'none'
    
    embed = {
        "title": "Unauthorized Attempt",
        "color": 0xff0000,
        "fields": [
            {"name": "User", "value": f"{user} ({user.id})", "inline": True},
            {"name": "Command", "value": cmd_name, "inline": True},
            {"name": "User Roles", "value": role_str, "inline": True}
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    payload = {"embeds": [embed]}
    
    try:
        await asyncio.to_thread(requests.post, WEBHOOK_URL, json=payload)
    except Exception as e:
        logger.error(f"Failed to send denied log webhook: {e}")

@client.event
async def on_ready():
    logger.info(f'Logged in as {client.user} (ID: {client.user.id})')
    
    # Sync slash commands
    if GUILD:
        tree.copy_global_to(guild=GUILD)
        synced = await tree.sync(guild=GUILD)
        logger.info(f'Synced {len(synced)} slash commands to guild {GUILD.id}.')
    else:
        synced = await tree.sync()
        logger.info(f'Synced {len(synced)} global slash commands.')
    
    logger.info('Bot is ready and using slash commands only!')
    # Start background task to update presence with player count unless disabled
    if not disable_player_update:
        # Start background task to update status immediately and then every 120s
        update_status.start()
    else:
        logger.info("Discord player status updates are disabled via environment setting.")

# Get player amount and put it in the status loop
@tasks.loop(seconds=120)
async def update_status():
    await client.wait_until_ready()
    # Check if server process is running and fetch output
    if is_scpsl_process_running():
        # Send 'players' and capture the pane in one login shell to preserve session
        cmd = 'tmux send-keys -t scpsl players Enter; sleep 2; tmux capture-pane -pt scpsl -S -100 -J'
        capture = await asyncio.create_subprocess_exec(
            "sudo", "-i", "-u", "steam", "bash", "-lc", cmd,
            stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await capture.communicate()
        raw = stdout.decode(errors='ignore').replace('\r', '')
        # Strip ANSI codes and split into lines
        cleaned = re.sub(r'\x1b\[[0-9;]*m', '', raw)
        # Find all player count lines and pick the last one for current count
        lines = cleaned.splitlines()
        count_lines = [ln for ln in lines if 'List of players' in ln]
        # Only log the most recent count line
        if count_lines:
            last_line = count_lines[-1]
            logger.info(f"[DEBUG] tmux latest count line: {last_line}")
        if count_lines:
            # Reuse last_line from above
            m = re.search(r'List of players \((\d+)\)', last_line)
            count = int(m.group(1)) if m else 0
        else:
            count = 0
        logger.info(f"[DEBUG] Player count: {count}")
        # prepare status and embed parameters
        if count is not None:
            status_str = f"{count}/25 Players"
            color = 0x007bff if count > 0 else 0xffffff
            title = None
        else:
            status_str = "Server offline"
            color = 0xff0000
            title = "Server is currently offline"

        logger.info(f"[DEBUG] update_status: status='{status_str}'")

        # update bot presence
        await client.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=status_str
            )
        )

        # fetch channel and keep only the most recent bot message
        channel = client.get_channel(STATUS_CHANNEL_ID) or await client.fetch_channel(STATUS_CHANNEL_ID)
        recent = [msg async for msg in channel.history(limit=1) if msg.author == client.user]
        # delete any older bot messages beyond the first
        async for old in channel.history(limit=100):
            if old.author == client.user and (not recent or old.id != recent[0].id):
                await old.delete()

        # build embed
        if title:
            embed = discord.Embed(title=title, color=color)
        else:
            embed = discord.Embed(description=f"**{status_str}**", color=color)

        embed.set_author(name="Server Name Player Count")
        embed.set_footer(text="Last Updated")
        embed.timestamp = datetime.now(timezone.utc)

        # edit the existing message or send a new one
        if recent:
            await recent[0].edit(embed=embed)
        else:
            await channel.send(embed=embed)

@tree.command(name='help', description='Displays a list of available bot commands')
async def help_command(interaction: discord.Interaction):
    """Provides a list of available commands to authorized users."""
    member = interaction.user
    
    # Permission check via JSON permissions
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Define embed pages
    page1_desc = (
        "**Available Slash Commands:**\n"
        "</restartserver:0> - Restarts the SCP:SL server\n"
        "</startserver:0> - Starts the SCP:SL server\n"
        "</stopserver:0> - Stops the SCP:SL server\n"
        "</setserverstate:0> - Set server mode (private/public)\n"
        "</restartnextround:0> - Restarts after current round finishes\n"
        "</roundrestart:0> - Restarts the current round immediately\n"
        "</softrestart:0> - Soft restart with reconnect notice\n"
        "</fetchlogs:0> - Fetch server console logs\n"
        "</onlineplayers:0> - List online players currently connected\n"
        "</console:0> - Run a console command on the server\n"
        "</systemreboot:0> - Reboot the system\n"
        "</help:0> - Show this help menu"
    )
    
    page2_desc = (
        "This bot allows authorized users to control an SCP:SL server directly from Discord using slash commands.\n\n"
        "**How it works:**\n"
        "• All commands are slash commands (no prefix needed)\n"
        "• Bot sends commands to the tmux session named `scpsl`\n"
        "• Logs and outputs are sent back as messages or files\n"
        "• All interactions are logged for security"
    )
    
    page3_desc = (
        "• **Developer:** Kf637\n"
        "• **Libraries:** discord.py, python-dotenv, requests\n"
        "• **Server control:** tmux and subprocess\n"
        "• **Source Code:** [GitHub Repository](https://github.com/Kf637/SLBot)\n"
        "• **Command Type:** Slash Commands Only"
    )
    
    embed1 = discord.Embed(title="📋 Commands", description=page1_desc, color=0x00ff00)
    embed2 = discord.Embed(title="ℹ️ Information", description=page2_desc, color=0x00ff00)
    embed3 = discord.Embed(title="👨‍💻 Credits", description=page3_desc, color=0x00ff00)
    
    pages = [embed1, embed2, embed3]
    
    # Paginated view
    class HelpView(discord.ui.View):
        def __init__(self, author_id):
            super().__init__(timeout=60)
            self.page = 0
            self.author_id = author_id
            # Initial button states
            self.previous.disabled = True

        @discord.ui.button(label='◀️ Previous', style=discord.ButtonStyle.secondary)
        async def previous(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            self.page = max(0, self.page - 1)
            button.disabled = (self.page == 0)
            self.next.disabled = False
            await button_interaction.response.edit_message(embed=pages[self.page], view=self)

        @discord.ui.button(label='Next ▶️', style=discord.ButtonStyle.primary)
        async def next(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            self.page = min(len(pages) - 1, self.page + 1)
            button.disabled = (self.page == len(pages) - 1)
            self.previous.disabled = False
            await button_interaction.response.edit_message(embed=pages[self.page], view=self)

    view = HelpView(interaction.user.id)
    await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

@tree.command(name='restartserver', description='Restarts the SCP:SL server')
async def restartserver(interaction: discord.Interaction):
    """Stops and starts the tmux session for SCP:SL and verifies port binding"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    global restart_in_progress
    if restart_in_progress:
        await interaction.response.send_message("A restart is already in progress; please wait until it completes.", ephemeral=True)
        return
    
    restart_in_progress = True
    try:
        await log_command(interaction)
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Step 1: Check if server process is running
        await interaction.edit_original_response(content="🔍 Checking if SCP:SL Server is running...")
        if not is_scpsl_process_running():
            await interaction.edit_original_response(content="❌ No server process found; nothing to restart.")
            return
        
        # Step 2: Attempt shutdown
        await interaction.edit_original_response(content="🛑 Attempting to shutdown SCP:SL...")
        await asyncio.to_thread(subprocess.run, ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "exit", "Enter"])
        
        for _ in range(10):
            if not is_scpsl_process_running():
                break
            await asyncio.sleep(1)
        
        # Step 3: Starting server
        await asyncio.sleep(3)
        await interaction.edit_original_response(content="🚀 Starting SCP:SL Server...")
        await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-i", "-u", "steam", "tmux", "new-session", "-d", "-s", "scpsl", "bash", "-c", "cd /home/steam/steamcmd/scpsl && ./LocalAdmin 7777"]
        )
        
        # Step 4: Poll for readiness via process
        await interaction.edit_original_response(content="⏳ Waiting for server to start...")
        started = False
        for _ in range(60):
            if is_scpsl_process_running():
                started = True
                break
            await asyncio.sleep(1)

        if started:
            await interaction.edit_original_response(content="✅ Server restarted successfully!")
            logger.info("Server restarted successfully: SCPSL process is running.")
        else:
            await interaction.edit_original_response(content="⚠️ Server restart timed out. Please check the server logs.")
            logger.warning("Server restart timed out: SCPSL process not detected after 60 seconds.")
    
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
        try:
            await interaction.followup.send(f"❌ Error during restart: {e}")
            logger.error(f"Exception during restart: {e}")
        except Exception:
            pass
    finally:
        restart_in_progress = False

@tree.command(name='startserver', description='Starts the SCP:SL server')
async def startserver(interaction: discord.Interaction):
    """Starts the tmux session for SCP:SL and verifies port binding"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Prevent starting if server process already running
    if is_scpsl_process_running():
        await interaction.response.send_message("⚠️ Server is already running; please stop or restart instead.", ephemeral=True)
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    logger.info(f"User {member} ({member.id}) invoked startserver")
    
    # Start new tmux session with server
    logger.info("Starting new tmux session 'scpsl'")
    start_res = await asyncio.to_thread(
        subprocess.run,
        [
            "sudo", "-i", "-u", "steam", "tmux", "new-session", "-d", "-s", "scpsl",
            "bash", "-c", "cd /home/steam/steamcmd/scpsl && ./LocalAdmin 7777"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait up to 60 seconds for the server process to start
    await interaction.edit_original_response(content="⏳ Starting server, please wait...")
    started = False
    for i in range(60):
        if is_scpsl_process_running():
            started = True
            logger.info(f"SCPSL process detected on attempt {i+1}")
            break
        await asyncio.sleep(1)

    if started:
        logger.info("Server started successfully")
        await interaction.edit_original_response(content="✅ Server started successfully!")
    else:
        logger.warning("Server start timed out")
        await interaction.edit_original_response(content="⚠️ Server start timed out. Please check the logs.")

@tree.command(name='stopserver', description='Stops the SCP:SL server')
async def stopserver(interaction: discord.Interaction):
    """Stops the tmux session for SCP:SL and verifies process termination"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    await interaction.response.defer(thinking=True, ephemeral=True)
    logger.info(f"User {member} ({member.id}) invoked stopserver")
    
    # Graceful shutdown: send 'exit' to tmux session
    await interaction.edit_original_response(content="🛑 Stopping server...")
    await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "exit", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for session to close
    await asyncio.sleep(5)
    
    # Check if session still exists and force kill if needed
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if has_res.returncode == 0:
        logger.info("Session still active; force killing tmux session 'scpsl'")
        await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", "steam", "tmux", "kill-session", "-t", "scpsl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    
    # Wait up to 60 seconds for server process to exit
    freed = False
    for i in range(60):
        if not is_scpsl_process_running():
            freed = True
            logger.info(f"Server process exited on attempt {i+1}")
            break
        await asyncio.sleep(1)
    
    if freed:
        await interaction.edit_original_response(content="✅ Server stopped successfully!")
    else:
        await interaction.edit_original_response(content="⚠️ Server stop timed out. Process may still be running.")

@tree.command(name='setserverstate', description='Set server to private or public mode')
@app_commands.describe(state='Choose mode: private or public')
@app_commands.choices(state=[
    app_commands.Choice(name='🔒 Private', value='private'),
    app_commands.Choice(name='🌐 Public', value='public')
])
async def setserverstate(interaction: discord.Interaction, state: app_commands.Choice[str]):
    """Set server visibility state"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "❌ Server is not running; please start the server first.", ephemeral=True
        )
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    cmd = f"!{state.value}"
    
    # Send command in tmux
    await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", cmd, "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Poll for confirmation
    confirmation = None
    expected = "hidden from the server list." if state.value == "private" else "visible on the server list."
    
    for _ in range(25):
        await asyncio.sleep(0.2)
        capture_res = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", "steam", "-H", "tmux", "capture-pane", "-pt", "scpsl", "-S", "-100", "-J"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        raw = capture_res.stdout.decode()
        lines = raw.replace('\r', '').splitlines()
        
        confirmation = next(
            (line for line in lines if f"[{state.value}]" in line and expected in line),
            None
        )
        
        if not confirmation:
            confirmation = next(
                (line for line in lines if f"[{state.value}]" in line),
                None
            )
        
        if confirmation:
            break
    
    final = confirmation or f"No confirmation from server for {state.value}."
    clean = re.sub(r'^\[.*?\]\s*', '', final)
    
    icon = "🔒" if state.value == "private" else "🌐"
    await interaction.edit_original_response(content=f"{icon} {clean}")

@tree.command(name='restartnextround', description='Restarts the server after the current round is finished')
async def restartnextround(interaction: discord.Interaction):
    """Schedules a server restart after the current round finishes"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "❌ Server is not running; please start the server first.", ephemeral=True
        )
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    # Send restart next round command to tmux
    await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "restartnextround", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    await asyncio.sleep(1)
    await interaction.edit_original_response(content="⏳ Server WILL restart after next round finishes.")

@tree.command(name='roundrestart', description='Restarts the current round immediately')
async def roundrestart(interaction: discord.Interaction):
    """Forces the round to restart immediately"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message("❌ Server is not running; please start the server first.", ephemeral=True)
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    # Send round restart command
    await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "roundrestart", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    await asyncio.sleep(1)
    await interaction.edit_original_response(content="🔄 Round restart forced!")

@tree.command(name='softrestart', description='Restarts the server softly, notifying players to reconnect')
async def softrestart(interaction: discord.Interaction):
    """Restarts the server but tells all players to reconnect after restart"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "❌ Server is not running; please start the server first.", ephemeral=True
        )
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    # Send soft restart command to tmux
    await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "softrestart", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Poll for confirmation
    confirmation = None
    for _ in range(25):
        await asyncio.sleep(0.2)
        capture_res = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", "steam", "-H", "tmux", "capture-pane", "-pt", "scpsl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        lines = capture_res.stdout.decode().splitlines()
        for line in lines:
            if "Server will softly restart" in line:
                confirmation = line
                break
        if confirmation:
            break
    
    final_msg = confirmation or "No soft restart confirmation from server."
    clean_msg = re.sub(r'^\[.*?\]\s*', '', final_msg)
    await interaction.edit_original_response(content=f"🔄 {clean_msg}")

@tree.command(name='fetchlogs', description='Gets server console logs')
async def fetchlogs(interaction: discord.Interaction):
    """Fetch server console logs and send them as file"""
    member = interaction.user
    
    if disable_fetchlogs:
        await interaction.response.send_message("❌ This command has been disabled by the system administrator.", ephemeral=True)
        return
    
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Verify tmux session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "❌ Server is not running; please start the server first.", ephemeral=True
        )
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    try:
        # Capture last logs
        capture_res = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", "steam", "-H", "tmux", "capture-pane", "-pt", "scpsl", "-S", "-100000", "-J"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        raw = capture_res.stdout.decode().replace('\r', '')
        
        # Mask out IPv4 and IPv6 addresses except the server IP announcement
        ip_pattern = r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b|(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}"
        lines = raw.splitlines(True)
        new_lines = []
        
        for line in lines:
            # Preserve server IP announcement line
            if "Your server IP address is " in line:
                new_lines.append(line)
                continue
            # Preserve timestamp bracketed prefix
            if line.startswith('[') and ']' in line:
                idx = line.index(']') + 1
                prefix, body = line[:idx], line[idx:]
                new_lines.append(prefix + re.sub(ip_pattern, 'XXX.XXX.XXX.XXX', body))
            else:
                new_lines.append(re.sub(ip_pattern, 'XXX.XXX.XXX.XXX', line))
        
        masked = ''.join(new_lines)
        
        # Truncate inline snippet to fit within 2000-char Discord message limit including code fences and prefix
        fence = '```'
        prefix_template = "📄 **Console Logs (last {} chars):**\n"
        # Estimate prefix length using placeholder to account for dynamic digit length
        estimated_prefix_len = len(prefix_template.format(0))
        # Compute max snippet length to fit within Discord limit
        max_inner = 2000 - estimated_prefix_len - len(fence) * 2
        max_inner = max(0, max_inner)
        snippet = masked[-max_inner:] if len(masked) > max_inner else masked
        # Include entire masked logs in the full log file without truncation
        full_logs = masked
        prefix = prefix_template.format(len(snippet))
        # Send inline snippet within limits
        # Ensure content does not exceed Discord's 2000-character limit
        content = f"{prefix}{fence}{snippet}{fence}"
        if len(content) > 2000:
            # Trim snippet to fit under the limit
            over = len(content) - 2000
            snippet = snippet[over:]
            prefix = prefix_template.format(len(snippet))
            content = f"{prefix}{fence}{snippet}{fence}"
        await interaction.edit_original_response(content=content)
        
        # Write full logs to a temporary file, send it, then delete
        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpfile:
            tmpfile.write(full_logs.encode())
            tmpfile.flush()
        
        tmp_path = tmpfile.name
        await interaction.followup.send(
            content="📁 **Full logs file:**",
            file=discord.File(tmp_path, filename="scpsl_logs.txt"), 
            ephemeral=True
        )
        
        # Remove the temporary file after sending
        os.remove(tmp_path)
        
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
        try:
            await interaction.edit_original_response(content=f"❌ Error fetching logs: {e}")
        except Exception:
            pass

@tree.command(name='onlineplayers', description='Displays the current online players in the server')
async def onlineplayers(interaction: discord.Interaction):
    """Query the server for active players and display the list."""
    member = interaction.user
    
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    await log_command(interaction)
    
    # Verify tmux session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message("❌ Server is not running; please start the server first.", ephemeral=True)
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    try:
        # Send players command
        await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "players", "Enter"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        # Wait briefly for output
        await asyncio.sleep(0.5)
        
        # Capture recent pane output
        cap = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-u", "steam", "-H", "tmux", "capture-pane", "-pt", "scpsl", "-S", "-100", "-J"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        raw = cap.stdout.decode().replace('\r', '')
        lines = raw.splitlines()
        
        # Locate header and extract total player count
        idxs = [i for i, l in enumerate(lines) if 'List of players' in l]
        count = 0
        if idxs:
            mcount = re.search(r'List of players \((\d+)\)', lines[idxs[-1]])
            count = int(mcount.group(1)) if mcount else 0
        
        # If zero players, respond early
        if count == 0:
            await interaction.edit_original_response(content='👥 **No players online** (0)')
            return
        
        # Extract players from the most recent header
        players = []
        start = idxs[-1] + 1
        for entry in lines[start:]:
            if not entry.strip():
                break
            # Strip leading timestamp and dash, but preserve leading underscores/dots in username
            clean = re.sub(r'^\[.*?\]\s*-\s*', '', entry)
            clean = re.sub(r'^- (?![._])', '', clean)
            players.append(clean.rstrip())
        
        # Deduplicate while preserving order
        players = list(dict.fromkeys(players))
        
        # Format response
        player_list = '\n'.join(f"• {player}" for player in players)
        content = f'👥 **Online players ({count}):**\n{player_list}'
        
        await interaction.edit_original_response(content=content)
        
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
        await interaction.edit_original_response(content=f"❌ Error retrieving players: {e}")

@tree.command(name='console', description='Run a console command on the server (admin only)')
@app_commands.describe(command='The console command to run')
async def console(interaction: discord.Interaction, command: str):
    """Run arbitrary console command with admin confirmation"""
    member = interaction.user
    
    if disable_console:
        await interaction.response.send_message("❌ This command has been disabled by the system administrator.", ephemeral=True)
        return
    
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["sudo", "-u", "steam", "-H", "tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    if has_res.returncode != 0:
        await interaction.response.send_message("❌ Server is not running; please start the server first.", ephemeral=True)
        return
    
    # Ask for confirmation with buttons
    class RunConsoleView(discord.ui.View):
        def __init__(self, cmd, author_id):
            super().__init__(timeout=60)
            self.cmd = cmd
            self.author_id = author_id
        
        @discord.ui.button(label='✅ Confirm', style=discord.ButtonStyle.danger, custom_id='console')
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            
            await button_interaction.response.edit_message(content="⏳ Executing command...", view=None)
            
            # Capture output before command
            before = await asyncio.to_thread(subprocess.run, ["sudo", "-u", "steam", "-H", "tmux", "capture-pane", "-pt", "scpsl", "-S", "-1000", "-J"], stdout=subprocess.PIPE)
            before_lines = before.stdout.decode().replace('\r', '').splitlines()
            
            # Execute command
            await asyncio.to_thread(subprocess.run, ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", self.cmd, "Enter"])
            await asyncio.sleep(2)
            
            # Capture output after
            after = await asyncio.to_thread(subprocess.run, ["sudo", "-u", "steam", "-H", "tmux", "capture-pane", "-pt", "scpsl", "-S", "-1000", "-J"], stdout=subprocess.PIPE)
            after_lines = after.stdout.decode().replace('\r', '').splitlines()
            
            # Extract new lines after command
            new_lines = after_lines[len(before_lines):]
            if not new_lines:
                new_lines = ['<no new output>']
            
            # Show full output if it fits, otherwise send file
            full_output = '\n'.join(new_lines)
            header = f"**Executed:** `{self.cmd}`\n**Console output** ({len(new_lines)} new lines):\n"
            fence = '```'
            max_inline = 2000 - len(header) - len(fence)*2
            
            if len(full_output) <= max_inline:
                content = f"{header}{fence}{full_output}{fence}"
                await button_interaction.edit_original_response(content=content)
            else:
                # Show last portion that fits
                snippet = full_output[-max_inline:]
                content = (
                    f"{header}Output too long ({len(new_lines)} lines); showing last characters and sending full output as file.\n"
                    f"{fence}{snippet}{fence}"
                )
                await button_interaction.edit_original_response(content=content)
                buf = io.BytesIO(full_output.encode())
                await button_interaction.followup.send(
                    content="📁 **Full command output:**",
                    file=discord.File(buf, filename="command_output.txt"), 
                    ephemeral=True
                )
            
            await log_command(button_interaction)
        
        @discord.ui.button(label='❌ Cancel', style=discord.ButtonStyle.secondary)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            await button_interaction.response.edit_message(content="❌ Cancelled.", view=None)
    
    view = RunConsoleView(command, interaction.user.id)
    await interaction.response.send_message(f"⚠️ **Are you sure you want to run:** `{command}`?", view=view, ephemeral=True)

@tree.command(name='systemreboot', description='Reboots the system, shutting down SCP:SL first if running')
async def systemreboot(interaction: discord.Interaction):
    """System reboot command with confirmation view"""
    member = interaction.user
    
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    # Check if bot has sudo permissions
    if not os.access('/usr/bin/sudo', os.X_OK):
        await interaction.response.send_message(
            "❌ An error occurred. Please contact the server administrator.",
            ephemeral=True
        )
        logger.error("System reboot command failed: bot does not have sudo permissions.")
        return
    
    # Confirmation view
    class SystemRebootView(discord.ui.View):
        def __init__(self, author_id):
            super().__init__(timeout=60)
            self.author_id = author_id
        
        @discord.ui.button(label='🔄 Confirm Reboot', style=discord.ButtonStyle.danger)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            
            await log_command(button_interaction)
            await button_interaction.response.edit_message(content="🔍 Checking if SCP:SL is running...", view=None)
            
            if is_scpsl_process_running():
                await button_interaction.edit_original_response(content="🛑 Attempting to shutdown SCP:SL...")
                await asyncio.to_thread(subprocess.run, ["sudo", "-u", "steam", "-H", "tmux", "send-keys", "-t", "scpsl", "exit", "Enter"])
                await asyncio.sleep(10)
                if is_scpsl_process_running():
                    await button_interaction.edit_original_response(content="❌ Failed to shutdown SCP:SL")
                    return
            
            await asyncio.sleep(3)
            await button_interaction.edit_original_response(content="🔄 Rebooting system... Bot will go offline.")
            
            # Make bot appear offline before reboot
            await client.change_presence(status=discord.Status.invisible)
            await asyncio.to_thread(subprocess.run, ["sudo", "reboot"])
        
        @discord.ui.button(label='❌ Cancel', style=discord.ButtonStyle.secondary)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            await button_interaction.response.edit_message(content="❌ Cancelled.", view=None)
    
    view = SystemRebootView(interaction.user.id)
    await interaction.response.send_message(
        "⚠️ **Are you sure you want to reboot the system?**\nThis will shutdown SCP:SL and reboot the host.",
        view=view,
        ephemeral=True
    )

# Global error handler for slash commands
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    """Handle slash command errors"""
    logger.error(f"Slash command error in {interaction.command}: {error}")
    
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ An unexpected error occurred: {error}",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ An unexpected error occurred: {error}",
                ephemeral=True
            )
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")
    
    # Log the full traceback
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)

# Get player amount and put it in the status


# Start the bot
if __name__ == '__main__':
    logger.info("Starting Discord bot with slash commands only...")
    client.run(TOKEN)
