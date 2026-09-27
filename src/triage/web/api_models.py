"""Request and response bodies for POST /api/triage (used by the n8n workflows)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings
from ..mail.parsing import (
    format_address,
    html_to_text,
    parse_address,
    parse_addresses,
    reply_subject,
    text_to_html,
)
from ..models import CATEGORY_LABELS, EmailAddress, InboundEmail, ThreadMessage
from ..pipeline import TriageOutcome, local_time
from ..timeutil import utcnow


class Address(BaseModel):
    email: str
    name: str | None = None


AddressLike = Address | str


def _one(value: AddressLike | None) -> EmailAddress | None:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_address(value)
    return EmailAddress(email=value.email.lower(), name=value.name)


def _many(value: list[AddressLike] | str | None) -> list[EmailAddress]:
    if not value:
        return []
    if isinstance(value, str):
        return parse_addresses(value)
    return [a for a in (_one(v) for v in value) if a is not None]


def _header_value(name: str, value: str) -> str:
    """n8n's Gmail trigger gives whole header lines ("Auto-Submitted: auto-replied"); keep just the value."""
    prefix = f"{name}:"
    return value[len(prefix) :].strip() if value.lower().startswith(prefix.lower()) else value


class ThreadItem(BaseModel):
    sender: str
    sent_at: datetime
    text: str
    outbound: bool = False


class TriageRequest(BaseModel):
    """One email as a workflow tool sees it. Addresses may be objects or "Name <email>" strings."""

    model_config = ConfigDict(populate_by_name=True)

    provider: Literal["gmail", "outlook", "other"] = "gmail"
    message_id: str = Field(description="The mailbox's id for the message (Gmail id / Graph id)")
    thread_id: str | None = Field(default=None, description="Gmail threadId or Graph conversationId")
    sender: AddressLike = Field(alias="from")
    reply_to: AddressLike | None = None
    to: list[AddressLike] | str = Field(default_factory=list)
    cc: list[AddressLike] | str = Field(default_factory=list)
    subject: str = ""
    text: str | None = None
    html: str | None = None
    received_at: datetime | None = None
    message_id_header: str | None = Field(default=None, description="The RFC 5322 Message-ID header")
    references: list[str] | str = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    thread: list[ThreadItem] = Field(default_factory=list)

    def to_email(self) -> InboundEmail:
        body = self.text or (html_to_text(self.html) if self.html else "")
        refs = self.references.split() if isinstance(self.references, str) else self.references
        sender = _one(self.sender)
        assert sender is not None
        return InboundEmail(
            provider_id=self.message_id,
            thread_id=self.thread_id or self.message_id,
            sender=sender,
            reply_to=_one(self.reply_to),
            to=_many(self.to),
            cc=_many(self.cc),
            subject=self.subject,
            body_text=body.strip(),
            received_at=self.received_at or utcnow(),
            message_id_header=self.message_id_header,
            references=refs,
            attachments=self.attachments,
            headers={k.lower(): _header_value(k, v) for k, v in self.headers.items()},
            thread_context=[
                ThreadMessage(sender=t.sender, sent_at=t.sent_at, body_text=t.text, outbound=t.outbound)
                for t in self.thread[-2:]
            ],
        )


class DraftOut(BaseModel):
    to: str
    subject: str
    body_text: str
    body_html: str
    thread_id: str
    in_reply_to: str | None
    references: str | None


class TriageResponse(BaseModel):
    query_id: int
    message_id: str
    thread_id: str
    status: str
    labels: list[str]
    category: str | None = None
    category_label: str | None = None
    urgency: str | None = None
    summary: str | None = None
    escalation_flags: list[str] = Field(default_factory=list)
    assignee: dict[str, Any] | None = None
    rule: str | None = None
    alert: bool = False
    alert_text: str | None = None
    due_at: datetime | None = None
    draft: DraftOut | None = None
    citations: list[dict[str, str]] = Field(default_factory=list)
    excluded_reason: str | None = None
    dashboard_url: str


def triage_response(outcome: TriageOutcome, email: InboundEmail, settings: Settings) -> TriageResponse:
    c = outcome.classification
    route = outcome.route
    dashboard = f"{settings.public_url.rstrip('/')}/queries/{outcome.query_id}"
    draft = None
    if outcome.draft_text:
        draft = DraftOut(
            to=format_address(email.reply_to or email.sender),
            subject=reply_subject(email.subject),
            body_text=outcome.draft_text,
            body_html=text_to_html(outcome.draft_text),
            thread_id=email.thread_id,
            in_reply_to=email.message_id_header,
            references=" ".join([*email.references, email.message_id_header])
            if email.message_id_header
            else None,
        )
    alert_text = None
    if route and route.alert:
        who = email.sender.name or email.sender.email
        if outcome.status == "excluded":
            alert_text = (
                f"Confidential client email for {route.assignee.name}: not processed by AI. {dashboard}"
            )
        elif c:
            due = f" Reply by {local_time(outcome.due_at, settings)}." if outcome.due_at else ""
            alert_text = (
                f"{c.urgency.title()}: {CATEGORY_LABELS[c.category]} from {who}, assigned to "
                f"{route.assignee.name}. {c.summary}{due} {dashboard}"
            )
        else:
            alert_text = f"Email from {who} needs a person ({outcome.error}). {dashboard}"
    return TriageResponse(
        query_id=outcome.query_id,
        message_id=email.provider_id,
        thread_id=email.thread_id,
        status=outcome.status,
        labels=outcome.labels,
        category=c.category if c else None,
        category_label=CATEGORY_LABELS[c.category] if c else None,
        urgency=c.urgency if c else None,
        summary=c.summary if c else None,
        escalation_flags=list(c.escalation_flags) if c else [],
        assignee=route.assignee.model_dump() if route else None,
        rule=route.rule if route else None,
        alert=bool(route and route.alert),
        alert_text=alert_text,
        due_at=outcome.due_at,
        draft=draft,
        citations=[ci.model_dump() for ci in outcome.citations],
        excluded_reason=outcome.excluded_reason,
        dashboard_url=dashboard,
    )
