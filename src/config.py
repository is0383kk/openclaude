"""OpenClaude configuration constants."""

import logging
import sys
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

# Webhook サーバー関連のファイルパス／デフォルトポート
WEBHOOK_PID_FILE: Path = BASE_DIR / "webhook.pid"
WEBHOOK_DEFAULT_PORT: int = 28789

# Cron ジョブ関連のファイルパス
CRON_DIR: Path = BASE_DIR / "cron"
CRON_JOBS_FILE: Path = CRON_DIR / "jobs.json"
CRON_RUNS_DIR: Path = CRON_DIR / "runs"


# ---------------------------------------------------------------------------
# ロギング設定
# ---------------------------------------------------------------------------


def setup_logging() -> None:
    """デーモン・API サーバー共通のロギングを設定する。HH:MM:SS level メッセージ の形式で stdout に出力する。"""

    class _LowerLevelFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            original = record.levelname
            record.levelname = original.lower()
            result = super().format(record)
            record.levelname = original
            return result

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_LowerLevelFormatter(fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logging.root.setLevel(logging.INFO)
    logging.root.handlers = [handler]
