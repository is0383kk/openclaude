"""OpenClaude API サーバー - FastAPI + uvicorn による HTTP エンドポイント。

外部から HTTP リクエストを受け付け、Unix ソケット経由でデーモンにメッセージを転送する。

エンドポイント:
    POST   /message               メッセージ送信（完全レスポンス）
    GET    /status                デーモンステータスと PID
    GET    /sessions              セッション一覧
    DELETE /sessions              全セッション削除
    DELETE /sessions/{session_id} 指定セッション削除

起動方法:
    python3 -m src.api          (~/.openclaude/ から)
    python3 -m src.api --port 8080
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

# モジュール実行 (python3 -m src.api) とスクリプト実行の両方をサポート
try:
    from .config import (
        BASE_DIR,
        DAEMON_LOG,
        DEFAULT_SESSION_ID,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
        WEBHOOK_PID_FILE,
    )
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import (
        BASE_DIR,
        DAEMON_LOG,
        DEFAULT_SESSION_ID,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
        WEBHOOK_PID_FILE,
    )


# ---------------------------------------------------------------------------
# Pydantic モデル
# ---------------------------------------------------------------------------


class MessageRequest(BaseModel):
    """POST /message のリクエストボディ。"""

    session_id: str = DEFAULT_SESSION_ID
    message: str = Field(min_length=1)


class MessageResponse(BaseModel):
    """POST /message のレスポンスボディ。"""

    session_id: str
    response: str
    stop_reason: str | None = None


class SessionInfo(BaseModel):
    """セッション情報。"""

    session_id: str
    sdk_session_id: str | None = None
    last_active: str | None = None
    total_tokens: int = 0


class SessionsResponse(BaseModel):
    """GET /sessions のレスポンスボディ。"""

    sessions: list[SessionInfo]
    total: int


class CleanupResponse(BaseModel):
    """DELETE /sessions のレスポンスボディ。"""

    deleted_count: int
    failed: list[str] = []


class DeleteSessionResponse(BaseModel):
    """DELETE /sessions/{session_id} のレスポンスボディ。"""

    session_id: str
    deleted_file: str | None = None
    failed: str | None = None


class StatusResponse(BaseModel):
    """GET /status のレスポンスボディ。"""

    status: str
    pid: int


# ---------------------------------------------------------------------------
# FastAPI アプリ
# ---------------------------------------------------------------------------

app = FastAPI(title="OpenClaude API", version="0.1.0")


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


async def _request_daemon(payload: dict[str, Any]) -> dict[str, Any]:
    """Unix ソケット経由でデーモンに JSON リクエストを送信し、単一レスポンスを返す。

    Args:
        payload: デーモンに送信する JSON ペイロード。

    Returns:
        デーモンからの JSON レスポンス。

    Raises:
        HTTPException: デーモンが起動していない場合（503）。
    """
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise HTTPException(status_code=503, detail=f"Daemon is not running: {e}") from e

    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise HTTPException(status_code=503, detail="Empty response from daemon")
        return json.loads(line.decode("utf-8").strip())  # type: ignore[no-any-return]
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------


@app.post("/message")
async def post_message(request: MessageRequest) -> MessageResponse:
    """デーモンにメッセージを転送し、完全なレスポンスを返す。

    Args:
        request: セッション ID とメッセージを含むリクエスト。

    Returns:
        デーモンからの完全なレスポンス。

    Raises:
        HTTPException: デーモンが起動していない場合（503）またはデーモンがエラーを返した場合（500）。
    """
    response_text = ""
    stop_reason: str | None = None

    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise HTTPException(status_code=503, detail=f"Daemon is not running: {e}") from e

    try:
        payload = {
            "type": "query",
            "session_id": request.session_id,
            "message": request.message,
        }
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            resp = json.loads(line.decode("utf-8").strip())
            resp_type = resp.get("type")

            if resp_type == "chunk":
                response_text += resp.get("text", "")
            elif resp_type == "done":
                stop_reason = resp.get("stop_reason")
                break
            elif resp_type == "error":
                raise HTTPException(status_code=500, detail=resp.get("message", "Unknown error"))
    finally:
        writer.close()
        await writer.wait_closed()

    return MessageResponse(
        session_id=request.session_id,
        response=response_text,
        stop_reason=stop_reason,
    )


@app.get("/status")
async def get_status() -> StatusResponse:
    """デーモンのステータスと PID を返す。

    Returns:
        デーモンのステータス（常に running）と PID。
    """
    return StatusResponse(status="running", pid=os.getpid())


@app.get("/sessions")
async def get_sessions() -> SessionsResponse:
    """デーモンからセッション一覧を取得して返す。

    Returns:
        セッション一覧と件数。

    Raises:
        HTTPException: デーモンが起動していない場合（503）またはエラーが発生した場合（500）。
    """
    resp = await _request_daemon({"type": "sessions"})
    if resp.get("type") == "error":
        raise HTTPException(status_code=500, detail=resp.get("message", "Unknown error"))
    sessions = [SessionInfo(**s) for s in resp.get("sessions", [])]
    return SessionsResponse(sessions=sessions, total=len(sessions))


@app.delete("/sessions")
async def cleanup_sessions() -> CleanupResponse:
    """全セッションを削除する。

    Returns:
        削除したセッション数と失敗一覧。

    Raises:
        HTTPException: デーモンが起動していない場合（503）またはエラーが発生した場合（500）。
    """
    resp = await _request_daemon({"type": "cleanup_sessions"})
    if resp.get("type") == "error":
        raise HTTPException(status_code=500, detail=resp.get("message", "Unknown error"))
    return CleanupResponse(
        deleted_count=resp.get("deleted_count", 0),
        failed=resp.get("failed", []),
    )


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> DeleteSessionResponse:
    """指定したセッションを削除する。

    Args:
        session_id: 削除するセッションの alias。

    Returns:
        削除結果（セッション ID・削除ファイル名・失敗理由）。

    Raises:
        HTTPException: セッションが見つからない場合（404）、デーモン未起動（503）、エラー（500）。
    """
    resp = await _request_daemon({"type": "delete_session", "session_id": session_id})
    if resp.get("type") == "error":
        msg = resp.get("message", "Unknown error")
        status_code = 404 if "not found" in msg.lower() else 500
        raise HTTPException(status_code=status_code, detail=msg)
    return DeleteSessionResponse(
        session_id=resp.get("session_id", session_id),
        deleted_file=resp.get("deleted_file"),
        failed=resp.get("failed"),
    )


# ---------------------------------------------------------------------------
# ロギング設定
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    """API サーバー用のロギングを設定する。HH:MM:SS level メッセージ の形式で stdout に出力する。"""

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


# ---------------------------------------------------------------------------
# プロセス管理 (cli.py から使用)
# ---------------------------------------------------------------------------


def start_webhook_process(port: int = WEBHOOK_DEFAULT_PORT) -> None:
    """API サーバーをデタッチされたバックグラウンドプロセスとして起動する。

    Args:
        port: API サーバーがリッスンするポート番号。
    """
    python = sys.executable
    with open(str(DAEMON_LOG), "a") as log:
        subprocess.Popen(  # noqa: S603
            [python, "-m", "src.api", "--port", str(port)],
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def stop_webhook_process() -> bool:
    """PID ファイル経由で API サーバーに SIGTERM を送信する。

    Returns:
        停止シグナルの送信に成功した場合は True。
    """
    if not WEBHOOK_PID_FILE.exists():
        return False
    try:
        pid = int(WEBHOOK_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def get_webhook_status() -> tuple[str, Optional[int]]:
    """API サーバーのステータスと PID を返す。

    Returns:
        tuple: (status_string, pid_or_None)
            status_string は 'running', 'stopped', 'stale' のいずれか。
    """
    if not WEBHOOK_PID_FILE.exists():
        return "stopped", None
    try:
        pid = int(WEBHOOK_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return "stopped", None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale", pid
    except PermissionError:  # noqa: S110
        pass  # プロセスは存在するがシグナル送信権限がない

    return "running", pid


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------


async def _main(port: int) -> None:
    """API サーバーを起動してシャットダウンまで待機する。

    Args:
        port: API サーバーがリッスンするポート番号。
    """
    _setup_logging()
    WEBHOOK_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    _logger.info("OpenClaude API server starting on port %d (PID: %d)", port, os.getpid())

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")  # noqa: S104
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        WEBHOOK_PID_FILE.unlink(missing_ok=True)
        _logger.info("OpenClaude API server stopped.")


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="OpenClaude API Server")
    _parser.add_argument("--port", type=int, default=WEBHOOK_DEFAULT_PORT, metavar="PORT")
    _args = _parser.parse_args()
    asyncio.run(_main(_args.port))
