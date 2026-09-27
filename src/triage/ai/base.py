"""The assistant interface. Implementations classify and draft; none of them can send anything."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from ..models import Classification, ClientRecord, ContextSource, DraftResult, InboundEmail

T = TypeVar("T")


@dataclass(frozen=True)
class CallUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    stop_reason: str | None = None


@dataclass(frozen=True)
class AIResult(Generic[T]):
    value: T
    usage: CallUsage | None


class AssistantRefusal(Exception):
    """The model declined the request. The query goes to a person without a draft."""

    def __init__(self, message: str, usage: CallUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class AssistantError(Exception):
    """A transient or unexpected failure (rate limit, network, invalid output). The message is retried later."""

    def __init__(self, message: str, usage: CallUsage | None = None, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.usage = usage
        self.retryable = retryable


class Assistant(Protocol):
    name: str

    def classify(self, email: InboundEmail, client: ClientRecord | None) -> AIResult[Classification]: ...

    def draft(
        self,
        email: InboundEmail,
        classification: Classification,
        client: ClientRecord | None,
        sources: list[ContextSource],
        instruction: str | None = None,
    ) -> AIResult[DraftResult]: ...
