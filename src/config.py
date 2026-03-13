"""OpenClaude configuration constants."""

from pathlib import Path

# ユーザーディレクトリ以下の .openclaude ディレクトリをベースに各種ファイルパスを定義
BASE_DIR: Path = Path.home() / ".openclaude"

# デーモン関連のファイルパス
SOCKET_PATH: Path = BASE_DIR / "openclaude.sock"
PID_FILE: Path = BASE_DIR / "openclaude.pid"
DAEMON_LOG: Path = BASE_DIR / "daemon.log"

# セッションデータ保存用ディレクトリ／ファイル／デフォルトセッションID
SESSIONS_DIR: Path = BASE_DIR / "sessions"
SESSIONS_JSON: Path = SESSIONS_DIR / "sessions.json"
DEFAULT_SESSION_ID: str = "main"

# プロジェクトごとにセッションを分けるためのディレクトリ。プロジェクト名はカレントディレクトリのパスを加工して生成。
_projects_dir_name = str(BASE_DIR).replace("/", "-").replace(".", "-")
CLAUDE_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects" / _projects_dir_name

CONTEXT_WINDOW: int = 200_000
