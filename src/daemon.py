"""
OpenClaude Daemon - asyncio Unix socket server.

Handles session management and proxies requests to claude-agent-sdk.

Run via:
    python3 -m src.daemon          (from ~/.openclaude/)
    python3 src/daemon.py          (with sys.path adjustments below)
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

# Allow running as both a module (python3 -m src.daemon) and a script
try:
    from .config import BASE_DIR, CONTEXT_WINDOW, DAEMON_LOG, PID_FILE, SESSIONS_DIR, SOCKET_PATH
    from .session_store import SessionMeta, SessionStore
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import BASE_DIR, CONTEXT_WINDOW, DAEMON_LOG, PID_FILE, SESSIONS_DIR, SOCKET_PATH
    from src.session_store import SessionMeta, SessionStore


# ---------------------------------------------------------------------------
# Daemon server
# ---------------------------------------------------------------------------

class OpenClaudeDaemon:
    def __init__(self) -> None:
        self.session_store = SessionStore()
        self._server: Optional[asyncio.AbstractServer] = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the Unix socket server and run until shutdown."""
        # Ensure directories exist
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing session metadata
        await self.session_store.load()

        # Remove stale socket file from previous run
        SOCKET_PATH.unlink(missing_ok=True)

        # Start Unix socket server
        self._server = await asyncio.start_unix_server(
            self.handle_client, path=str(SOCKET_PATH)
        )

        # Write PID file after socket is ready
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        print(f"OpenClaude daemon started (PID: {os.getpid()})", flush=True)

        # Run until shutdown event
        async with self._server:
            await self._shutdown_event.wait()

        # Cleanup
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        print("OpenClaude daemon stopped.", flush=True)

    # ------------------------------------------------------------------
    # Client handler
    # ------------------------------------------------------------------

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
            except Exception:
                pass
        except Exception as e:
            try:
                await self._send_json(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    async def handle_query(
        self, request: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        """Call claude-agent-sdk and stream response chunks back to CLI."""
        # Import here to keep startup fast and allow ImportError messages
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                query,
            )
            from claude_agent_sdk.types import StreamEvent, TextBlock
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

        # Log user message before sending to SDK
        await self.session_store.append_message(
            session_alias, role="user", content=user_message
        )

        current_sdk_session_id: Optional[str] = sdk_session_id
        current_model: Optional[str] = None
        full_text: str = ""
        has_stream_events: bool = False

        try:
            async for message in query(prompt=user_message, options=options):
                # --- Session init ---
                if hasattr(message, "subtype") and message.subtype == "init":
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
                    meta = init_meta

                # --- Streaming text chunks (when include_partial_messages=True) ---
                elif isinstance(message, StreamEvent):
                    event = message.event
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            has_stream_events = True
                            chunk = delta.get("text", "")
                            if chunk:
                                full_text += chunk
                                await self._send_json(writer, {"type": "chunk", "text": chunk})

                # --- Full assistant message (for model info; text fallback if no StreamEvent) ---
                elif isinstance(message, AssistantMessage):
                    if hasattr(message, "model") and message.model:
                        current_model = message.model
                    # Fallback: if no StreamEvent received, stream text here
                    if not has_stream_events:
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                full_text += block.text
                                await self._send_json(writer, {"type": "chunk", "text": block.text})

                # --- Final result: update session metadata ---
                elif isinstance(message, ResultMessage):
                    # Fallback sdk_session_id from ResultMessage
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

                    # Log assistant message
                    await self.session_store.append_message(
                        session_alias,
                        role="assistant",
                        content=full_text,
                        model=current_model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        stop_reason=stop_reason,
                    )

                    # Send completion signal
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

        except Exception as e:
            await self._send_json(writer, {"type": "error", "message": str(e)})

    async def handle_sessions(self, writer: asyncio.StreamWriter) -> None:
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
        await self._send_json(writer, {"type": "stopped"})
        # Delay shutdown to let response flush
        asyncio.get_event_loop().call_later(0.2, self._shutdown_event.set)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_json(self, writer: asyncio.StreamWriter, data: dict) -> None:
        line = json.dumps(data, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()


# ---------------------------------------------------------------------------
# Process management (used by cli.py)
# ---------------------------------------------------------------------------

def start_daemon_process() -> None:
    """Fork daemon as a detached background process."""
    python = sys.executable

    with open(str(DAEMON_LOG), "a") as log:
        subprocess.Popen(
            [python, "-m", "src.daemon"],
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def stop_daemon_process() -> bool:
    """
    Send stop request via socket, or SIGTERM via PID file.
    Returns True if stop was sent successfully.
    """
    import socket as _socket

    # Try socket first
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
    except Exception:
        pass

    # Fallback: send SIGTERM via PID file
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            pass

    return False


def get_daemon_status() -> tuple[str, Optional[int]]:
    """
    Returns (status_string, pid_or_None).
    status_string: 'running', 'stopped', 'stale'
    """
    if not PID_FILE.exists():
        return "stopped", None

    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return "stopped", None

    # Check if process is alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale", pid
    except PermissionError:
        pass  # Process exists but we can't signal it

    # Check socket connectivity
    if SOCKET_PATH.exists():
        return "running", pid

    return "stale", pid


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    # Set CWD to project root so setting_sources=["project"] finds .claude/settings.json
    os.chdir(str(BASE_DIR))
    daemon = OpenClaudeDaemon()
    await daemon.start()


if __name__ == "__main__":
    asyncio.run(_main())
