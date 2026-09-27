"""A deterministic stand-in for Claude, so the product can be demoed and tested offline.

For the bundled demo emails it returns hand-written responses from
fixtures/demo/scripted_responses.json. For anything else it uses simple keyword
rules and a template reply built from the best retrieved source. Real
classification and drafting quality comes from the Claude assistant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..models import Classification, ClientRecord, ContextSource, DraftResult, InboundEmail
from .base import AIResult, AssistantRefusal, CallUsage

RULES: list[tuple[str, tuple[str, ...]]] = [
    ("automated_or_spam", ("unsubscribe", "out of office", "automatic reply", "guaranteed", "free audit")),
    (
        "legal_compliance",
        ("subject access", "gdpr", "data protection", "solicitor", "lawyer", "legal action"),
    ),
    ("cancellation", ("cancel", "not renew", "notice period", "competing quote", "other providers")),
    ("account_access", ("locked out", "password", "two-factor", "2fa", "sign in", "log in", "login")),
    ("billing", ("invoice", "charged", "refund", "credit", "direct debit", "price", "pricing", "payment")),
    (
        "technical_issue",
        ("error", "failed", "not working", "broken", "sync", "can't see", "cannot see", "bug"),
    ),
    ("feature_request", ("feature", "would be great", "roadmap", "suggestion")),
    ("scheduling", ("book a call", "meeting", "schedule", "reschedule", "call next week")),
    ("complaint", ("disappointed", "unacceptable", "frustrated", "terrible", "complaint")),
    ("onboarding", ("onboarding", "go live", "go-live", "parallel run", "import")),
]
CRITICAL = ("payday", "not be paid", "not paid", "urgent", "asap", "immediately", "outage", "breach")
HIGH = ("locked out", "charged twice", "duplicate", "deadline", "disappointed", "unacceptable", "escalat")
NEGATIVE = ("disappointed", "unacceptable", "frustrated", "terrible", "angry", "again")


def _has(text: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w in text]


class MockAssistant:
    name = "mock"

    def __init__(self, responses_file: Path | None = None) -> None:
        self.scripted: dict[str, Any] = {}
        if responses_file and responses_file.exists():
            data = json.loads(responses_file.read_text(encoding="utf-8"))
            self.scripted = {k: v for k, v in data.items() if not k.startswith("_")}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _usage() -> CallUsage:
        return CallUsage(model="mock", stop_reason="end_turn")

    def classify(self, email: InboundEmail, client: ClientRecord | None) -> AIResult[Classification]:
        self.calls.append(("classify", email.provider_id))
        if "[mock-refuse]" in email.subject:
            raise AssistantRefusal("Mock refusal for testing", self._usage())
        scripted = self.scripted.get(email.provider_id)
        if scripted:
            return AIResult(Classification.model_validate(scripted["classification"]), self._usage())
        return AIResult(self._rule_classify(email), self._usage())

    def _rule_classify(self, email: InboundEmail) -> Classification:
        text = f"{email.subject}\n{email.body_text}".lower()
        category = "general_question"
        for cat, keywords in RULES:
            if _has(text, keywords):
                category = cat
                break
        urgency = "critical" if _has(text, CRITICAL) else "high" if _has(text, HIGH) else "normal"
        if category in ("feature_request", "automated_or_spam"):
            urgency = "low"
        negative = _has(text, NEGATIVE)
        flags: list[str] = []
        if category == "cancellation" or "other providers" in text:
            flags.append("churn_risk")
        if category == "legal_compliance":
            flags.append("regulatory")
        words = [w for w in re.findall(r"[a-z]{4,}", email.subject.lower())][:6]
        first_line = next(
            (ln.strip() for ln in email.body_text.splitlines() if len(ln.strip()) > 20), email.subject
        )
        return Classification.model_validate(
            {
                "category": category,
                "urgency": urgency,
                "urgency_reason": "Keyword rules (mock assistant).",
                "sentiment": "angry" if len(negative) > 1 else "negative" if negative else "neutral",
                "summary": (first_line[:140] + "…") if len(first_line) > 140 else first_line,
                "needs_reply": category != "automated_or_spam",
                "complexity": "complex" if flags else "simple",
                "confidence": 0.6,
                "questions": [s.strip() + "?" for s in re.findall(r"([^.?!\n]{8,160})\?", email.body_text)][
                    :3
                ],
                "escalation_flags": flags,
                "kb_queries": [" ".join(words)] if words else [],
                "client_hint": None,
            }
        )

    def draft(
        self,
        email: InboundEmail,
        classification: Classification,
        client: ClientRecord | None,
        sources: list[ContextSource],
        instruction: str | None = None,
    ) -> AIResult[DraftResult]:
        self.calls.append(("draft", email.provider_id))
        scripted = self.scripted.get(email.provider_id)
        if scripted and "draft" in scripted:
            result = DraftResult.model_validate(scripted["draft"])
            if instruction:
                result.body += (
                    "\n\n[Mock assistant: with AI_PROVIDER=claude the draft is rewritten to follow the "
                    f"instruction: {instruction.strip()!r}]"
                )
            return AIResult(result, self._usage())
        name = (email.sender.name or "").split(" ")[0]
        greeting = f"Hi {name}," if name and name[0].isupper() else "Hello,"
        kb = [s for s in sources if s.kind == "kb"]
        if kb:
            first = kb[0]
            sentence = re.split(r"(?<=[.!?])\s", " ".join(first.text.split()))[0]
            body = f"{greeting}\n\nThanks for getting in touch. {sentence}\n\nWe'll follow up with anything else you need."
            citations = [{"source_id": first.source_id, "supports": sentence[:120]}]
            missing: list[str] = []
        else:
            body = f"{greeting}\n\nThanks for getting in touch. We're looking into this and will reply by tomorrow."
            citations = []
            missing = ["No knowledge-base article matched: write the answer by hand"]
        if instruction:
            body += f"\n\n[Mock assistant: instruction noted: {instruction.strip()!r}]"
        return AIResult(
            DraftResult.model_validate(
                {"body": body, "citations": citations, "missing_info": missing, "confidence": 0.5}
            ),
            self._usage(),
        )
