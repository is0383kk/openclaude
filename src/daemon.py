"""OpenClaude デーモン - asyncio Unix ソケットサーバー。

セッション管理を行い、claude-agent-sdk へのリクエストをプロキシする。

起動方法:
    python3 -m src.daemon          (~/.openclaude/ から)
    python3 src/daemon.py          (sys.path 調整あり)
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# モジュール実行 (python3 -m src.daemon) とスクリプト実行の両方をサポート
try:
    from .config import (
        BASE_DIR,
        CONTEXT_WINDOW,
        DAEMON_LOG,
        PID_FILE,
        SESSIONS_DIR,
        SOCKET_PATH,
    )
    from .session_store import SessionMeta, SessionStore
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import (
        BASE_DIR,
        CONTEXT_WINDOW,
        DAEMON_LOG,
        PID_FILE,
        SESSIONS_DIR,
        SOCKET_PATH,
    )
    from src.session_store import SessionMeta, SessionStore


# ---------------------------------------------------------------------------
# デーモンサーバー
# ---------------------------------------------------------------------------


class OpenClaudeDaemon:
    """Unix ソケットサーバーとして動作する常駐デーモン。"""

    def __init__(self) -> None:
        """セッションストアとサーバー状態を初期化する。"""
        self.session_store = SessionStore()
        self._server: Optional[asyncio.AbstractServer] = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Unix ソケットサーバーを起動し、シャットダウンまで待機する。"""
        # ディレクトリが存在することを確認
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # 既存のセッションメタデータを読み込む
        await self.session_store.load()

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

        print(f"OpenClaude daemon started (PID: {os.getpid()})", flush=True)

        # シャットダウンイベントが発火するまで待機
        async with self._server:
            await self._shutdown_event.wait()

        # クリーンアップ
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        print("OpenClaude daemon stopped.", flush=True)

    # ------------------------------------------------------------------
    # クライアントハンドラー
    # ------------------------------------------------------------------

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
            elif req_type == "stop":
                await self.handle_stop(writer)
            else:
                await self._send_json(
                    writer, {"type": "error", "message": f"Unknown type: {req_type}"}
                )
        except json.JSONDecodeError as e:
            try:
                await self._send_json(writer, {"type": "error", "message": f"Invalid JSON: {e}"})
            except Exception:  # noqa: S110
                pass
        except Exception as e:
            try:
                await self._send_json(writer, {"type": "error", "message": str(e)})
            except Exception:  # noqa: S110
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: S110
                pass

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

        meta = await self.session_store.get_session(session_alias)
        sdk_session_id = meta.sdk_session_id if meta else None

        options = ClaudeAgentOptions(
            setting_sources=["project"],
            permission_mode="bypassPermissions",
            cwd=str(BASE_DIR),
            include_partial_messages=True,
            resume=sdk_session_id,
        )

        # SDK に送信する前にユーザーメッセージをログに記録
        await self.session_store.append_message(session_alias, role="user", content=user_message)

        current_sdk_session_id: Optional[str] = sdk_session_id
        current_model: Optional[str] = None
        full_text: str = ""
        has_stream_events: bool = False

        try:
            async for message in query(prompt=user_message, options=options):
                if hasattr(message, "subtype") and message.subtype == "init":
                    meta, current_sdk_session_id = await self._handle_init_event(
                        message, session_alias, meta, current_sdk_session_id
                    )
                elif isinstance(message, StreamEvent):
                    full_text, has_stream_events = await self._handle_stream_event(
                        message, writer, full_text, has_stream_events
                    )
                elif isinstance(message, AssistantMessage):
                    current_model, full_text = await self._handle_assistant_message(
                        message, writer, has_stream_events, full_text
                    )
                elif isinstance(message, ResultMessage):
                    await self._handle_result_message(
                        message,
                        writer,
                        session_alias,
                        meta,
                        current_sdk_session_id,
                        current_model,
                        full_text,
                    )
        except Exception as e:
            await self._send_json(writer, {"type": "error", "message": str(e)})

    async def _handle_init_event(
        self,
        message: Any,
        session_alias: str,
        meta: Optional[SessionMeta],
        current_sdk_session_id: Optional[str],
    ) -> tuple[Optional[SessionMeta], Optional[str]]:
        """セッション初期化メッセージを処理してメタデータを更新する。"""
        new_id = (message.data or {}).get("session_id")
        if new_id:
            current_sdk_session_id = new_id
        now = datetime.now(timezone.utc).isoformat()
        init_meta = SessionMeta(
            session_id=session_alias,
            sdk_session_id=current_sdk_session_id,
            model=None,
            created_at=meta.created_at if meta else now,
            last_active_at=now,
            total_input_tokens=meta.total_input_tokens if meta else 0,
            total_output_tokens=meta.total_output_tokens if meta else 0,
            context_window=CONTEXT_WINDOW,
            num_turns=meta.num_turns if meta else 0,
        )
        await self.session_store.upsert_session(init_meta)
        return init_meta, current_sdk_session_id

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
    ) -> tuple[Optional[str], str]:
        """AssistantMessage からモデル情報を取得する。

        StreamEvent 未着時はフォールバックとして本文テキストをストリーミングする。
        """
        from claude_agent_sdk.types import TextBlock

        current_model: Optional[str] = message.model if hasattr(message, "model") else None
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
        session_alias: str,
        meta: Optional[SessionMeta],
        current_sdk_session_id: Optional[str],
        current_model: Optional[str],
        full_text: str,
    ) -> None:
        """ResultMessage を処理してセッションを更新し、完了シグナルを送信する。"""
        if not current_sdk_session_id and hasattr(message, "session_id"):
            current_sdk_session_id = message.session_id

        usage = getattr(message, "usage", None) or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_cost = getattr(message, "total_cost_usd", None)
        stop_reason = getattr(message, "stop_reason", "end_turn")
        num_turns = getattr(message, "num_turns", 0)

        now = datetime.now(timezone.utc).isoformat()
        updated_meta = SessionMeta(
            session_id=session_alias,
            sdk_session_id=current_sdk_session_id,
            model=current_model,
            created_at=meta.created_at if meta else now,
            last_active_at=now,
            total_input_tokens=(meta.total_input_tokens if meta else 0) + input_tokens,
            total_output_tokens=(meta.total_output_tokens if meta else 0) + output_tokens,
            context_window=CONTEXT_WINDOW,
            num_turns=(meta.num_turns if meta else 0) + num_turns,
        )
        await self.session_store.upsert_session(updated_meta)

        # アシスタントメッセージをログに記録
        await self.session_store.append_message(
            session_alias,
            role="assistant",
            content=full_text,
            model=current_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
        )

        # 完了シグナルを送信
        await self._send_json(
            writer,
            {
                "type": "done",
                "stop_reason": stop_reason,
                "model": current_model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_cost_usd": total_cost,
                "num_turns": num_turns,
            },
        )

    async def handle_sessions(self, writer: asyncio.StreamWriter) -> None:
        """セッション一覧を JSON で返す。"""
        sessions = await self.session_store.list_sessions()
        await self._send_json(
            writer,
            {
                "type": "sessions_list",
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "sdk_session_id": s.sdk_session_id,
                        "model": s.model,
                        "created_at": s.created_at,
                        "last_active_at": s.last_active_at,
                        "total_input_tokens": s.total_input_tokens,
                        "total_output_tokens": s.total_output_tokens,
                        "context_window": s.context_window,
                        "num_turns": s.num_turns,
                    }
                    for s in sessions
                ],
            },
        )

    async def handle_stop(self, writer: asyncio.StreamWriter) -> None:
        """停止レスポンスを返した後、デーモンをシャットダウンする。"""
        await self._send_json(writer, {"type": "stopped"})
        # レスポンスのフラッシュを待ってからシャットダウン
        asyncio.get_running_loop().call_later(0.2, self._shutdown_event.set)

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    async def _send_json(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        line = json.dumps(data, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()


# ---------------------------------------------------------------------------
# プロセス管理 (cli.py から使用)
# ---------------------------------------------------------------------------


def start_daemon_process() -> None:
    """デーモンをデタッチされたバックグラウンドプロセスとして起動する。"""
    python = sys.executable

    with open(str(DAEMON_LOG), "a") as log:
        subprocess.Popen(  # noqa: S603
            [python, "-m", "src.daemon"],
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
    import socket as _socket

    # まずソケット経由で試みる
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
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
    except Exception:  # noqa: S110
        pass

    # フォールバック: PID ファイル経由で SIGTERM を送信
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:  # noqa: S110
            pass

    return False


def get_daemon_status() -> tuple[str, Optional[int]]:
    """デーモンのステータスと PID を返す。

    Returns:
        tuple: (status_string, pid_or_None)
            status_string は 'running', 'stopped', 'stale' のいずれか。
    """
    if not PID_FILE.exists():
        return "stopped", None

    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return "stopped", None

    # プロセスが生存しているか確認
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale", pid
    except PermissionError:  # noqa: S110
        pass  # プロセスは存在するがシグナル送信権限がない

    # ソケット接続を確認
    if SOCKET_PATH.exists():
        return "running", pid

    return "stale", pid


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------


async def _main() -> None:
    # setting_sources=["project"] が .claude/settings.json を参照できるよう CWD を設定
    os.chdir(str(BASE_DIR))
    daemon = OpenClaudeDaemon()
    await daemon.start()


if __name__ == "__main__":
    asyncio.run(_main())
