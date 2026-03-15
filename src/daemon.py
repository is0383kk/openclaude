"""OpenClaude デーモン - asyncio Unix ソケットサーバー。

セッション管理を行い、claude-agent-sdk へのリクエストをプロキシする。

起動方法:
    python3 -m src.daemon          (~/.openclaude/ から)
    python3 src/daemon.py          (sys.path 調整あり)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# モジュール実行 (python3 -m src.daemon) とスクリプト実行の両方をサポート
try:
    from .config import (
        BASE_DIR,
        CLAUDE_PROJECTS_DIR,
        DAEMON_LOG,
        PID_FILE,
        SESSIONS_DIR,
        SESSIONS_JSON,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
        setup_logging,
    )
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import (
        BASE_DIR,
        CLAUDE_PROJECTS_DIR,
        DAEMON_LOG,
        PID_FILE,
        SESSIONS_DIR,
        SESSIONS_JSON,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
        setup_logging,
    )


# ---------------------------------------------------------------------------
# デーモンサーバー
# ---------------------------------------------------------------------------


class OpenClaudeDaemon:
    """Unix ソケットサーバーとして動作する常駐デーモン。"""

    def __init__(self) -> None:
        """セッション辞書とサーバー状態を初期化する。"""
        self._sessions: dict[str, str] = self._load_sessions()  # alias → sdk_session_id
        self._server: asyncio.AbstractServer | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Unix ソケットサーバーを起動し、シャットダウンまで待機する。"""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # 前回実行時の古いソケットファイルを削除
        SOCKET_PATH.unlink(missing_ok=True)

        # Unix ソケットサーバーを起動
        self._server = await asyncio.start_unix_server(self.handle_client, path=str(SOCKET_PATH))

        # ソケット準備完了後に PID ファイルを書き込む
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

        # グレースフルシャットダウン用のシグナルハンドラーを登録
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        _logger.info("OpenClaude daemon started (PID: %d)", os.getpid())

        # シャットダウンイベントが発火するまで待機
        async with self._server:
            await self._shutdown_event.wait()

        # クリーンアップ
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        _logger.info("OpenClaude daemon stopped.")

    # ------------------------------------------------------------------
    # クライアントハンドラー
    # ------------------------------------------------------------------

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """クライアント接続を受け付け、リクエストの種別に応じてハンドラーに振り分ける。"""
        try:
            line = await reader.readline()
            if not line:
                return
            request = json.loads(line.decode("utf-8").strip())
            req_type = request.get("type")

            if req_type == "query":
                await self.handle_query(request, writer)
            elif req_type == "sessions":
                await self.handle_sessions(writer)
            elif req_type == "cleanup_sessions":
                await self.handle_cleanup_sessions(writer)
            elif req_type == "delete_session":
                await self.handle_delete_session(request, writer)
            elif req_type == "stop":
                await self.handle_stop(writer)
            else:
                await self._send_json(writer, {"type": "error", "message": f"Unknown type: {req_type}"})
        except json.JSONDecodeError as e:
            _logger.error("Invalid JSON from client: %s", e)
            try:
                await self._send_json(writer, {"type": "error", "message": f"Invalid JSON: {e}"})
            except Exception as send_err:
                _logger.debug("Failed to send JSON decode error to client: %s", send_err)
        except Exception as e:
            _logger.error("Unhandled error in handle_client: %s", e)
            try:
                await self._send_json(writer, {"type": "error", "message": str(e)})
            except Exception as send_err:
                _logger.debug("Failed to send error response to client: %s", send_err)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as close_err:
                _logger.debug("Failed to close writer: %s", close_err)

    # ------------------------------------------------------------------
    # リクエストハンドラー
    # ------------------------------------------------------------------

    async def handle_query(self, request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        """claude-agent-sdk を呼び出してレスポンスチャンクを CLI にストリーミングする。"""
        # 起動を速くするためにここでインポートする（ImportError メッセージも出しやすい）
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                query,
            )
            from claude_agent_sdk.types import StreamEvent
        except ImportError as e:
            await self._send_json(
                writer,
                {"type": "error", "message": f"claude_agent_sdk not installed: {e}"},
            )
            return

        session_alias = request.get("session_id", "main")
        user_message = request.get("message", "")

        if not user_message.strip():
            await self._send_json(writer, {"type": "error", "message": "Empty message"})
            return

        _logger.info("query: session=%s, message_len=%d", session_alias, len(user_message))
        sdk_session_id = self._sessions.get(session_alias)

        options = ClaudeAgentOptions(
            setting_sources=["project"],
            permission_mode="bypassPermissions",
            cwd=str(BASE_DIR),
            include_partial_messages=True,
            resume=sdk_session_id,
        )

        current_model: str | None = None
        full_text: str = ""
        has_stream_events: bool = False

        try:
            async for message in query(prompt=user_message, options=options):
                if hasattr(message, "subtype") and message.subtype == "init":
                    self._handle_init_event(message, session_alias)
                elif isinstance(message, StreamEvent):
                    full_text, has_stream_events = await self._handle_stream_event(
                        message, writer, full_text, has_stream_events
                    )
                elif isinstance(message, AssistantMessage):
                    current_model, full_text = await self._handle_assistant_message(
                        message, writer, has_stream_events, full_text
                    )
                elif isinstance(message, ResultMessage):
                    await self._handle_result_message(message, writer, current_model)
        except Exception as e:
            _logger.error("query error: session=%s, error=%s", session_alias, e)
            await self._send_json(writer, {"type": "error", "message": str(e)})

    def _handle_init_event(self, message: Any, session_alias: str) -> None:
        """セッション初期化メッセージから sdk_session_id を取得してメモリとファイルに保存する。"""
        new_id = (message.data or {}).get("session_id")
        if new_id:
            self._sessions[session_alias] = new_id
            self._save_sessions()

    async def _handle_stream_event(
        self,
        message: Any,
        writer: asyncio.StreamWriter,
        full_text: str,
        has_stream_events: bool,
    ) -> tuple[str, bool]:
        """StreamEvent からテキストチャンクを抽出してストリーミングする。"""
        event = message.event
        if event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                has_stream_events = True
                chunk = delta.get("text", "")
                if chunk:
                    full_text += chunk
                    await self._send_json(writer, {"type": "chunk", "text": chunk})
        return full_text, has_stream_events

    async def _handle_assistant_message(
        self,
        message: Any,
        writer: asyncio.StreamWriter,
        has_stream_events: bool,
        full_text: str,
    ) -> tuple[str | None, str]:
        """AssistantMessage からモデル情報を取得する。

        StreamEvent 未着時はフォールバックとして本文テキストをストリーミングする。
        """
        from claude_agent_sdk.types import TextBlock

        current_model: str | None = message.model if hasattr(message, "model") else None
        # StreamEvent が来なかった場合のフォールバック
        if not has_stream_events:
            for block in message.content:
                if isinstance(block, TextBlock):
                    full_text += block.text
                    await self._send_json(writer, {"type": "chunk", "text": block.text})
        return current_model, full_text

    async def _handle_result_message(
        self,
        message: Any,
        writer: asyncio.StreamWriter,
        current_model: str | None,
    ) -> None:
        """ResultMessage から完了シグナルを送信する。"""
        usage = getattr(message, "usage", None) or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        _logger.info(
            "query done: stop_reason=%s, model=%s, input_tokens=%d, output_tokens=%d",
            getattr(message, "stop_reason", "end_turn"),
            current_model,
            input_tokens,
            output_tokens,
        )
        await self._send_json(
            writer,
            {
                "type": "done",
                "stop_reason": getattr(message, "stop_reason", "end_turn"),
                "model": current_model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_cost_usd": getattr(message, "total_cost_usd", None),
                "num_turns": getattr(message, "num_turns", 0),
            },
        )

    async def handle_sessions(self, writer: asyncio.StreamWriter) -> None:
        """メモリ上のセッション一覧を JSON で返す。"""
        sessions = []
        for alias, sid in self._sessions.items():
            stats = self._read_session_stats(sid) if sid else {"last_active": None, "total_tokens": 0}
            sessions.append(
                {
                    "session_id": alias,
                    "sdk_session_id": sid,
                    "last_active": stats["last_active"],
                    "total_tokens": stats["total_tokens"],
                }
            )
        await self._send_json(writer, {"type": "sessions_list", "sessions": sessions})

    async def handle_stop(self, writer: asyncio.StreamWriter) -> None:
        """停止レスポンスを返した後、デーモンをシャットダウンする。"""
        await self._send_json(writer, {"type": "stopped"})
        # レスポンスのフラッシュを待ってからシャットダウン
        asyncio.get_running_loop().call_later(0.2, self._shutdown_event.set)

    async def handle_cleanup_sessions(self, writer: asyncio.StreamWriter) -> None:
        """全セッションのメモリ・sessions.json・JSONL ファイルを削除する。"""
        _logger.info("cleanup_sessions: start, count=%d", len(self._sessions))
        deleted_files: list[str] = []
        failed_files: list[str] = []

        for sdk_session_id in list(self._sessions.values()):
            if sdk_session_id:
                deleted, error = self._delete_session_jsonl(sdk_session_id)
                if deleted:
                    deleted_files.append(deleted)
                if error:
                    failed_files.append(error)

        self._sessions = {}
        self._save_sessions()

        _logger.info("cleanup_sessions: done, deleted=%d, failed=%d", len(deleted_files), len(failed_files))
        await self._send_json(
            writer,
            {
                "type": "cleanup_done",
                "deleted_count": len(deleted_files),
                "failed": failed_files,
            },
        )

    async def handle_delete_session(self, request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        """指定した alias のセッションのメモリ・sessions.json・JSONL ファイルを削除する。"""
        session_alias = request.get("session_id", "")

        if not session_alias:
            await self._send_json(writer, {"type": "error", "message": "session_id is required"})
            return

        _logger.info("delete_session: session=%s", session_alias)
        sdk_session_id = self._sessions.get(session_alias)
        if sdk_session_id is None:
            _logger.warning("delete_session: session not found: %s", session_alias)
            await self._send_json(writer, {"type": "error", "message": f"Session not found: {session_alias}"})
            return

        deleted_file, failed = self._delete_session_jsonl(sdk_session_id)

        del self._sessions[session_alias]
        self._save_sessions()

        _logger.info("delete_session: done, session=%s, deleted_file=%s", session_alias, deleted_file)
        await self._send_json(
            writer,
            {
                "type": "delete_done",
                "session_id": session_alias,
                "deleted_file": deleted_file,
                "failed": failed,
            },
        )

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _delete_session_jsonl(self, sdk_session_id: str) -> tuple[str | None, str | None]:
        """sdk_session_id に対応する JSONL ファイルを削除して (deleted_name, error_message) を返す。"""
        jsonl_path = CLAUDE_PROJECTS_DIR / f"{sdk_session_id}.jsonl"
        try:
            jsonl_path.unlink()
            return jsonl_path.name, None
        except FileNotFoundError:
            _logger.debug("JSONL already absent: %s", jsonl_path.name)
            return None, None
        except Exception as e:
            return None, f"{jsonl_path.name}: {e}"

    def _read_session_stats(self, sdk_session_id: str) -> dict[str, Any]:
        """sdk_session_id に対応する JSONL から last_active と total_tokens を返す。"""
        jsonl_path = CLAUDE_PROJECTS_DIR / f"{sdk_session_id}.jsonl"
        last_active: str | None = None
        total_tokens = 0

        try:
            lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return {"last_active": None, "total_tokens": 0}

        for raw in lines:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as e:
                _logger.warning("Skipping malformed JSONL line in %s: %s", jsonl_path.name, e)
                continue
            if "timestamp" in entry:
                last_active = entry["timestamp"]
            msg = entry.get("message")
            if isinstance(msg, dict) and msg.get("stop_reason"):
                usage = msg.get("usage") or {}
                total_tokens += usage.get("input_tokens", 0)
                total_tokens += usage.get("cache_creation_input_tokens", 0)
                total_tokens += usage.get("cache_read_input_tokens", 0)
                total_tokens += usage.get("output_tokens", 0)

        return {"last_active": last_active, "total_tokens": total_tokens}

    def _load_sessions(self) -> dict[str, str]:
        """sessions.json から alias → sdk_session_id を読み込む。"""
        try:
            data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except FileNotFoundError:
            _logger.debug("sessions.json not found, starting with empty sessions")
        except Exception as e:
            _logger.warning("Failed to load sessions: %s", e)
        return {}

    def _save_sessions(self) -> None:
        """self._sessions を sessions.json にアトミックに書き込む。"""
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(SESSIONS_DIR), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f, ensure_ascii=False)
            os.replace(tmp, str(SESSIONS_JSON))
        except Exception as e:
            _logger.warning("Failed to save sessions: %s", e)

    async def _send_json(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        line = json.dumps(data, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()


# ---------------------------------------------------------------------------
# プロセス管理 (cli.py から使用)
# ---------------------------------------------------------------------------


def start_daemon_process(port: int = WEBHOOK_DEFAULT_PORT) -> None:
    """デーモンをデタッチされたバックグラウンドプロセスとして起動する。

    Args:
        port: API サーバーがリッスンするポート番号。
    """
    python = sys.executable

    with open(str(DAEMON_LOG), "a") as log:
        subprocess.Popen(  # noqa: S603
            [python, "-m", "src.daemon", "--port", str(port)],
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def stop_daemon_process() -> bool:
    """ソケット経由で停止リクエストを送信するか、PID ファイルで SIGTERM を送信する。

    Returns:
        停止リクエストの送信に成功した場合は True。
    """
    # まずソケット経由で試みる
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(SOCKET_PATH))
        sock.sendall((json.dumps({"type": "stop"}) + "\n").encode("utf-8"))
        resp_raw = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp_raw += chunk
            if b"\n" in resp_raw:
                break
        sock.close()
        return True
    except Exception as e:
        _logger.debug("stop_daemon_process: socket stop failed, falling back to PID: %s", e)

    # フォールバック: PID ファイル経由で SIGTERM を送信
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError) as e:
        _logger.debug("stop_daemon_process: PID fallback failed: %s", e)

    return False


def get_daemon_status() -> tuple[str, int | None]:
    """デーモンのステータスと PID を返す。

    Returns:
        tuple: (status_string, pid_or_None)
            status_string は 'running', 'stopped', 'stale' のいずれか。
    """
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return "stopped", None

    # プロセスが生存しているか確認
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale", pid
    except PermissionError as e:
        _logger.debug("get_daemon_status: cannot signal PID %d (no permission): %s", pid, e)

    # ソケット接続を確認
    if SOCKET_PATH.exists():
        return "running", pid

    return "stale", pid


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------


async def _main(port: int) -> None:
    """デーモンと API サーバーを起動してシャットダウンまで待機する。

    Args:
        port: API サーバーがリッスンするポート番号。
    """
    # setting_sources=["project"] が .claude/settings.json を参照できるよう CWD を設定
    os.chdir(str(BASE_DIR))
    setup_logging()

    try:
        import uvicorn  # noqa: PLC0415

        from .api import app as api_app  # noqa: PLC0415
    except ImportError as e:
        _logger.warning("API server disabled (fastapi/uvicorn not installed): %s", e)
        daemon = OpenClaudeDaemon()
        await daemon.start()
        return

    # uvicorn がシグナルハンドラーを上書きしないようサブクラス化
    class _NoSignalServer(uvicorn.Server):
        def install_signal_handlers(self) -> None:
            pass  # daemon 側のシグナルハンドラーを維持するため何もしない

    daemon = OpenClaudeDaemon()
    api_config = uvicorn.Config(api_app, host="0.0.0.0", port=port, log_level="info")  # noqa: S104
    api_server = _NoSignalServer(api_config)

    async def _run_daemon() -> None:
        await daemon.start()
        api_server.should_exit = True  # daemon 停止後に API も停止

    _logger.info("OpenClaude API server will start on port %d", port)
    await asyncio.gather(_run_daemon(), api_server.serve())


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="OpenClaude Daemon")
    _parser.add_argument("--port", type=int, default=WEBHOOK_DEFAULT_PORT, metavar="PORT")
    _args = _parser.parse_args()
    asyncio.run(_main(_args.port))
