"""OpenClaude CLI - コマンド引数の解析と実行。

コマンド一覧:
    openclaude start [--port PORT]
    openclaude stop
    openclaude restart [--port PORT]
    openclaude status
    openclaude logs [--tail N]
    openclaude sessions
    openclaude sessions cleanup
    openclaude sessions delete SESSION_ID
    openclaude [--session-id ID] --message TEXT
    openclaude [--session-id ID] -m TEXT
    echo "質問" | openclaude
    openclaude < file.txt
    cat file.txt | openclaude -m "これを要約して"
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, cast

try:
    from .config import CLAUDE_PROJECTS_DIR, DAEMON_LOG, DEFAULT_SESSION_ID, PID_FILE, SOCKET_PATH, WEBHOOK_DEFAULT_PORT
    from .daemon import get_daemon_status, start_daemon_process, stop_daemon_process
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import (
        CLAUDE_PROJECTS_DIR,
        DAEMON_LOG,
        DEFAULT_SESSION_ID,
        PID_FILE,
        SOCKET_PATH,
        WEBHOOK_DEFAULT_PORT,
    )
    from src.daemon import get_daemon_status, start_daemon_process, stop_daemon_process

_CRAB = "🦀"


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
def main() -> None:
    """CLI のエントリーポイント。"""
    cli = OpenClaudeCLI()
    cli.run()


# ---------------------------------------------------------------------------
# CLI クラス
# ---------------------------------------------------------------------------
class OpenClaudeCLI:
    """コマンドライン引数を解析してデーモン操作とメッセージ送信を行うクラス。"""

    def run(self) -> None:
        """引数を解析して対応するコマンドを実行する。"""
        parser = self._build_parser()
        args = parser.parse_args()

        if args.command == "start":
            self.cmd_start(getattr(args, "port", WEBHOOK_DEFAULT_PORT))
        elif args.command == "stop":
            self.cmd_stop()
        elif args.command == "restart":
            self.cmd_restart(getattr(args, "port", WEBHOOK_DEFAULT_PORT))
        elif args.command == "status":
            self.cmd_status()
        elif args.command == "logs":
            self.cmd_logs(getattr(args, "tail", None))
        elif args.command == "sessions":
            if getattr(args, "sessions_command", None) == "cleanup":
                asyncio.run(self.cmd_sessions_cleanup())
            elif getattr(args, "sessions_command", None) == "delete":
                asyncio.run(self.cmd_sessions_delete(args.session_id))
            else:
                asyncio.run(self.cmd_sessions())
        else:
            message = self._resolve_message(args.message)
            if message is not None:
                asyncio.run(self.cmd_message(args.session_id, message))
            else:
                parser.print_help()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="openclaude",
            description="OpenClaude - Resident AI Agent System",
        )

        subparsers = parser.add_subparsers(dest="command")
        start_parser = subparsers.add_parser("start", help="Start the OpenClaude daemon")
        start_parser.add_argument(
            "--port",
            type=int,
            default=WEBHOOK_DEFAULT_PORT,
            metavar="PORT",
            help=f"Port for the API server (default: {WEBHOOK_DEFAULT_PORT})",
        )
        subparsers.add_parser("stop", help="Stop the OpenClaude daemon")
        restart_parser = subparsers.add_parser("restart", help="Restart the OpenClaude daemon")
        restart_parser.add_argument(
            "--port",
            type=int,
            default=WEBHOOK_DEFAULT_PORT,
            metavar="PORT",
            help=f"Port for the API server (default: {WEBHOOK_DEFAULT_PORT})",
        )
        subparsers.add_parser("status", help="Show daemon status")
        logs_parser = subparsers.add_parser("logs", help="Show daemon log")
        logs_parser.add_argument(
            "--tail",
            type=int,
            default=None,
            metavar="N",
            help="Show last N lines (default: show all)",
        )
        sessions_parser = subparsers.add_parser("sessions", help="Manage conversation sessions")
        sessions_sub = sessions_parser.add_subparsers(dest="sessions_command")
        sessions_sub.add_parser("cleanup", help="Clean up all sessions")
        delete_parser = sessions_sub.add_parser("delete", help="Delete a specific session")
        delete_parser.add_argument("session_id", metavar="SESSION_ID", help="Session alias to delete")

        # 会話モード
        parser.add_argument(
            "--session-id",
            default=DEFAULT_SESSION_ID,
            metavar="SESSION_ID",
            help=f"Session identifier (default: {DEFAULT_SESSION_ID})",
        )
        parser.add_argument(
            "--message",
            "-m",
            default=None,
            metavar="MESSAGE",
            help="Message to send to the agent",
        )
        return parser

    # ------------------------------------------------------------------
    # デーモン管理コマンド
    # ------------------------------------------------------------------
    def cmd_start(self, port: int = WEBHOOK_DEFAULT_PORT) -> None:
        """デーモンと API サーバーを起動する。既に起動済みの場合はメッセージを表示して終了する。"""
        status, pid = get_daemon_status()
        if status == "running":
            print(f"OpenClaude is already running (PID: {pid})")
            return

        if status == "stale":
            print(f"Removing stale PID file (PID: {pid} is dead)...")
            PID_FILE.unlink(missing_ok=True)

        print("Starting OpenClaude daemon...")
        start_daemon_process(port)

        # ソケットファイルが現れるまで最大15秒待機
        for _ in range(150):
            time.sleep(0.1)
            if SOCKET_PATH.exists():
                status, pid = get_daemon_status()
                if status == "running":
                    print(f"OpenClaude started (PID: {pid})")
                    return

        print(
            "ERROR: Daemon did not start in time. Check daemon.log for details.",
            file=sys.stderr,
        )
        sys.exit(1)

    def cmd_stop(self) -> None:
        """デーモンを停止する。起動していない場合はメッセージを表示して終了する。"""
        status, _ = get_daemon_status()
        if status == "stopped":
            print("OpenClaude is not running.")
            return

        print("Stopping OpenClaude daemon...")
        ok = stop_daemon_process()
        if ok:
            # ソケットファイルが消えるまで最大5秒待機
            for _ in range(50):
                time.sleep(0.1)
                if not SOCKET_PATH.exists():
                    break
            print("OpenClaude stopped.")
        else:
            print("ERROR: Failed to stop OpenClaude.", file=sys.stderr)
            sys.exit(1)

    def cmd_restart(self, port: int = WEBHOOK_DEFAULT_PORT) -> None:
        """デーモンと API サーバーを再起動する。"""
        self.cmd_stop()
        time.sleep(0.5)
        self.cmd_start(port)

    def cmd_status(self) -> None:
        """デーモンの稼働状態を表示する。"""
        status, pid = get_daemon_status()
        if status == "running":
            print(f"OpenClaude is running (PID: {pid})")
        elif status == "stale":
            print(f"OpenClaude has a stale PID file (PID: {pid}, process not found).")
        else:
            print("OpenClaude is stopped.")

    def cmd_logs(self, tail: int | None = None) -> None:
        """デーモンログを表示する。"""
        if not DAEMON_LOG.exists():
            print("No log file found.", file=sys.stderr)
            return

        lines = DAEMON_LOG.read_text(encoding="utf-8").splitlines()
        if tail is not None:
            lines = lines[-tail:]
        print("\n".join(lines))

    # ------------------------------------------------------------------
    # セッションコマンド
    # ------------------------------------------------------------------

    async def cmd_sessions(self) -> None:
        """デーモンからセッション一覧を取得して表示する。"""
        sessions = await self._fetch_sessions()

        print(f"{_CRAB} OpenClaude\n")

        if not sessions:
            print("Sessions: 0")
            return

        print(f"Sessions: {len(sessions)}")
        print(f"Sessions Path: {CLAUDE_PROJECTS_DIR}\n")

        col_id = max(max(len(s["session_id"]) for s in sessions), 10)
        col_sdk = max(max(len(s.get("sdk_session_id") or "-") for s in sessions), 14)
        col_la = max(max(len(s.get("last_active") or "-") for s in sessions), 11)
        print(f"{'session-id':<{col_id}}  {'sdk_session_id':<{col_sdk}}  {'last_active':<{col_la}}  total_tokens")
        for s in sessions:
            alias = s["session_id"]
            sdk_id = s.get("sdk_session_id") or "-"
            last_active = s.get("last_active") or "-"
            total_tokens = s.get("total_tokens", 0)
            print(f"{alias:<{col_id}}  {sdk_id:<{col_sdk}}  {last_active:<{col_la}}  {total_tokens}")

    async def cmd_sessions_cleanup(self) -> None:
        """全セッションをクリーンアップする。"""
        if not self._is_daemon_up():
            print("OpenClaude daemon is not running.")
            return

        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
            writer.write((json.dumps({"type": "cleanup_sessions"}) + "\n").encode("utf-8"))
            await writer.drain()

            response = await self._read_json(reader)
            writer.close()
            await writer.wait_closed()

            if response.get("type") == "cleanup_done":
                count = response.get("deleted_count", 0)
                print(f"Cleaned up {count} session(s).")
                for f in response.get("failed", []):
                    print(f"  [warn] {f}", file=sys.stderr)
            elif response.get("type") == "error":
                print(f"ERROR: {response.get('message')}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    async def cmd_sessions_delete(self, session_id: str) -> None:
        """指定したセッションを削除する。"""
        if not self._is_daemon_up():
            print("OpenClaude daemon is not running.")
            return

        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
            writer.write((json.dumps({"type": "delete_session", "session_id": session_id}) + "\n").encode("utf-8"))
            await writer.drain()

            response = await self._read_json(reader)
            writer.close()
            await writer.wait_closed()

            if response.get("type") == "delete_done":
                print(f"Deleted session: {session_id}")
                if response.get("failed"):
                    print(f"  [warn] {response['failed']}", file=sys.stderr)
            elif response.get("type") == "error":
                print(f"ERROR: {response.get('message')}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    async def _fetch_sessions(self) -> list[dict[str, Any]]:
        """デーモンに接続してセッション一覧を取得する。デーモン未起動時は空リストを返す。"""
        if not self._is_daemon_up():
            return []

        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
            writer.write((json.dumps({"type": "sessions"}) + "\n").encode("utf-8"))
            await writer.drain()

            response = await self._read_json(reader)
            writer.close()
            await writer.wait_closed()

            if response.get("type") == "sessions_list":
                return cast(list[dict[str, Any]], response.get("sessions", []))
            return []
        except Exception:
            return []

    # ------------------------------------------------------------------
    # メッセージコマンド
    # ------------------------------------------------------------------

    async def cmd_message(self, session_id: str, message: str) -> None:
        """エージェントにメッセージを送信してレスポンスをストリーミング表示する。"""
        # デーモンが起動していなければ自動起動
        if not self._is_daemon_up():
            print("Starting OpenClaude daemon...")
            start_daemon_process()
            # ソケットが現れるまで待機
            for _ in range(150):
                await asyncio.sleep(0.1)
                if SOCKET_PATH.exists():
                    break
            else:
                print(
                    "ERROR: Daemon did not start. Check daemon.log for details.",
                    file=sys.stderr,
                )
                sys.exit(1)

        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            print(f"ERROR: Cannot connect to daemon: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            # ヘッダー表示
            print(f"{_CRAB} OpenClaude\uff08{session_id}\uff09")
            print("\u2502")
            print("\u25c7")

            # リクエスト送信
            request = {"type": "query", "session_id": session_id, "message": message}
            writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()

            # レスポンスをストリーミング受信
            while True:
                response = await self._read_json(reader)
                resp_type = response.get("type")

                if resp_type == "chunk":
                    text = response.get("text", "")
                    print(text, end="", flush=True)

                elif resp_type == "done":
                    print()  # 最終改行
                    break

                elif resp_type == "error":
                    print()
                    print(f"ERROR: {response.get('message')}", file=sys.stderr)
                    sys.exit(1)

                else:
                    logging.getLogger(__name__).debug("cmd_message: unknown response type: %s", resp_type)

        finally:
            writer.close()
            await writer.wait_closed()

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _resolve_message(self, message_arg: str | None) -> str | None:
        """コマンドライン引数と stdin からメッセージを解決する。

        stdin がパイプ/リダイレクトの場合は stdin の内容を読み込む。
        - message_arg が None の場合: stdin の内容をそのままメッセージにする。
        - message_arg がある場合: stdin の内容を前置きし、message_arg を後ろに結合する。
          例: `cat report.txt | openclaude -m "これを要約して"`
        """
        if sys.stdin.isatty():
            return message_arg

        stdin_text = sys.stdin.read().strip()
        if not stdin_text:
            return message_arg

        return stdin_text if message_arg is None else stdin_text + "\n\n" + message_arg

    def _is_daemon_up(self) -> bool:
        status, _ = get_daemon_status()
        return status == "running"

    async def _read_json(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        line = await reader.readline()
        if not line:
            return {}
        return cast(dict[str, Any], json.loads(line.decode("utf-8").strip()))
