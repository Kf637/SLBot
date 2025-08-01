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

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# configure logging to match discord.py style and ensure early messages appear
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    force=True
)
 # Logger for this module
logger = logging.getLogger(__name__)
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
    # Attach to root logger so all info logs go to file
    logging.getLogger().addHandler(file_handler)
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
    "GUILD_ID":       GUILD_ID,
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


try:
    perms_path = os.path.join(os.path.dirname(__file__), 'permission.json')
    with open(perms_path, 'r') as f:
        # Load mapping of command names to list of allowed role IDs
        COMMAND_PERMISSIONS = json.load(f)
except Exception as e:
    # Fallback to empty permissions on error
    COMMAND_PERMISSIONS = {}
    print(f"Error loading permissions: {e}")

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
    print("Uncaught exception in asyncio loop:", context)
    traceback.print_exc()
loop.set_exception_handler(handle_loop_exception)

# Global error handler for events
@client.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"Error in event handler {event}:")
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


async def log_command(interaction: discord.Interaction):
    """Log slash command usage to webhook"""
    # Only log slash commands (ignore component interactions)
    if not getattr(interaction, 'command', None) or not getattr(interaction.command, 'name', None):
        return
    if not WEBHOOK_URL:
        return
    user = interaction.user
    # Extract command name: slash command preferred, fallback to original interaction metadata
    data = interaction.data if isinstance(interaction.data, dict) else {}
    if getattr(interaction, 'command', None) and getattr(interaction.command, 'name', None):
        cmd_name = interaction.command.name
    elif getattr(interaction.message, 'interaction_metadata', None) and getattr(interaction.message.interaction_metadata, 'name', None):
        cmd_name = interaction.message.interaction_metadata.name
    else:
        cmd_name = data.get('custom_id') or data.get('name') or 'unknown'
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
        print(f"Failed to send command log webhook: {e}")
    # Also log to file
    logger.info("Command used: %s by %s (%s); Roles: %s", cmd_name, user.name, user.id, role_str)
