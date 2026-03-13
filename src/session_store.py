"""セッション永続化: sessions.json メタデータ + セッション別 JSONL ログ。"""

from dataclasses import dataclass
from typing import Optional


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
