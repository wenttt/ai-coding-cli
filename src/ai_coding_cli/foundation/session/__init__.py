"""Session + Conversation persistence service. See ADR-0008.

Public exports:
    - SessionManager: get_or_create + start_conversation + append_messages +
      record_turn + end_conversation
    - Message, Turn, Session, Conversation dataclasses (in-memory views)
"""

from __future__ import annotations

from ._manager import SessionManager
from ._types import (
    ConversationView,
    Message,
    SessionView,
    TurnRecord,
)

__all__ = [
    "SessionManager",
    "Message",
    "TurnRecord",
    "SessionView",
    "ConversationView",
]