async def log_denied(interaction: discord.Interaction):
    """Log unauthorized attempts to webhook"""
    if not WEBHOOK_URL:
        return
    user = interaction.user
    # Determine command name: slash or component or original message context
    data = interaction.data if isinstance(interaction.data, dict) else {}
    cmd_obj = getattr(interaction, 'command', None)
    if cmd_obj and getattr(cmd_obj, 'name', None):
        cmd_name = cmd_obj.name
    else:
        cmd_name = data.get('name') or data.get('custom_id')
        if not cmd_name:
            orig = getattr(interaction.message, 'interaction_metadata', None)
            cmd_name = getattr(orig, 'name', None)
        if not cmd_name:
            cmd_name = 'unknown'
    # Determine user roles for logging (roles that the user has)
    roles = [r.name for r in getattr(user, 'roles', [])]
    role_str = ', '.join(roles) if roles else 'none'
    embed = {
        "title": "Unauthorized Attempt",
        "color": 0xff0000,
        "fields": [
            {"name": "User", "value": f"{user} ({user.id})", "inline": True},
            {"name": "Command", "value": cmd_name, "inline": True}
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    payload = {"embeds": [embed]}
    try:
        await asyncio.to_thread(requests.post, WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"Failed to send denied log webhook: {e}")

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    # Sync slash commands
    if GUILD:
        tree.copy_global_to(guild=GUILD)
        # Unregister legacy '/test' command if present
        try:
            tree.remove_command('runconsolecommand', guild=GUILD)
        except Exception:
            pass
        synced = await tree.sync(guild=GUILD)
        print(f'Synced {len(synced)} commands to guild {GUILD.id}.')
    else:
        # Unregister legacy '/test' global command
        try:
            tree.remove_command('runconsolecommand')
        except Exception:
            pass
        synced = await tree.sync()
        print(f'Synced {len(synced)} global commands.')
    print('Bot is ready!')

@tree.command(name='help', description='Displays a list of available bot commands')
async def help_command(interaction: discord.Interaction):
    """Provides a list of available commands to authorized users."""
    member = interaction.user
    # Permission check via JSON permissions
    if not has_permission(member, interaction.command.name):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    # Define embed pages
    page1_desc = (
        "**Available Commands:**\n"
        "/restartserver - Restarts the SCP:SL server\n"
        "/startserver - Starts the SCP:SL server\n"
        "/stopserver - Stops the SCP:SL server\n"
        "/setserverstate <private|public> - Set server mode (Server must be verified by Northwood, this removes or readds the server to the list)\n"
        "/restartnextround - Restarts after current round finishes\n"
        "/roundrestart - Restarts the current round immediately\n"
        "/softrestart - Soft restart with reconnect notice\n"
        "/fetchlogs - Fetch server console logs\n"
        "/onlineplayers - List online players currently connected\n"
        "/console <command> - Run a console command on the server\n"
        "/help - Show this help menu"
    )
    page2_desc = (
        "This bot allows authorized users to control an SCP:SL server directly from Discord.\n"
        "This bot works by sending commands into the tmux session with the name `scpsl` and capturing the output.\n"
        "Logs and outputs are sent back as messages or files."
    )
    page3_desc = (
        "• Developer: Kf637\n"
        "• Libraries: discord.py, python-dotenv, requests\n"
        "• Server control via tmux and subprocess\n"
        "• Source Code: [GitHub Repository](https://github.com/Kf637/SLBot)\n"
    )
    embed1 = discord.Embed(title="Commands", description=page1_desc, color=0x00ff00)
    embed2 = discord.Embed(title="Info", description=page2_desc, color=0x00ff00)
    embed3 = discord.Embed(title="Credits", description=page3_desc, color=0x00ff00)
    pages = [embed1, embed2, embed3]
    # Paginated view
    class HelpView(discord.ui.View):
        def __init__(self, author_id):
            super().__init__(timeout=60)
            self.page = 0
            self.author_id = author_id
            # initial button states
            self.previous.disabled = True

        @discord.ui.button(label='Previous', style=discord.ButtonStyle.secondary)
        async def previous(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            self.page = max(0, self.page - 1)
            button.disabled = (self.page == 0)
            self.next.disabled = False
            await button_interaction.response.edit_message(embed=pages[self.page], view=self)

        @discord.ui.button(label='Next', style=discord.ButtonStyle.primary)
        async def next(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            self.page = min(len(pages) - 1, self.page + 1)
            button.disabled = (self.page == len(pages) - 1)
            self.previous.disabled = False
            await button_interaction.response.edit_message(embed=pages[self.page], view=self)

    view = HelpView(interaction.user.id)
    # send initial embed page
    await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

"""Restart server command"""
@tree.command(name='restartserver', description='Restarts the SCP:SL server')
async def restartserver(interaction: discord.Interaction):
    """Stops and starts the tmux session for SCP:SL and verifies port binding"""
    # Permission check via JSON permissions
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
        await interaction.response.defer(thinking=True)
        # Step 1: Check if server process is running
        await interaction.edit_original_response(content="Checking if SCP:SL Server is running")
        if not is_scpsl_process_running():
            await interaction.edit_original_response(content="No server process found; nothing to restart.")
            restart_in_progress = False
            return
        # Step 2: Attempt shutdown
        await interaction.edit_original_response(content="Attempting to shutdown SCP:SL")
        await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", "scpsl", "exit", "Enter"])
        for _ in range(10):
            if not is_scpsl_process_running():
                break
            await asyncio.sleep(1)
        # Step 3: Starting server
        await asyncio.sleep(3)
        await interaction.edit_original_response(content="Starting SCP:SL Server")
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "new-session", "-d", "-s", "scpsl", "bash", "-c", "cd /home/steam/steamcmd/scpsl && ./LocalAdmin 7777"]
        )
        # Step 4: Poll for readiness
        await interaction.edit_original_response(content="Checking if SCP:SL Server is running")
        bound = False
        for _ in range(60):
            if is_scpsl_process_running():
                res = await asyncio.to_thread(
                    subprocess.run,
                    "ss -tuln | grep -q ':7777'",
                    shell=True
                )
                if res.returncode == 0:
                    bound = True
                    break
            await asyncio.sleep(1)
        if bound:
            await interaction.edit_original_response(content="Server started successfully.")
            print("[DEBUG] Server started successfully: port 7777 is bound.")
        else:
            await interaction.edit_original_response(content="Server restart timed out, something went wrong. Please check the server logs.")
            print("[DEBUG] Server restart timed out: port 7777 not bound after 60 seconds.")
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
        try:
            await interaction.followup.send(f"Error during restart: {e}")
            print(f"[ERROR] Exception during restart: {e}")
        except Exception:
            pass
    finally:
        restart_in_progress = False

# Start server command
@tree.command(name='startserver', description='Starts the SCP:SL server')
async def startserver(interaction: discord.Interaction):
    """Starts the tmux session for SCP:SL and verifies port binding"""
    # Permission check: ensure user has the required role
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await log_command(interaction)
    # Prevent starting if server process already running
    if is_scpsl_process_running():
        await interaction.response.send_message("Server is already running; please stop or restart instead.", ephemeral=True)
        return
    # Acknowledge command and allow processing
    await interaction.response.send_message("Starting server, please wait...", ephemeral=True)
    print(f"[DEBUG] User {member} ({member.id}) invoked startserver")
    # Start new tmux session with server: cd into directory then execute
    print("[DEBUG] Starting new tmux session 'scpsl'")
    start_res = await asyncio.to_thread(
        subprocess.run,
        [
            "tmux", "new-session", "-d", "-s", "scpsl",
            "bash", "-c",
            "cd /home/steam/steamcmd/scpsl && ./LocalAdmin 7777"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    print(f"[DEBUG] new-session stdout: '{start_res.stdout.decode().strip()}', stderr: '{start_res.stderr.decode().strip()}'")
    # Wait up to 60 seconds for the server to bind port 7777
    print("[DEBUG] Waiting up to 60s for port 7777 to bind")
    bound = False
    for i in range(60):
        print(f"[DEBUG] Check attempt {i+1}")
        grep_res = await asyncio.to_thread(
            subprocess.run,
            "ss -tuln | grep -q ':7777'",
            shell=True
        )
        if grep_res.returncode == 0:
            bound = True
            print(f"[DEBUG] Port 7777 bound on attempt {i+1}")
            break
        await asyncio.sleep(1)
    if bound:
        print("[DEBUG] Port 7777 is bound, server start succeeded")
        await interaction.edit_original_response(content="Server started successfully: port 7777 is bound.")
    else:
        print("[DEBUG] Port 7777 did not bind after 60s timeout")
        await interaction.edit_original_response(content="Server start timed out: port 7777 not bound after 60 seconds.")

# Stop server command
@tree.command(name='stopserver', description='Stops the SCP:SL server')
async def stopserver(interaction: discord.Interaction):
    """Stops the tmux session for SCP:SL and verifies port unbinding"""
    # Permission check: ensure user has the required role
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await log_command(interaction)
    # Acknowledge command and allow processing
    await interaction.response.send_message("Stopping server, please wait...", ephemeral=True)
    print(f"[DEBUG] User {member} ({member.id}) invoked stopserver")
    # Graceful shutdown: send 'exit' to tmux session
    print("[DEBUG] Sending 'exit' to tmux session 'scpsl'")
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", "scpsl", "exit", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # Wait for session to close
    await asyncio.sleep(5)
    # Check if session still exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if has_res.returncode == 0:
        print("[DEBUG] Session still active; force killing tmux session 'scpsl'")
        kill_res = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "kill-session", "-t", "scpsl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print(f"[DEBUG] kill-session stdout: '{kill_res.stdout.decode().strip()}', stderr: '{kill_res.stderr.decode().strip()}'")
    else:
        print("[DEBUG] Session exited cleanly after 'exit' command")
    # Wait up to 60 seconds for server process to exit
    print("[DEBUG] Waiting up to 60s for server process to exit")
    freed = False
    for i in range(60):
        print(f"[DEBUG] Check stop attempt {i+1}")
        # Check if the server process has exited
        if not is_scpsl_process_running():
            freed = True
            print(f"[DEBUG] Server process exited on attempt {i+1}")
            break
        await asyncio.sleep(1)
    if freed:
        await interaction.edit_original_response(content="Server stopped successfully: process is not running.")
    else:
        await interaction.edit_original_response(content="Server stop timed out: process still running after 60 seconds.")

# Set server state command
@tree.command(name='setserverstate', description='Set server to private or public mode')
@app_commands.describe(state='Choose mode: private or public')
@app_commands.choices(state=[
    app_commands.Choice(name='private', value='private'),
    app_commands.Choice(name='public', value='public')
])
async def setserverstate(interaction: discord.Interaction, state: app_commands.Choice[str]):
    """Attach to tmux session and run !private or !public"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await log_command(interaction)
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "Server is not running; please start the server first.", ephemeral=True
        )
        return
    # Defer response to allow editing later
    await interaction.response.defer(thinking=True)
    cmd = f"!{state.value}"
    # Send command in tmux
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", "scpsl", cmd, "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # Poll for confirmation for up to 5 seconds, checking every 0.2s
    confirmation = None
    expected = "hidden from the server list." if state.value == "private" else "visible on the server list."
    for _ in range(25):
        await asyncio.sleep(0.2)
        # Capture last 100 lines and join wrapped lines
        capture_res = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "capture-pane", "-pt", "scpsl", "-S", "-100", "-J"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        # Remove carriage returns so long lines aren’t cut off
        raw = capture_res.stdout.decode()
        lines = raw.replace('\r', '').splitlines()
        # Look for full confirmation
        confirmation = next(
            (line for line in lines if f"[{state.value}]" in line and expected in line),
            None
        )
        # Fallback: any tag contains state
        if not confirmation:
            confirmation = next(
                (line for line in lines if f"[{state.value}]" in line),
                None
            )
        if confirmation:
            break
    final = confirmation or f"No confirmation from server for {state.value}."
    # Clean timestamp
    clean = re.sub(r'^\[.*?\]\s*', '', final)
    await interaction.followup.send(clean)

# Restart next round command
@tree.command(name='restartnextround', description='Restarts the server after the current round is finished.')
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
        ["tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "Server is not running; please start the server first."
        )
        return
    # Acknowledge scheduling
    await interaction.response.send_message(
        "Scheduling server restart after next round...", ephemeral=True
    )
    # Send restart next round command to tmux
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", "scpsl", "restartnextround", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    await asyncio.sleep(1)
    # Confirm to user
    await interaction.edit_original_response(
        content="Server WILL restart after next round."
    )

# Round restart command
@tree.command(name='roundrestart', description='Restarts the current round.')
async def roundrestart(interaction: discord.Interaction):
    """Forces the round to restart immediately"""
    member = interaction.user
    # Permission check
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await log_command(interaction)
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if has_res.returncode != 0:
        await interaction.response.send_message("Server is not running; please start the server first.")
        return
    # Acknowledge
    await interaction.response.send_message("Forcing round restart...", ephemeral=True)
    # Send round restart command
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", "scpsl", "roundrestart", "Enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    await asyncio.sleep(1)
    # Confirm to user
    await interaction.edit_original_response(content="Round restart forced.")

# Soft restart command
@tree.command(name='softrestart', description='Restarts the server softly, notifying players to reconnect.')
async def softrestart(interaction: discord.Interaction):
    """Restarts the server but tells all players to reconnect after restart"""
    member = interaction.user
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.")
        return
    await log_command(interaction)
    # Verify server session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "has-session", "-t", "scpsl"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "Server is not running; please start the server first."
        )
        return
    # Acknowledge action
    await interaction.response.send_message("Soft restarting server, please wait...", ephemeral=True)
    # Send soft restart command to tmux
    await asyncio.to_thread(
        subprocess.run,
        ["tmux", "send-keys", "-t", "scpsl", "softrestart", "Enter"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    # Rapidly poll for confirmation over 5 seconds (25 x 0.2s)
    confirmation = None
    for _ in range(25):
        await asyncio.sleep(0.2)
        capture_res = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "capture-pane", "-pt", "scpsl"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        lines = capture_res.stdout.decode().splitlines()
        for line in lines:
            if "Server will softly restart" in line:
                confirmation = line
                break
        if confirmation:
            break
    # Send result to user
    final_msg = confirmation or "No soft restart confirmation from server."
    # Remove leading timestamp
    clean_msg = re.sub(r'^\[.*?\]\s*', '', final_msg)
    await interaction.edit_original_response(content=clean_msg)

# Fetch server console logs
@tree.command(name='fetchlogs', description='Gets server console logs')
async def fetchlogs(interaction: discord.Interaction):
    """Fetch last 2000 characters inline and upload last 10000 characters as a TXT file (ephemeral)."""
    member = interaction.user
    # If fetchlogs commands are globally disabled, treat as disabled
    if disable_fetchlogs:
        await interaction.response.send_message("This command has been disabled by the system administrator.", ephemeral=True)
        return
    # Permission check
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await log_command(interaction)
    # Verify tmux session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "has-session", "-t", "scpsl"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if has_res.returncode != 0:
        await interaction.response.send_message(
            "Server is not running; please start the server first.", ephemeral=True
        )
        return
    # Defer ephemeral response
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        # Capture last logs
        capture_res = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "capture-pane", "-pt", "scpsl", "-S", "-100000", "-J"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        raw = capture_res.stdout.decode().replace('\r', '')
        # Mask out IPv4 and IPv6 addresses except the server IP announcement
        ip_pattern = r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b|(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}"
        lines = raw.splitlines(True)
        new_lines = []
        for line in lines:
            # preserve server IP announcement line
            if "Your server IP address is " in line:
                new_lines.append(line)
                continue
            # preserve timestamp bracketed prefix
            if line.startswith('[') and ']' in line:
                idx = line.index(']') + 1
                prefix, body = line[:idx], line[idx:]
                new_lines.append(prefix + re.sub(ip_pattern, 'XXX.XXX.XXX.XXX', body))
            else:
                new_lines.append(re.sub(ip_pattern, 'XXX.XXX.XXX.XXX', line))
        masked = ''.join(new_lines)
        # Truncate inline snippet to fit within 2000-char limit including code fences
        fence = '```'
        # max characters inside code fences (2000 total limit minus fence overhead)
        max_inner = 2000 - (len(fence) * 2)
        snippet = masked[-max_inner:] if len(masked) > max_inner else masked
        full_logs = masked[-10000:] if len(masked) > 100000 else masked
        # Send inline snippet
        await interaction.followup.send(f"{fence}{snippet}{fence}", ephemeral=True)
        # Write full logs to a temporary file, send it, then delete
        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmpfile:
            tmpfile.write(full_logs.encode())
            tmpfile.flush()
        tmp_path = tmpfile.name
        await interaction.followup.send(
            file=discord.File(tmp_path, filename="scpsl_logs.txt"), ephemeral=True
        )
        # Remove the temporary file after sending
        os.remove(tmp_path)
        
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
        try:
            await interaction.followup.send(f"Error fetching logs: {e}", ephemeral=True)
        except Exception:
            pass

# Global error handler for slash commands
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    # If response not yet sent, send error message
    try:
        await interaction.response.send_message(
            f"An unexpected error occurred: {error}",
        )
    except Exception:
        # Fallback if response already sent
        await interaction.followup.send(
            f"An unexpected error occurred: {error}",
        )
    # Log the full traceback
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)

# Global error handler for slash commands
@tree.command(name='onlineplayers', description='Displays the current online players in the server')
async def onlineplayers(interaction: discord.Interaction):
    """Query the server for active players and display the list."""
    member = interaction.user
    # Permission check
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.")
        return
    await log_command(interaction)
    # Verify tmux session exists
    has_res = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "has-session", "-t", "scpsl"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if has_res.returncode != 0:
        await interaction.response.send_message("Server is not running; please start the server first.")
        return
    # Defer response
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        # Send players command
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", "scpsl", "players", "Enter"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Wait briefly for output
        await asyncio.sleep(0.5)
        # Capture recent pane output
        cap = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "capture-pane", "-pt", "scpsl", "-S", "-100", "-J"],
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
            content = f'No players online ({count}).'
            await interaction.followup.send(content, ephemeral=True)
            return
        # Extract players from the most recent header
        players = []
        start = idxs[-1] + 1
        for entry in lines[start:]:
            if not entry.strip():
                break
            # Strip leading timestamp and dash, but preserve leading underscores/dots in username
            # e.g., "[00:00:01] - .user" -> ".user"
            clean = re.sub(r'^\[.*?\]\s*-\s*', '', entry)
            # Only remove a dash+space if it is not followed by a dot or underscore (to avoid stripping from usernames)
            clean = re.sub(r'^- (?![._])', '', clean)
            players.append(clean.rstrip())
        # Deduplicate while preserving order
        players = list(dict.fromkeys(players))
        # Format response
        content = f'Online players ({count}):\n' + '\n'.join(players)
        await interaction.followup.send(content, ephemeral=True)
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
        await interaction.followup.send(f"Error retrieving players: {e}", ephemeral=True)

# Run arbitrary console command with admin confirmation
@tree.command(name='console', description='Run a console command on the server (admin only)')
@app_commands.describe(command='The console command to run')
async def console(interaction: discord.Interaction, command: str):
    member = interaction.user
    # If console commands are globally disabled, treat as disabled
    if disable_console:
        await interaction.response.send_message("This command has been disabled by the system administrator.", ephemeral=True)
        return
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    # Ask for confirmation with buttons
    class RunConsoleView(discord.ui.View):
        def __init__(self, cmd, author_id):
            super().__init__(timeout=60)
            self.cmd = cmd
            self.author_id = author_id
        @discord.ui.button(label='Confirm', style=discord.ButtonStyle.danger, custom_id='console')
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            # capture output before command
            before = await asyncio.to_thread(subprocess.run,
                ["tmux", "capture-pane", "-pt", "scpsl", "-S", "-1000", "-J"],
                stdout=subprocess.PIPE)
            before_lines = before.stdout.decode().replace('\r', '').splitlines()
            # execute command
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", "scpsl", self.cmd, "Enter"])
            await asyncio.sleep(5)
            # capture output after
            after = await asyncio.to_thread(subprocess.run,
                ["tmux", "capture-pane", "-pt", "scpsl", "-S", "-1000", "-J"],
                stdout=subprocess.PIPE)
            after_lines = after.stdout.decode().replace('\r', '').splitlines()
            # extract new lines after command
            new_lines = after_lines[len(before_lines):]
            if not new_lines:
                new_lines = ['<no new output>']
            # show full output if it fits, otherwise send file
            full_output = '\n'.join(new_lines)
            header = f"Executed: `{self.cmd}`\nConsole output ({len(new_lines)} new lines):\n"
            fence = '```'
            max_inline = 2000 - len(header) - len(fence)*2
            if len(full_output) <= max_inline:
                content = f"{header}{fence}{full_output}{fence}"
                await button_interaction.response.edit_message(content=content, view=None)
            else:
                # show last portion that fits
                snippet = full_output[-max_inline:]
                content = (
                    f"{header}Output too long ({len(new_lines)} lines); showing last characters and sending full output as file.\n"
                    f"{fence}{snippet}{fence}"
                )
                await button_interaction.response.edit_message(content=content, view=None)
                buf = io.BytesIO(full_output.encode())
                await button_interaction.followup.send(file=discord.File(buf, filename="command_output.txt"), ephemeral=True)
            await log_command(button_interaction)
        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.", ephemeral=True)
                return
            await button_interaction.response.edit_message(content="Cancelled.", view=None)
    view = RunConsoleView(command, interaction.user.id)
    await interaction.response.send_message(f"⚠️ Are you sure you want to run: `{command}`?", view=view, ephemeral=True)



"""System reboot command with confirmation view"""
@tree.command(name='systemreboot', description='Reboots the system, shutting down SCP:SL first if running')
async def systemreboot(interaction: discord.Interaction):
    member = interaction.user
    # Permission check
    if not has_permission(member, interaction.command.name):
        await log_denied(interaction)
        await interaction.response.send_message("You don't have permission to use this command.")
        return

    # Check if bot has sudo permissions
    if not os.access('/usr/bin/sudo', os.X_OK):
        await interaction.response.send_message(
            "An error occurred. Please contact the server administrator.",
            ephemeral=True
        )
        logging.error("System reboot command failed: bot does not have sudo permissions.")
        return

    # Confirmation view
    class SystemRebootView(discord.ui.View):
        def __init__(self, author_id):
            super().__init__(timeout=60)
            self.author_id = author_id

        @discord.ui.button(label='Confirm', style=discord.ButtonStyle.danger)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.")
                return
            # Step 1: Check server
            await log_command(button_interaction)
            await button_interaction.response.edit_message(content="Checking if SCP:SL is running...", view=None)
            if is_scpsl_process_running():
                await button_interaction.response.edit_message(content="Attempting to shutdown SCP:SL", view=None)
                await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", "scpsl", "exit", "Enter"] )
                await asyncio.sleep(10)
                if is_scpsl_process_running():
                    await button_interaction.response.edit_message(content="Failed to shutdown SCP:SL", view=None)
                    return
            # Step 2: Reboot
            await asyncio.sleep(3)
            await button_interaction.response.edit_message(content="Rebooting system, SCP:SL does not autostart", view=None)
            # Make bot appear offline before reboot
            await button_interaction.client.change_presence(status=discord.Status.invisible)
            await asyncio.to_thread(subprocess.run, ["sudo", "reboot"] )
            await log_command(button_interaction)

        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != self.author_id:
                await button_interaction.response.send_message("This button isn't for you.")
                return
            await button_interaction.response.edit_message(content="Cancelled.", view=None)

    view = SystemRebootView(interaction.user.id)
    await interaction.response.send_message(
        "⚠️ Are you sure you want to reboot the system? This will shutdown SCP:SL and reboot the host.",
        view=view,
        ephemeral=True
    )

# Start the bot
if __name__ == '__main__':
    client.run(TOKEN)
