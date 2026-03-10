"""Session persistence: sessions.json metadata + per-session JSONL logs."""
import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import CONTEXT_WINDOW, SESSIONS_DIR, SESSIONS_JSON


@dataclass
class SessionMeta:
    session_id: str
    sdk_session_id: Optional[str]
    model: Optional[str]
    created_at: str
    last_active_at: str
    total_input_tokens: int
    total_output_tokens: int
    context_window: int
    num_turns: int


class SessionStore:
    """Manages session metadata (sessions.json) and JSONL conversation logs."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionMeta] = {}

    async def load(self) -> None:
        """Load sessions.json on daemon startup. Creates directory if needed."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if SESSIONS_JSON.exists():
            try:
                data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
                for alias, meta_dict in data.items():
                    self._sessions[alias] = SessionMeta(**meta_dict)
            except Exception:
                self._sessions = {}

    async def save(self) -> None:
        """Atomically write sessions.json (tmpfile -> rename)."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        data = {alias: asdict(meta) for alias, meta in self._sessions.items()}
        fd, tmp_path = tempfile.mkstemp(dir=str(SESSIONS_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(SESSIONS_JSON))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    async def get_session(self, alias: str) -> Optional[SessionMeta]:
        return self._sessions.get(alias)

    async def upsert_session(self, meta: SessionMeta) -> None:
        async with self._lock:
            self._sessions[meta.session_id] = meta
            await self.save()

    async def list_sessions(self) -> list[SessionMeta]:
        return list(self._sessions.values())

    async def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        model: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        stop_reason: Optional[str] = None,
    ) -> None:
        """Append one line to the session's JSONL file."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        jsonl_path = SESSIONS_DIR / f"{session_id}.jsonl"
        now = datetime.now(timezone.utc).isoformat()
        entry: dict[str, Any] = {
            "timestamp": now,
            "role": role,
            "content": content,
        }
        if role == "assistant":
            if model:
                entry["model"] = model
            entry["input_tokens"] = input_tokens
            entry["output_tokens"] = output_tokens
            if stop_reason:
                entry["stop_reason"] = stop_reason

        async with self._lock:
            with open(str(jsonl_path), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def format_age(last_active_at: str) -> str:
        """Convert ISO timestamp to human-readable age like '12m ago'."""
        try:
            then = datetime.fromisoformat(last_active_at)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta_seconds = int((now - then).total_seconds())
            if delta_seconds < 60:
                return f"{delta_seconds}s ago"
            elif delta_seconds < 3600:
                return f"{delta_seconds // 60}m ago"
            elif delta_seconds < 86400:
                return f"{delta_seconds // 3600}h ago"
            else:
                return f"{delta_seconds // 86400}d ago"
        except Exception:
            return "unknown"

    @staticmethod
    def format_tokens(meta: "SessionMeta") -> str:
        """Format token usage like '7.7k/200k (3%)'."""
        total = meta.total_input_tokens + meta.total_output_tokens
        ctx = meta.context_window or CONTEXT_WINDOW
        pct = int(total / ctx * 100) if ctx > 0 else 0

        def fmt_k(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        return f"{fmt_k(total)}/{fmt_k(ctx)} ({pct}%)"
