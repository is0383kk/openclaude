"""OpenClaude API サーバー - FastAPI + uvicorn による HTTP エンドポイント。

外部から HTTP リクエストを受け付け、Unix ソケット経由でデーモンにメッセージを転送する。

エンドポイント:
    POST   /message               メッセージ送信（完全レスポンス）
    POST   /message/stream        メッセージ送信（SSE ストリーミング）
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
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

# モジュール実行 (python3 -m src.api) とスクリプト実行の両方をサポート
try:
    from .config import (
        DEFAULT_SESSION_ID,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
        WEBHOOK_PID_FILE,
        setup_logging,
    )
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import (
        DEFAULT_SESSION_ID,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
        WEBHOOK_PID_FILE,
        setup_logging,
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
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None


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


class CronAddRequest(BaseModel):
    """POST /cron のリクエストボディ。"""

    name: str | None = None
    schedule: str
    session_id: str = DEFAULT_SESSION_ID
    message: str = Field(min_length=1)


class CronJobResponse(BaseModel):
    """Cron ジョブ情報。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    schedule: str
    session_id: str
    message: str
    enabled: bool
    created_at: str
    last_run_at: str | None = None
    last_run_status: str | None = None


class CronListResponse(BaseModel):
    """GET /cron のレスポンスボディ。"""

    jobs: list[CronJobResponse]
    total: int


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


def _sse_event(data: dict[str, Any]) -> str:
    r"""Dict を SSE イベント文字列に変換する。

    Args:
        data: SSE イベントのペイロード。

    Returns:
        `data: {...}\\n\\n` 形式の SSE イベント文字列。
    """
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_query_payload(request: MessageRequest) -> dict[str, Any]:
    """MessageRequest からデーモンに送信するクエリペイロードを構築する。"""
    return {"type": "query", "session_id": request.session_id, "message": request.message}


async def _stream_message_generator(request: MessageRequest) -> AsyncGenerator[str, None]:
    r"""Unix ソケット経由でデーモンと通信し、SSE イベントを yield する。

    Args:
        request: セッション ID とメッセージを含むリクエスト。

    Yields:
        SSE フォーマットの文字列（`data: {...}\n\n`）。
    """
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        yield _sse_event({"type": "error", "message": f"Daemon is not running: {e}"})
        return

    try:
        writer.write((json.dumps(_build_query_payload(request), ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            resp = json.loads(line.decode("utf-8").strip())
            resp_type = resp.get("type")

            if resp_type == "chunk":
                yield _sse_event({"type": "chunk", "text": resp.get("text", "")})
            elif resp_type == "done":
                yield _sse_event(resp)
                break
            elif resp_type == "error":
                yield _sse_event({"type": "error", "message": resp.get("message", "Unknown error")})
                break
    finally:
        writer.close()
        await writer.wait_closed()


@app.post("/message/stream")
async def post_message_stream(request: MessageRequest) -> StreamingResponse:
    r"""デーモンにメッセージを転送し、SSE ストリーミングでレスポンスを返す。

    Args:
        request: セッション ID とメッセージを含むリクエスト。

    Returns:
        SSE ストリーミングレスポンス（`text/event-stream`）。
        各イベントは `data: {...}\n\n` 形式で、type は `chunk` / `done` / `error`。
    """
    return StreamingResponse(
        _stream_message_generator(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    chunks: list[str] = []
    done_resp: dict[str, Any] = {}

    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise HTTPException(status_code=503, detail=f"Daemon is not running: {e}") from e

    try:
        writer.write((json.dumps(_build_query_payload(request), ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            resp = json.loads(line.decode("utf-8").strip())
            resp_type = resp.get("type")

            if resp_type == "chunk":
                chunks.append(resp.get("text", ""))
            elif resp_type == "done":
                done_resp = resp
                break
            elif resp_type == "error":
                raise HTTPException(status_code=500, detail=resp.get("message", "Unknown error"))
    finally:
        writer.close()
        await writer.wait_closed()

    return MessageResponse(
        session_id=request.session_id,
        response="".join(chunks),
        stop_reason=done_resp.get("stop_reason"),
        model=done_resp.get("model"),
        input_tokens=done_resp.get("input_tokens"),
        output_tokens=done_resp.get("output_tokens"),
        total_cost_usd=done_resp.get("total_cost_usd"),
        num_turns=done_resp.get("num_turns"),
    )


@app.get("/cron")
async def get_cron() -> CronListResponse:
    """Cron ジョブ一覧を取得する。

    Returns:
        Cron ジョブ一覧と件数。

    Raises:
        HTTPException: デーモンが起動していない場合（503）またはエラーが発生した場合（500）。
    """
    resp = await _request_daemon({"type": "cron_list"})
    if resp.get("type") == "error":
        raise HTTPException(status_code=500, detail=resp.get("message", "Unknown error"))
    jobs = [CronJobResponse(**j) for j in resp.get("jobs", [])]
    return CronListResponse(jobs=jobs, total=len(jobs))


@app.post("/cron", status_code=201)
async def post_cron(request: CronAddRequest) -> CronJobResponse:
    """Cron ジョブを追加する。

    Args:
        request: cron 式・セッション ID・メッセージを含むリクエスト。

    Returns:
        追加された Cron ジョブ情報。

    Raises:
        HTTPException: 不正な cron 式（422）、デーモン未起動（503）、エラー（500）。
    """
    resp = await _request_daemon(
        {
            "type": "cron_add",
            "name": request.name,
            "schedule": request.schedule,
            "session_id": request.session_id,
            "message": request.message,
        }
    )
    if resp.get("type") == "error":
        msg = resp.get("message", "Unknown error")
        status_code = 422 if "invalid cron" in msg.lower() else 500
        raise HTTPException(status_code=status_code, detail=msg)
    return CronJobResponse.model_validate(resp)


@app.delete("/cron/{job_id}")
async def delete_cron(job_id: str) -> dict[str, str]:
    """Cron ジョブを削除する。

    Args:
        job_id: 削除するジョブの ID。

    Returns:
        削除されたジョブ ID を含む辞書。

    Raises:
        HTTPException: ジョブが見つからない場合（404）、デーモン未起動（503）、エラー（500）。
    """
    resp = await _request_daemon({"type": "cron_delete", "job_id": job_id})
    if resp.get("type") == "error":
        msg = resp.get("message", "Unknown error")
        status_code = 404 if "not found" in msg.lower() else 500
        raise HTTPException(status_code=status_code, detail=msg)
    return {"job_id": resp.get("job_id", job_id)}


@app.post("/cron/{job_id}/run")
async def run_cron(job_id: str) -> dict[str, str]:
    """Cron ジョブを手動で即時実行する。

    Args:
        job_id: 実行するジョブの ID。

    Returns:
        実行開始したジョブ ID を含む辞書。

    Raises:
        HTTPException: ジョブが見つからない場合（404）、デーモン未起動（503）、エラー（500）。
    """
    resp = await _request_daemon({"type": "cron_run", "job_id": job_id})
    if resp.get("type") == "error":
        msg = resp.get("message", "Unknown error")
        status_code = 404 if "not found" in msg.lower() else 500
        raise HTTPException(status_code=status_code, detail=msg)
    return {"job_id": resp.get("job_id", job_id), "status": "started"}


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
# エントリーポイント
# ---------------------------------------------------------------------------


async def _main(port: int) -> None:
    """API サーバーを起動してシャットダウンまで待機する。

    Args:
        port: API サーバーがリッスンするポート番号。
    """
    setup_logging()
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
