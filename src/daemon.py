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
import tempfile
from pathlib import Path
from typing import Any, Optional

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
    )


# ---------------------------------------------------------------------------
# デーモンサーバー
# ---------------------------------------------------------------------------


class OpenClaudeDaemon:
    """Unix ソケットサーバーとして動作する常駐デーモン。"""

    def __init__(self) -> None:
        """セッション辞書とサーバー状態を初期化する。"""
        self._sessions: dict[str, str] = self._load_sessions()  # alias → sdk_session_id
        self._server: Optional[asyncio.AbstractServer] = None
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

        sdk_session_id = self._sessions.get(session_alias)

        options = ClaudeAgentOptions(
            setting_sources=["project"],
            permission_mode="bypassPermissions",
            cwd=str(BASE_DIR),
            include_partial_messages=True,
            resume=sdk_session_id,
        )

        current_model: Optional[str] = None
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
        current_model: Optional[str],
    ) -> None:
        """ResultMessage から完了シグナルを送信する。"""
        usage = getattr(message, "usage", None) or {}
        await self._send_json(
            writer,
            {
                "type": "done",
                "stop_reason": getattr(message, "stop_reason", "end_turn"),
                "model": current_model,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
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
        deleted_files: list[str] = []
        failed_files: list[str] = []

        for sdk_session_id in list(self._sessions.values()):
            if sdk_session_id:
                jsonl_path = CLAUDE_PROJECTS_DIR / f"{sdk_session_id}.jsonl"
                if jsonl_path.exists():
                    try:
                        jsonl_path.unlink()
                        deleted_files.append(jsonl_path.name)
                    except Exception as e:
                        failed_files.append(f"{jsonl_path.name}: {e}")

        self._sessions = {}
        self._save_sessions()

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

        sdk_session_id = self._sessions.get(session_alias)
        if sdk_session_id is None:
            await self._send_json(writer, {"type": "error", "message": f"Session not found: {session_alias}"})
            return

        deleted_file: Optional[str] = None
        failed: Optional[str] = None

        jsonl_path = CLAUDE_PROJECTS_DIR / f"{sdk_session_id}.jsonl"
        if jsonl_path.exists():
            try:
                jsonl_path.unlink()
                deleted_file = jsonl_path.name
            except Exception as e:
                failed = f"{jsonl_path.name}: {e}"

        del self._sessions[session_alias]
        self._save_sessions()

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

    def _read_session_stats(self, sdk_session_id: str) -> dict[str, Any]:
        """sdk_session_id に対応する JSONL から last_active と total_tokens を返す。"""
        jsonl_path = CLAUDE_PROJECTS_DIR / f"{sdk_session_id}.jsonl"
        last_active: Optional[str] = None
        total_tokens = 0

        if not jsonl_path.exists():
            return {"last_active": None, "total_tokens": 0}

        for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"[warn] Skipping malformed JSONL line in {jsonl_path.name}: {e}", file=sys.stderr, flush=True)
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
            if SESSIONS_JSON.exists():
                data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            print(f"[warn] Failed to load sessions: {e}", file=sys.stderr, flush=True)
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
            print(f"[warn] Failed to save sessions: {e}", file=sys.stderr, flush=True)

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
