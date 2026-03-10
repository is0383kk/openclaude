"""OpenClaude configuration constants."""
from pathlib import Path

BASE_DIR: Path = Path.home() / ".openclaude"

SOCKET_PATH: Path = BASE_DIR / "openclaude.sock"
PID_FILE: Path = BASE_DIR / "openclaude.pid"
DAEMON_LOG: Path = BASE_DIR / "daemon.log"

SESSIONS_DIR: Path = BASE_DIR / "sessions"
SESSIONS_JSON: Path = SESSIONS_DIR / "sessions.json"

DEFAULT_SESSION_ID: str = "main"
CONTEXT_WINDOW: int = 200000  # claude-sonnet-4-6
