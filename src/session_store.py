"""セッション永続化: sessions.json メタデータ + セッション別 JSONL ログ。"""

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import CONTEXT_WINDOW, SESSIONS_DIR, SESSIONS_JSON


@dataclass
class SessionMeta:
    """セッションのメタデータを保持するデータクラス。"""

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
    """sessions.json メタデータと JSONL 会話ログを管理するクラス。"""

    def __init__(self) -> None:
        """ロックと空のセッション辞書を初期化する。"""
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionMeta] = {}

    async def load(self) -> None:
        """デーモン起動時に sessions.json を読み込む。ディレクトリが存在しない場合は作成する。"""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if SESSIONS_JSON.exists():
            try:
                data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
                for alias, meta_dict in data.items():
                    self._sessions[alias] = SessionMeta(**meta_dict)
            except Exception:
                self._sessions = {}

    async def save(self) -> None:
        """sessions.json をアトミックに書き込む（tmpfile -> rename）。"""
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
            except Exception:  # noqa: S110
                pass
            raise

    async def get_session(self, alias: str) -> Optional[SessionMeta]:
        """エイリアスに対応するセッションメタデータを返す。存在しない場合は None を返す。"""
        return self._sessions.get(alias)

    async def upsert_session(self, meta: SessionMeta) -> None:
        """セッションメタデータを追加または更新し、sessions.json に保存する。"""
        async with self._lock:
            self._sessions[meta.session_id] = meta
            await self.save()

    async def list_sessions(self) -> list[SessionMeta]:
        """全セッションのメタデータ一覧を返す。"""
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
        """セッションの JSONL ファイルに1行追記する。"""
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
        """ISO タイムスタンプを '12m ago' のような人間が読みやすい形式に変換する。"""
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
        """トークン使用量を '7.7k/200k (3%)' の形式でフォーマットする。"""
        total = meta.total_input_tokens + meta.total_output_tokens
        ctx = meta.context_window or CONTEXT_WINDOW
        pct = int(total / ctx * 100) if ctx > 0 else 0

        def fmt_k(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        return f"{fmt_k(total)}/{fmt_k(ctx)} ({pct}%)"
