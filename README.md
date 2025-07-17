# SLBot

**SLBot** is a Discord bot for managing an SCP: Secret Laboratory (SCP:SL) game server via Discord slash commands. It supports starting, stopping, restarting the server (soft/hard), managing rounds, fetching logs, issuing console commands, and even rebooting the host machine.

## Features

- `/startserver`, `/stopserver`, `/restartserver`, `/roundrestart`, `/restartnextround`, `/softrestart`
- `/setserverstate <private|public>` to toggle server visibility
- `/onlineplayers` lists active players
- `/fetchlogs` retrieves recent server console logs
- `/console <command>` executes admin console commands in tmux
- `/systemreboot` gracefully shuts down SCP:SL and reboots the host

## Prerequisites

- Python 3.10+ (tested on 3.12)
- A running SCP:SL server managed via `tmux` named `scpsl`, running on port 7777, with files at `/home/steam/steamcmd/scpsl`
- A Discord bot token and a server (guild) ID
- Optional: A Discord webhook URL for command logging

## Installation

1. Clone or copy the repository:
   ```bash
   git clone https://github.com/your-repo/SLBot.git
   cd SLBot
   ```
2. Create a virtual environment (recommended):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

1. Copy the example environment file and fill in your credentials:
   ```bash
   cp .env .env.local
   ```
   Edit `.env.local` and set:
   ```ini
   DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
   GUILD_ID=YOUR_DISCORD_SERVER_ID
   WEBHOOK_URL=YOUR_WEBHOOK_URL  # optional
   ```
2. Update `permission.json` with the Discord role IDs authorized for each command. Example:
   ```json
   {
     "restartserver": [123456789012345678],
     "restartnextround": [123456789012345678],
     ...
   }
   ```

## Usage

Run the bot with:
```bash
python bot.py
```

On startup, the bot will register slash commands in the configured guild. Once online, use `/help` in Discord to see available commands.

## Notes

- Ensure the bot has the `applications.commands` and `bot` scope in your Discord application settings.
- The server management commands rely on a `tmux` session named `scpsl`. Adjust the session name in `bot.py` if yours differs.
- The `/systemreboot` command requires the host user to have passwordless `sudo reboot` privileges.

## License

This project is licensed under the [MIT License](LICENSE).
