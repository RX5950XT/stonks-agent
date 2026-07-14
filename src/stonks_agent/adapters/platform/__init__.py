"""Optional external platform adapters."""

from ._ai_trader_contracts import (
    AiTraderEventPollRequest,
    AiTraderReplyRequest,
    MemoryPlatformEventInbox,
)
from .ai_trader import (
    AI_TRADER_ENDPOINT_TEMPLATES,
    AiTraderHttpAdapter,
)

__all__ = [
    "AI_TRADER_ENDPOINT_TEMPLATES",
    "AiTraderEventPollRequest",
    "AiTraderHttpAdapter",
    "AiTraderReplyRequest",
    "MemoryPlatformEventInbox",
]
