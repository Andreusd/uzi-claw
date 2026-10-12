# Antigravity CLI Telegram Bot Bridge

A secure, private Telegram Bot bridge connected directly to your local Antigravity CLI (`agy`) agent on this computer.

## Features

- **Strict User Authorization & Stealth Mode**: Whitelist verification ensures only your approved Telegram numeric user ID can communicate with the bot. Anyone not in the allowlist is completely and silently ignored—no replies, error messages, or reaction—making the bot appear dead to unauthorized users.
- **Session Continuity**: Multi-turn conversation support (`agy --conversation=<id>`) so the agent remembers previous prompts and context.
- **Real-Time Progress Streaming**: Live Telegram message updates showing agent thinking, tool executions (`run_command`, `write_to_file`, etc.), and streaming text preview.
- **Autopilot Toggle**: In-chat `/autopilot on|off` to toggle tool auto-approvals (`--dangerously-skip-permissions`).
- **Session Controls**: `/new` to reset context, `/cd <path>` to navigate project directories, `/recent` to view and clickable `👉 /resume_1` (or `/resume <id>`) to switch to previous conversations.
- **Background Daemon & Boot Startup**: Configured as a `systemd` user service with lingering enabled, launching on system boot without requiring an interactive graphical login.

---

## Directory Structure

```text
~/Documents/Projects/AntigravityTelegramBridge/
├── .env.example                # Template for configuration
├── .env                        # Local configuration & Telegram Token
├── authorized_users.txt.example# Template for authorized Telegram user IDs
├── requirements.txt            # Python dependencies
├── main.py                     # Bot entrypoint
├── antigravity-telegram.service # Systemd service unit definition
├── src/
│   ├── config.py               # Environment & path configuration
│   ├── auth.py                 # Authorization check & decorator
│   ├── antigravity_runner.py   # Subprocess runner for agy CLI streaming
│   ├── terminal_runner.py      # Subprocess runner for direct shell execution
│   ├── device_stats.py         # Hardware & system stats (battery, CPU, RAM, disk)
│   ├── session_manager.py      # Session state & history parser
│   ├── formatters.py           # Message chunker & rate-limited live updater
│   └── bot.py                  # Telegram command & message handlers
└── .venv/                      # Python virtual environment
```

---

## Setup & Configuration

### 1. Set Your Telegram Bot Token

Obtain a token from [@BotFather](https://t.me/botfather) and add it to `.env`:

```bash
# In ~/Documents/Projects/AntigravityTelegramBridge/.env:
TELEGRAM_TOKEN=123456789:ABCDefGhIJKlmNoPQRsTUVwxyZ
```

### 2. Configure Authorized Users

Copy the template and add your immutable Telegram numeric user ID to `authorized_users.txt`:

```bash
cp authorized_users.txt.example authorized_users.txt
```

```text
# In authorized_users.txt:
123456789
```

*(You can obtain your numeric user ID from [@userinfobot](https://t.me/userinfobot) or via the `/id` command if authorized).*

---

## Running the Bot

### Manual Execution (for testing):

```bash
cd ~/Documents/Projects/AntigravityTelegramBridge
./.venv/bin/python3 main.py
```

### Systemd Background Service (Boot Startup):

The service unit is already installed at `~/.config/systemd/user/antigravity-telegram.service`.

Once you have added your `TELEGRAM_TOKEN` to `.env`:

```bash
# Reload user systemd daemon
systemctl --user daemon-reload

# Enable and start the service
systemctl --user enable --now antigravity-telegram.service

# Check service status
systemctl --user status antigravity-telegram.service

# View live logs
journalctl --user -u antigravity-telegram.service -f
```

To stop or restart:
```bash
systemctl --user stop antigravity-telegram.service
systemctl --user restart antigravity-telegram.service
```

---

## Bot Commands

| Command | Description |
|---|---|
| *(Any Text)* | Sends the prompt directly to the Antigravity agent (or runs directly in shell if in `/terminal` mode) |
| `/interrupt` | Interrupts the currently executing prompt or task (equivalent to pressing `Esc` in `agy` CLI or `Ctrl+C` in shell) |
| `/cancel` | Cancels / stops the currently running agent task or terminal command |
| `/device` | Checks host device stats: battery percentage, power drain, remaining time, CPU load, RAM, swap, disk, and uptime (aliases: `/battery`, `/stats`) |
| `/status` | Shows session conversation ID, working directory, autopilot, terminal mode, and state |
| `/terminal` | Toggles direct terminal mode: executes all commands straight in `bash` without LLM; send `/terminal` or `exit` to return |
| `/usage` | Displays model quotas and limit remaining (equivalent to `agy /usage`) |
| `/new` | Resets conversation and starts a fresh session on the next prompt |
| `/cd <path>` | Changes the agent working directory |
| `/autopilot <on\|off>` | Toggles auto-approving tool permissions |
| `/mode <plan\|accept-edits\|clear>` | Sets the agent execution mode |
| `/recent` | Lists recent CLI conversations with live 🟢 `[Running]` indicators and clickable `/resume_1` links |
| `/resume [id\|number]` | Resumes conversation by ID or index from `/recent`; lists chats if invoked without arguments |
| `/help` | Displays available commands |
