"""The mailbox interface.

There is deliberately no method that sends mail. A provider can read messages,
add labels or categories, and save or delete its own drafts. People send the
replies from Gmail or Outlook. tests/test_no_send.py checks that the provider
modules never call a send endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..models import InboundEmail


@dataclass(frozen=True)
class DraftRef:
    draft_id: str
    web_link: str | None
    # Changes when someone edits the draft (Gmail: the draft's message id; Graph: changeKey).
    version: str | None = None


class MailProvider(Protocol):
    name: str

    def list_new(self, since: datetime, limit: int) -> list[InboundEmail]:
        """Inbound messages received since `since` that are not yet labelled AI/Triaged, oldest first."""
        ...

    def get(self, message_id: str) -> InboundEmail | None:
        """One message with its thread context, or None if it no longer exists."""
        ...

    def apply_labels(self, email: InboundEmail, labels: list[str]) -> None:
        """Add labels (Gmail) or categories (Outlook), creating them if needed. Never removes user labels."""
        ...

    def create_reply_draft(self, email: InboundEmail, body_text: str) -> DraftRef:
        """Save a reply-to-sender draft in the same thread. It is not sent."""
        ...

    def delete_draft(self, ref: DraftRef) -> bool:
        """Delete a draft this app created, but only if nobody has edited it since (same version).

        Returns True if it was deleted. An edited, sent or already deleted draft is left alone.
        """
        ...

    def has_reply_after(self, thread_id: str, after: datetime) -> bool:
        """True if someone sent a reply in the thread after `after` (drafts do not count)."""
        ...


# Label names are shared by all providers. Gmail shows "/" as nesting; Outlook shows them as category names.
LABEL_ROOT = "AI"
TRIAGED = "AI/Triaged"
DRAFT_READY = "AI/Draft ready"
NEEDS_HUMAN = "AI/Needs human"
EXCLUDED = "AI/Excluded"
NO_REPLY = "AI/No reply needed"


def label(kind: str, value: str) -> str:
    return f"{LABEL_ROOT}/{kind}/{value}"
