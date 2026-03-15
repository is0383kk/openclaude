<table>
  <thead>
      <tr>
          <th style="text-align:center">English</th>
          <th style="text-align:center"><a href="./README_cn.md">Chinese</a></th>
          <th style="text-align:center"><a href="./README_ja.md">日本語</a></th>
      </tr>
    </thead>
</table>

# 🦀OpenClaude — Claude Code-native personal AI assistant

A persistent AI agent system built with `claude-agent-sdk`. Operates based on Claude Code's `settings.json`.  
This project is inspired by [OpenClaw](https://github.com/openclaw/openclaw).  
Runs as a Unix socket server, accepting messages from the CLI and REST API and proxying them to Claude.

---

## Features

| Feature                                | Command / Endpoint                                           |
| -------------------------------------- | ------------------------------------------------------------ |
| Daemon start / stop / restart / status | `openclaude start/stop/restart/status`                       |
| Send message (streaming)               | `openclaude -m "message"`                                    |
| stdin / pipe input                     | `echo "question" \| openclaude`                              |
| View logs                              | `openclaude logs [--tail N]`                                 |
| Session management                     | `openclaude sessions`                                        |
| Cron job management                    | `openclaude cron add/list/delete/run`                        |
| HTTP REST API                          | `POST /message`, `POST /message/stream`, `GET /status`, etc. |
| Cron REST API                          | `GET /cron`, `POST /cron`, `DELETE /cron/{id}`, etc.         |

---

## Setup

### Prerequisites

- Linux / Windows (WSL2)
- Python >= 3.14
- [An environment where claude-agent-sdk is available](https://platform.claude.com/docs/en/agent-sdk/overview)

### Dependencies

| Package                    | Purpose                   |
| -------------------------- | ------------------------- |
| `claude-agent-sdk>=0.1.48` | Claude AI Agent SDK       |
| `fastapi>=0.115.0`         | REST API framework        |
| `uvicorn>=0.30.0`          | ASGI server               |
| `apscheduler>=3.10,<4`     | Cron job scheduler (v3.x) |

### Installation

```bash
git clone <repository-url> ~/.openclaude
cd ~/.openclaude
pip install -r requirements.txt
```

> **Note:** The project must be placed in `~/.openclaude/`.
> Since `src/config.py` uses `Path.home() / ".openclaude"` as the base path, it will not work in a different directory.

---

## Usage

### Daemon Management

```bash
# Start (default port: 28789)
openclaude start

# Start with a specific port
openclaude start --port 18789

# Stop
openclaude stop

# Restart
openclaude restart

# Check status
openclaude status

# View logs
openclaude logs           # full output
openclaude logs --tail 50 # last 50 lines
```

### Sending Messages

```bash
# Simple send
openclaude -m "prompt"

# Specify a session
openclaude --session-id work -m "prompt"

# stdin / pipe
echo "question" | openclaude
cat report.txt | openclaude -m "Summarize this"
git diff | openclaude -m "Review this diff"
```

### Session Management

```bash
# List sessions
openclaude sessions

# Delete all sessions
openclaude sessions cleanup

# Delete a specific session
openclaude sessions delete <session-id>
```

### Cron Jobs

```bash
# Add a job (runs every morning at 9:00)
openclaude cron add "0 9 * * *" --name "morning" --session main -m "Organize today's tasks"

# List jobs
openclaude cron list

# Run manually
openclaude cron run <job-id>

# Delete a job
openclaude cron delete <job-id>
```

### systemd Integration (if configured)

```bash
systemctl --user start openclaude
systemctl --user stop openclaude
systemctl --user status openclaude
```

---

## REST API

After starting the daemon, it is accessible at `http://localhost:28789` by default.

| Method   | Path              | Description                  |
| -------- | ----------------- | ---------------------------- |
| `POST`   | `/message`        | Send message (full response) |
| `POST`   | `/message/stream` | Send message (SSE streaming) |
| `GET`    | `/status`         | Daemon status and PID        |
| `GET`    | `/sessions`       | List sessions                |
| `DELETE` | `/sessions`       | Delete all sessions          |
| `DELETE` | `/sessions/{id}`  | Delete a specific session    |
| `GET`    | `/cron`           | List cron jobs               |
| `POST`   | `/cron`           | Add a cron job               |
| `DELETE` | `/cron/{id}`      | Delete a cron job            |
| `POST`   | `/cron/{id}/run`  | Run a cron job manually      |

---

## Architecture

```
CLI (openclaude)
  └── src/cli.py
        └── Communicates with daemon via Unix socket (~/.openclaude/openclaude.sock)

Daemon + API server (same process)
  ├── src/daemon.py  ── Unix socket server
  ├── src/api.py     ── FastAPI + uvicorn (REST API)
  └── src/cron.py    ── apscheduler-based scheduler
```

### File Structure

```
~/.openclaude/
  ├── src/
  │   ├── config.py    # File path constants and logging configuration
  │   ├── daemon.py    # Unix socket server and message handlers
  │   ├── api.py       # FastAPI REST API server
  │   ├── cron.py      # Cron job management (CronJob / CronScheduler)
  │   └── cli.py       # CLI entry point
  ├── sessions/
  │   └── sessions.json         # Session alias -> SDK session ID mapping
  ├── cron/
  │   ├── jobs.json             # Cron job definitions (persisted)
  │   └── runs/<job_id>.jsonl   # Execution history
  ├── openclaude.sock           # Unix socket (only while running)
  ├── openclaude.pid            # PID file (only while running)
  └── daemon.log                # Daemon log
```
