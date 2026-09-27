"""Domain models shared by the pipeline, the mail providers and the assistant."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field

Category = Literal[
    "billing",
    "technical_issue",
    "account_access",
    "onboarding",
    "feature_request",
    "complaint",
    "cancellation",
    "scheduling",
    "legal_compliance",
    "general_question",
    "automated_or_spam",
    "other",
]
CATEGORIES: tuple[str, ...] = get_args(Category)

CATEGORY_LABELS: dict[str, str] = {
    "billing": "Billing",
    "technical_issue": "Technical issue",
    "account_access": "Account access",
    "onboarding": "Onboarding",
    "feature_request": "Feature request",
    "complaint": "Complaint",
    "cancellation": "Cancellation",
    "scheduling": "Scheduling",
    "legal_compliance": "Legal & compliance",
    "general_question": "General question",
    "automated_or_spam": "Automated or spam",
    "other": "Other",
}

Urgency = Literal["critical", "high", "normal", "low"]
URGENCIES: tuple[str, ...] = ("critical", "high", "normal", "low")

EscalationFlag = Literal[
    "legal_threat",
    "security_or_data",
    "regulatory",
    "churn_risk",
    "executive_or_vip",
    "payment_deadline",
]

# Lifecycle of a query (one inbound email that may need a reply).
QueryStatus = Literal[
    "draft_ready",  # draft saved in the mailbox, waiting for a person
    "needs_human",  # no draft (refusal, low confidence or error): a person writes the reply
    "excluded",  # confidential client: never sent to the AI
    "no_reply_needed",  # thanks, auto-replies, newsletters
    "replied",  # someone answered in the mailbox
    "resolved",  # closed by hand in the dashboard
    "superseded",  # a newer message in the same thread replaced it
    "error",  # processing failed; retried on the next poll
]
OPEN_STATUSES: tuple[str, ...] = ("draft_ready", "needs_human", "excluded", "error")


class EmailAddress(BaseModel):
    email: str
    name: str | None = None

    def display(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


class ThreadMessage(BaseModel):
    sender: str
    sent_at: datetime
    body_text: str
    outbound: bool


class InboundEmail(BaseModel):
    """One message from the shared inbox, normalized across Gmail, Graph and the demo mailbox."""

    provider_id: str
    thread_id: str
    sender: EmailAddress
    reply_to: EmailAddress | None = None
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    received_at: datetime
    message_id_header: str | None = None
    references: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    provider_labels: list[str] = Field(default_factory=list)
    thread_context: list[ThreadMessage] = Field(default_factory=list)


class Classification(BaseModel):
    category: Category = Field(description="The single best-fitting category.")
    urgency: Urgency = Field(description="Urgency per the rubric in the instructions.")
    urgency_reason: str = Field(description="One short sentence explaining the urgency, citing the email.")
    sentiment: Literal["positive", "neutral", "negative", "angry"]
    summary: str = Field(description="One sentence, max 25 words, saying what the client needs.")
    needs_reply: bool = Field(
        description="False only for thanks-only notes, auto-replies, newsletters, spam."
    )
    complexity: Literal["simple", "complex"] = Field(
        description="complex = needs investigation, judgment, a decision or several teams."
    )
    confidence: float = Field(description="0.0-1.0: how sure you are about category and urgency.")
    questions: list[str] = Field(description="The distinct questions or requests in the email, paraphrased.")
    escalation_flags: list[EscalationFlag] = Field(description="Every flag that applies; empty if none.")
    kb_queries: list[str] = Field(description="1-3 short search queries for the help-centre knowledge base.")
    client_hint: str | None = Field(
        description="Company name the sender says they write for, if stated in the email; otherwise null."
    )


class Citation(BaseModel):
    source_id: str = Field(
        description="Exactly one of the source ids provided, e.g. KB:refund-policy#annual-plans"
    )
    supports: str = Field(description="The statement in the reply that this source supports.")


class DraftResult(BaseModel):
    body: str = Field(
        description="The reply, plain text, starting with a greeting and ending before the sign-off/signature."
    )
    citations: list[Citation] = Field(description="Sources used for each factual statement.")
    missing_info: list[str] = Field(
        description="Facts the reviewer must check or fill in before sending; empty if none."
    )
    confidence: float = Field(description="0.0-1.0: how ready this draft is to send after a light edit.")


class ContextSource(BaseModel):
    """A piece of retrieved context the draft may cite."""

    source_id: str
    kind: Literal["kb", "client", "history"]
    title: str
    text: str
    url: str | None = None


class ClientRecord(BaseModel):
    id: str
    name: str
    domains: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)
    tier: str = "standard"
    plan: str = ""
    account_owner: str | None = None
    ai_excluded: bool = False
    status: str = "active"
    renewal_date: str | None = None
    notes: str = ""


class TeamMember(BaseModel):
    key: str
    name: str
    email: str
    role: str = ""
    slack_id: str | None = None
    teams_upn: str | None = None


class RouteDecision(BaseModel):
    assignee: TeamMember
    rule: str
    alert: bool
    reason: str
