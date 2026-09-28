"""The triage pipeline: new email → privacy gate → classify → context → draft → labels → routing → alert.

`process_inbox` runs it for every new message in the configured mailbox.
`triage_email` runs it for one message and is also what the n8n API calls
(with no mailbox: n8n saves the draft and labels itself).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .ai.base import AIResult, Assistant, AssistantError, AssistantRefusal, CallUsage
from .config import Settings
from .context.clients import ClientDirectory, client_sources
from .context.knowledge import KnowledgeSource
from .db import Database
from .mail.base import (
    DRAFT_READY,
    EXCLUDED,
    NEEDS_HUMAN,
    NO_REPLY,
    TRIAGED,
    DraftRef,
    MailboxNotConnected,
    MailProvider,
    label,
)
from .models import (
    CATEGORY_LABELS,
    Citation,
    Classification,
    ClientRecord,
    ContextSource,
    DraftResult,
    EmailAddress,
    InboundEmail,
    RouteDecision,
)
from .notify.base import Message, Notifier
from .privacy import PrivacyPolicy
from .routing import RouteFacts, Router
from .store import (
    audit,
    current_draft,
    get_query,
    get_query_by_message,
    insert_draft,
    open_queries,
    open_queries_in_thread,
    recent_for_client,
    record_llm_call,
    replace_sources,
    save_query,
    sources_for,
    supersede_drafts,
    update_query,
)
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
_poll_lock = threading.Lock()


@dataclass
class Services:
    settings: Settings
    db: Database
    mail: MailProvider | None
    assistant: Assistant
    clients: ClientDirectory
    kb: KnowledgeSource
    privacy: PrivacyPolicy
    router: Router
    notifier: Notifier
    clock: Callable[[], datetime] = utcnow
    # Why there is no mailbox (not connected yet, access revoked); shown in the dashboard.
    mail_error: str | None = None
    # The outcome of the latest inbox check (when, what happened, whether it worked), for the dashboard.
    last_poll: tuple[datetime, str, bool] | None = None


@dataclass
class TriageOutcome:
    query_id: int
    status: str
    labels: list[str] = field(default_factory=list)
    classification: Classification | None = None
    draft: DraftResult | None = None
    draft_text: str | None = None
    citations: list[Citation] = field(default_factory=list)
    route: RouteDecision | None = None
    due_at: datetime | None = None
    excluded_reason: str | None = None
    error: str | None = None


@dataclass
class PollReport:
    fetched: int = 0
    processed: int = 0
    skipped: int = 0
    drafts: int = 0
    needs_human: int = 0
    excluded: int = 0
    no_reply: int = 0
    errors: int = 0
    replied: int = 0
    closed_by_reply: int = 0
    busy: bool = False  # another check was already running

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)

    def message(self) -> str:
        if self.busy:
            return "An inbox check is already running. New queries appear here as they are triaged."
        if self.fetched == 0:
            return "Checked the inbox: nothing new."
        parts = [f"{self.processed} new"]
        for count, text in (
            (self.drafts, "drafted"),
            (self.needs_human, "need a person"),
            (self.excluded, "confidential"),
            (self.no_reply, "need no reply"),
            (self.replied, "already answered"),
            (self.errors, "will be retried"),
        ):
            if count:
                parts.append(f"{count} {text}")
        if self.closed_by_reply:
            parts.append(f"{self.closed_by_reply} closed because someone replied")
        return "Checked the inbox: " + ", ".join(parts) + "."


# ------------------------------------------------------------------ helpers


def is_outbound(email: InboundEmail, settings: Settings, mailbox_address: str | None = None) -> bool:
    sender = email.sender.email.lower()
    domain = sender.rpartition("@")[2]
    ours = {settings.mailbox_address.lower(), (mailbox_address or "").lower()} - {""}
    return sender in ours or domain in settings.team_domain_list


def automated_reason(email: InboundEmail) -> str | None:
    headers = {k.lower(): v.lower() for k, v in email.headers.items()}
    if headers.get("auto-submitted", "no") != "no" or "x-autoreply" in headers or "x-autorespond" in headers:
        return "Auto-reply"
    if headers.get("precedence") in ("bulk", "list", "junk") or "list-unsubscribe" in headers:
        return "Bulk or marketing mail"
    return None


def validate_citations(
    citations: list[Citation], sources: list[ContextSource]
) -> tuple[list[Citation], list[Citation]]:
    """Keep citations that name a source we actually gave the model; drop the rest (and report them)."""
    known = {s.source_id for s in sources}
    valid: list[Citation] = []
    dropped: list[Citation] = []
    seen: set[str] = set()
    for c in citations:
        if c.source_id not in known:
            dropped.append(c)
        elif c.source_id not in seen:
            seen.add(c.source_id)
            valid.append(c)
    return valid, dropped


def local_time(dt: datetime, settings: Settings) -> str:
    return dt.astimezone(ZoneInfo(settings.timezone)).strftime("%a %d %b %H:%M")


def compose_draft(
    settings: Settings,
    draft: DraftResult,
    citations: list[Citation],
    sources: list[ContextSource],
    *,
    query_id: int,
    warnings: list[str],
) -> str:
    """What is saved in the mailbox: an optional reviewer note, the reply, and the signature."""
    parts: list[str] = []
    if settings.draft_review_note:
        by_id = {s.source_id: s for s in sources}
        note = ["✂ AI DRAFT FOR REVIEW: delete this block before sending ✂"]
        if citations:
            note.append("Sources:")
            for c in citations:
                s = by_id[c.source_id]
                note.append(f"  • {s.title}" + (f" ({s.url})" if s.url else ""))
        checks = [*warnings, *draft.missing_info]
        if checks:
            note.append("Check before sending:")
            note.extend(f"  • {item}" for item in checks)
        note.append(f"Details: {settings.public_url.rstrip('/')}/queries/{query_id}")
        note.append("✂" + "-" * 58 + "✂")
        parts.append("\n".join(note))
    parts.append(draft.body.strip())
    parts.append(settings.reply_signature.strip())
    return "\n\n".join(parts)


def _record_usage(svc: Services, query_id: int | None, purpose: str, usage: CallUsage | None) -> None:
    if usage is None:
        return
    with svc.db.session() as conn:
        record_llm_call(
            conn,
            query_id,
            {
                "purpose": purpose,
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
                "stop_reason": usage.stop_reason,
                "created_at": svc.clock(),
            },
        )


def _classification_fields(c: Classification) -> dict[str, Any]:
    return {
        "category": c.category,
        "urgency": c.urgency,
        "urgency_reason": c.urgency_reason,
        "sentiment": c.sentiment,
        "complexity": c.complexity,
        "confidence": c.confidence,
        "summary": c.summary,
        "client_hint": c.client_hint,
        "escalation_flags": list(c.escalation_flags),
        "questions": c.questions,
    }


def _alert_message(
    svc: Services,
    query_id: int,
    email: InboundEmail,
    client: ClientRecord | None,
    classification: Classification | None,
    route: RouteDecision,
    due: datetime,
    *,
    draft_link: str | None,
    excluded: bool = False,
    reason: str | None = None,
) -> Message:
    settings = svc.settings
    who = client.name if client else (email.sender.name or email.sender.email)
    dashboard = f"{settings.public_url.rstrip('/')}/queries/{query_id}"
    facts = [
        (
            "Client",
            f"{client.name} ({client.tier})" if client else f"{email.sender.email} (no client record)",
        ),
        ("Assigned to", route.assignee.name),
        ("Reply by", local_time(due, settings)),
    ]
    links = [("Open in dashboard", dashboard)]
    if draft_link and draft_link.startswith("http"):
        links.append(("Open draft", draft_link))
    if excluded:
        return Message(
            kind="alert",
            title=f"Confidential client email: {who}",
            severity="high",
            lines=[
                "Not processed by AI: this client is excluded. Open the email in the shared mailbox.",
            ],
            facts=facts,
            links=links,
            mention=route.assignee,
        )
    if classification is None:
        return Message(
            kind="alert",
            title=f"Needs a person: email from {who}",
            severity="high",
            lines=[f"No draft was written ({reason or 'unknown reason'}).", f"Subject: {email.subject}"],
            facts=facts,
            links=links,
            mention=route.assignee,
        )
    category = CATEGORY_LABELS.get(classification.category, classification.category)
    lines = [classification.summary, f"Why {classification.urgency}: {classification.urgency_reason}"]
    if classification.escalation_flags:
        lines.append("Flags: " + ", ".join(f.replace("_", " ") for f in classification.escalation_flags))
    lines.append(
        "Draft reply is ready in the mailbox." if draft_link else "No draft: a person needs to reply."
    )
    return Message(
        kind="alert",
        title=f"{classification.urgency.title()}: {category} from {who}",
        severity="critical" if classification.urgency == "critical" else "high",
        lines=lines,
        facts=[*facts, ("Rule", route.rule)],
        links=links,
        mention=route.assignee,
    )


def _supersede_thread(
    svc: Services, provider: str, email: InboundEmail, new_id: int, mailbox: MailProvider | None
) -> None:
    """A newer message in the thread replaces older open queries (and their now-stale, unedited drafts)."""
    now = svc.clock()
    with svc.db.session() as conn:
        older = [
            q
            for q in open_queries_in_thread(conn, provider, email.thread_id, exclude_id=new_id)
            if q["received_at"] <= iso(email.received_at)
        ]
        drafts = [(q, current_draft(conn, q["id"])) for q in older]
        for q in older:
            update_query(
                conn, q["id"], status="superseded", superseded_by=new_id, closed_at=now, closed_by="system"
            )
            supersede_drafts(conn, q["id"], now)
            audit(conn, "system", "query.superseded", q["id"], at=now, by=new_id)
    if mailbox is None:
        return
    for q, d in drafts:
        if d and d.get("provider_draft_id"):
            ref = DraftRef(d["provider_draft_id"], d.get("web_link"), d.get("provider_version"))
            deleted = mailbox.delete_draft(ref)
            with svc.db.session() as conn:
                audit(
                    conn, "system", "draft.stale_deleted" if deleted else "draft.stale_kept", q["id"], at=now
                )


# ------------------------------------------------------------------ one email


def triage_email(
    svc: Services,
    email: InboundEmail,
    *,
    provider: str,
    source: str = "poll",
    mailbox: MailProvider | None = None,
) -> TriageOutcome:
    settings, router = svc.settings, svc.router
    now = svc.clock()
    client = svc.clients.match(email.sender.email)

    with svc.db.session() as conn:
        existing = get_query_by_message(conn, provider, email.provider_id)
    attempts = (existing["attempts"] if existing else 0) + 1

    base: dict[str, Any] = {
        "thread_id": email.thread_id,
        "source": source,
        "sender_email": email.sender.email.lower(),
        "sender_name": email.sender.name,
        "received_at": email.received_at,
        "client_id": client.id if client else None,
        "client_name": client.name if client else None,
        "client_tier": client.tier if client else None,
        "processed_at": now,
        "attempts": attempts,
        "error": None,
    }
    content = {"subject": email.subject, "body_text": email.body_text}

    def finish(query_id: int, labels: list[str]) -> None:
        if mailbox is not None and labels:
            mailbox.apply_labels(email, labels)

    # 1. Auto-replies and bulk mail: no AI call, no reply.
    reason = automated_reason(email)
    if reason:
        labels = [TRIAGED, NO_REPLY, label("Category", CATEGORY_LABELS["automated_or_spam"])]
        with svc.db.session() as conn:
            qid = save_query(
                conn,
                provider,
                email.provider_id,
                {
                    **base,
                    **content,
                    "status": "no_reply_needed",
                    "category": "automated_or_spam",
                    "urgency": "low",
                    "summary": f"{reason}: {email.subject}",
                    "labels": labels,
                    "closed_at": now,
                    "closed_by": "system",
                },
            )
            audit(conn, "system", "query.skipped_automated", qid, at=now, reason=reason)
        finish(qid, labels)
        return TriageOutcome(query_id=qid, status="no_reply_needed", labels=labels)

    # 2. Someone already answered in the mailbox (e.g. the message arrived before the assistant was switched on).
    if mailbox is not None and mailbox.has_reply_after(email.thread_id, email.received_at):
        labels = [TRIAGED]
        with svc.db.session() as conn:
            qid = save_query(
                conn,
                provider,
                email.provider_id,
                {
                    **base,
                    **content,
                    "status": "replied",
                    "labels": labels,
                    "closed_at": now,
                    "closed_by": "mailbox",
                },
            )
            audit(conn, "system", "query.already_replied", qid, at=now)
        finish(qid, labels)
        return TriageOutcome(query_id=qid, status="replied", labels=labels)

    # 3. Confidential clients never reach the AI; only metadata is stored.
    exclusion = svc.privacy.check(email, client)
    if exclusion:
        route = router.route(RouteFacts.build(None, client, excluded=True), client)
        due = router.due_at(email.received_at, "normal", client.tier if client else None)
        labels = [TRIAGED, EXCLUDED, label("Assigned", route.assignee.name)]
        with svc.db.session() as conn:
            qid = save_query(
                conn,
                provider,
                email.provider_id,
                {
                    **base,
                    "subject": None,
                    "body_text": None,
                    "status": "excluded",
                    "excluded_reason": exclusion.reason,
                    "summary": None,
                    "assignee_key": route.assignee.key,
                    "assignee_name": route.assignee.name,
                    "route_rule": route.rule,
                    "route_reason": route.reason,
                    "alerted": route.alert,
                    "due_at": due,
                    "labels": labels,
                },
            )
            audit(conn, "system", "query.excluded", qid, at=now, reason=exclusion.reason)
        _supersede_thread(svc, provider, email, qid, mailbox)
        finish(qid, labels)
        if route.alert:
            svc.notifier.send(
                _alert_message(svc, qid, email, client, None, route, due, draft_link=None, excluded=True), qid
            )
        return TriageOutcome(
            query_id=qid,
            status="excluded",
            labels=labels,
            route=route,
            due_at=due,
            excluded_reason=exclusion.reason,
        )

    # 4. Classify.
    with svc.db.session() as conn:
        qid = save_query(conn, provider, email.provider_id, {**base, **content, "status": "error"})

    def needs_human(classification: Classification | None, why: str) -> TriageOutcome:
        facts = RouteFacts.build(classification, client, needs_human=True)
        route = router.route(facts, client)
        urgency = classification.urgency if classification else "normal"
        due = router.due_at(email.received_at, urgency, client.tier if client else None)
        labels = [TRIAGED, NEEDS_HUMAN, label("Assigned", route.assignee.name)]
        if classification:
            labels.append(label("Urgency", classification.urgency.title()))
        fields: dict[str, Any] = {
            "status": "needs_human",
            "error": why,
            "assignee_key": route.assignee.key,
            "assignee_name": route.assignee.name,
            "route_rule": route.rule,
            "route_reason": route.reason,
            "alerted": route.alert,
            "due_at": due,
            "labels": labels,
        }
        if classification:
            fields.update(_classification_fields(classification))
        with svc.db.session() as conn:
            update_query(conn, qid, **fields)
            audit(conn, "system", "query.needs_human", qid, at=now, reason=why)
        _supersede_thread(svc, provider, email, qid, mailbox)
        finish(qid, labels)
        if route.alert:
            svc.notifier.send(
                _alert_message(svc, qid, email, client, None, route, due, draft_link=None, reason=why), qid
            )
        return TriageOutcome(
            query_id=qid,
            status="needs_human",
            labels=labels,
            classification=classification,
            route=route,
            due_at=due,
            error=why,
        )

    def failed(exc: AssistantError, classification: Classification | None) -> TriageOutcome:
        if not exc.retryable or attempts >= MAX_ATTEMPTS:
            return needs_human(classification, f"AI processing failed after {attempts} attempt(s): {exc}")
        with svc.db.session() as conn:
            update_query(conn, qid, status="error", error=str(exc))
            audit(conn, "system", "query.error", qid, at=now, error=str(exc), attempt=attempts)
        return TriageOutcome(query_id=qid, status="error", error=str(exc))

    try:
        classified: AIResult[Classification] = svc.assistant.classify(email, client)
    except AssistantRefusal as exc:
        _record_usage(svc, qid, "classify", exc.usage)
        return needs_human(None, str(exc))
    except AssistantError as exc:
        _record_usage(svc, qid, "classify", exc.usage)
        return failed(exc, None)
    _record_usage(svc, qid, "classify", classified.usage)
    classification = classified.value
    with svc.db.session() as conn:
        update_query(conn, qid, **_classification_fields(classification))

    # 5. Nothing to answer (thank-you notes and the like).
    if not classification.needs_reply or classification.category == "automated_or_spam":
        labels = [TRIAGED, NO_REPLY, label("Category", CATEGORY_LABELS[classification.category])]
        with svc.db.session() as conn:
            update_query(
                conn, qid, status="no_reply_needed", labels=labels, closed_at=now, closed_by="system"
            )
            audit(conn, "system", "query.no_reply_needed", qid, at=now)
        finish(qid, labels)
        return TriageOutcome(
            query_id=qid, status="no_reply_needed", labels=labels, classification=classification
        )

    # 6. Context: the client record, their recent queries, and knowledge-base sections.
    owner = router.member(client.account_owner) if client else None
    with svc.db.session() as conn:
        history = recent_for_client(conn, client.id, before=email.received_at) if client else []
    sources = client_sources(client, history, owner.name if owner else None)
    queries = classification.kb_queries or [email.subject]
    try:
        sources += svc.kb.search(queries, settings.kb_top_k)
    except Exception as exc:  # the knowledge base being down should not stop triage
        log.warning("Knowledge base search failed: %s", exc)
    with svc.db.session() as conn:
        replace_sources(conn, qid, sources)

    # 7. Draft, keeping only citations of sources we provided.
    try:
        drafted = svc.assistant.draft(email, classification, client, sources)
    except AssistantRefusal as exc:
        _record_usage(svc, qid, "draft", exc.usage)
        return needs_human(classification, str(exc))
    except AssistantError as exc:
        _record_usage(svc, qid, "draft", exc.usage)
        return failed(exc, classification)
    _record_usage(svc, qid, "draft", drafted.usage)
    draft = drafted.value
    citations, dropped = validate_citations(draft.citations, sources)
    warnings: list[str] = []
    if not citations:
        warnings.append("No sources cited: check every fact in this draft")
    if dropped:
        warnings.append(f"{len(dropped)} citation(s) removed because they named unknown sources")
    if min(draft.confidence, classification.confidence) < router.low_confidence_below:
        warnings.append("Low confidence: review carefully")
    full_text = compose_draft(settings, draft, citations, sources, query_id=qid, warnings=warnings)

    ref: DraftRef | None = None
    if mailbox is not None:
        try:
            ref = mailbox.create_reply_draft(email, full_text)
        except Exception as exc:
            return failed(AssistantError(f"Could not save the draft in the mailbox: {exc}"), classification)

    # 8. Route, label, store, alert.
    route = router.route(RouteFacts.build(classification, client), client)
    due = router.due_at(email.received_at, classification.urgency, client.tier if client else None)
    labels = [
        TRIAGED,
        DRAFT_READY,
        label("Urgency", classification.urgency.title()),
        label("Category", CATEGORY_LABELS[classification.category]),
        label("Assigned", route.assignee.name),
    ]
    if client:
        labels.append(label("Client", client.name))
    with svc.db.session() as conn:
        update_query(
            conn,
            qid,
            status="draft_ready",
            assignee_key=route.assignee.key,
            assignee_name=route.assignee.name,
            route_rule=route.rule,
            route_reason=route.reason,
            alerted=route.alert,
            due_at=due,
            labels=labels,
        )
        insert_draft(
            conn,
            qid,
            {
                "body": draft.body,
                "full_text": full_text,
                "citations": [c.model_dump() for c in citations],
                "missing_info": draft.missing_info,
                "dropped_citations": [c.model_dump() for c in dropped],
                "confidence": draft.confidence,
                "unsupported": not citations,
                "provider_draft_id": ref.draft_id if ref else None,
                "provider_version": ref.version if ref else None,
                "web_link": ref.web_link if ref else None,
                "created_at": now,
            },
        )
        audit(conn, "system", "query.draft_ready", qid, at=now, rule=route.rule, assignee=route.assignee.key)
    _supersede_thread(svc, provider, email, qid, mailbox)
    finish(qid, labels)
    if route.alert:
        svc.notifier.send(
            _alert_message(
                svc, qid, email, client, classification, route, due, draft_link=ref.web_link if ref else "n8n"
            ),
            qid,
        )
    return TriageOutcome(
        query_id=qid,
        status="draft_ready",
        labels=labels,
        classification=classification,
        draft=draft,
        draft_text=full_text,
        citations=citations,
        route=route,
        due_at=due,
    )


# ------------------------------------------------------------------ the mailbox loop


def process_inbox(svc: Services) -> PollReport:
    """Triage every new message in the mailbox, then close queries that were answered."""
    if svc.mail is None:
        raise MailboxNotConnected(svc.mail_error or "No mailbox is connected")
    report = PollReport()
    if not _poll_lock.acquire(blocking=False):
        log.info("A poll is already running; skipping this one")
        report.busy = True
        return report
    try:
        mail = svc.mail
        since = svc.clock() - timedelta(days=svc.settings.lookback_days)
        messages = mail.list_new(since, svc.settings.max_messages_per_poll)
        report.fetched = len(messages)
        for email in sorted(messages, key=lambda m: m.received_at):
            if is_outbound(email, svc.settings, mail.mailbox_address):
                report.skipped += 1
                continue
            with svc.db.session() as conn:
                existing = get_query_by_message(conn, mail.name, email.provider_id)
            if existing and (existing["status"] != "error" or existing["attempts"] >= MAX_ATTEMPTS):
                report.skipped += 1
                continue
            try:
                outcome = triage_email(svc, email, provider=mail.name, source="poll", mailbox=mail)
            except Exception:
                log.exception("Triage failed for message %s", email.provider_id)
                report.errors += 1
                continue
            report.processed += 1
            if outcome.status == "draft_ready":
                report.drafts += 1
            elif outcome.status == "needs_human":
                report.needs_human += 1
            elif outcome.status == "excluded":
                report.excluded += 1
            elif outcome.status == "no_reply_needed":
                report.no_reply += 1
            elif outcome.status == "replied":
                report.replied += 1
            elif outcome.status == "error":
                report.errors += 1
        report.closed_by_reply = sync_replies(svc)
    finally:
        _poll_lock.release()
    return report


def poll_safely(svc: Services) -> tuple[PollReport | None, str]:
    """Check the inbox and say what happened. Never raises: used by the dashboard and the scheduler."""
    from .connections import describe_mail_error, is_auth_failure

    if svc.mail is None:
        return None, svc.mail_error or "No mailbox is connected."
    try:
        report = process_inbox(svc)
    except Exception as exc:
        if is_auth_failure(exc):
            log.warning("Mailbox access stopped working: %s", exc)
            mark_disconnected(svc)
            message = svc.mail_error or str(exc)
        else:
            log.exception("Checking the inbox failed")
            message = "Couldn't check the inbox. " + describe_mail_error(exc)
        svc.last_poll = (svc.clock(), message, False)
        return None, message
    message = report.message()
    svc.last_poll = (svc.clock(), message, True)
    return report, message


def mark_disconnected(svc: Services) -> None:
    """The mailbox's credentials stopped working: stop using it and tell the dashboard why."""
    from .connections import auth_failure_message

    svc.mail = None
    svc.mail_error = auth_failure_message(svc.settings)


def sync_replies(svc: Services) -> int:
    """Close open queries whose thread now has a reply sent from the mailbox."""
    if svc.mail is None:
        return 0
    mail = svc.mail
    with svc.db.session() as conn:
        candidates = open_queries(conn, provider=mail.name)
    closed = 0
    now = svc.clock()
    for q in candidates:
        try:
            replied = mail.has_reply_after(q["thread_id"], parse_iso(q["received_at"]))
        except Exception as exc:
            log.warning("Reply check failed for query %s: %s", q["id"], exc)
            continue
        if replied:
            with svc.db.session() as conn:
                update_query(conn, q["id"], status="replied", closed_at=now, closed_by="mailbox")
                audit(conn, "system", "query.replied", q["id"], at=now)
            closed += 1
    return closed


def regenerate_draft(svc: Services, query_id: int, instruction: str | None, actor: str) -> TriageOutcome:
    """Write a new draft for an open query (optionally steered by a teammate) and replace the mailbox draft."""
    with svc.db.session() as conn:
        q = get_query(conn, query_id)
        old = current_draft(conn, query_id) if q else None
    if q is None:
        raise KeyError(query_id)
    if q["status"] == "excluded" or q["body_text"] is None:
        raise ValueError("This query is excluded from AI processing")
    email = InboundEmail(
        provider_id=q["provider_message_id"],
        thread_id=q["thread_id"],
        sender=EmailAddress(email=q["sender_email"], name=q["sender_name"]),
        subject=q["subject"] or "",
        body_text=q["body_text"] or "",
        received_at=parse_iso(q["received_at"]),
    )
    mailbox = svc.mail if svc.mail is not None and svc.mail.name == q["provider"] else None
    if mailbox is not None:
        # Re-read the message for its threading headers and thread context.
        fetched = mailbox.get(q["provider_message_id"])
        if fetched is not None:
            email = fetched
    client = svc.clients.get(q["client_id"])
    classification = Classification.model_validate(
        {
            "category": q["category"] or "other",
            "urgency": q["urgency"] or "normal",
            "urgency_reason": q["urgency_reason"] or "",
            "sentiment": q["sentiment"] or "neutral",
            "summary": q["summary"] or "",
            "needs_reply": True,
            "complexity": q["complexity"] or "simple",
            "confidence": q["confidence"] if q["confidence"] is not None else 0.5,
            "questions": q["questions"] or [],
            "escalation_flags": q["escalation_flags"] or [],
            "kb_queries": [],
            "client_hint": q["client_hint"],
        }
    )
    with svc.db.session() as conn:
        sources = sources_for(conn, query_id)
    now = svc.clock()
    try:
        drafted = svc.assistant.draft(email, classification, client, sources, instruction)
    except (AssistantRefusal, AssistantError) as exc:
        _record_usage(svc, query_id, "draft", exc.usage)
        raise
    _record_usage(svc, query_id, "draft", drafted.usage)
    draft = drafted.value
    citations, dropped = validate_citations(draft.citations, sources)
    warnings = [] if citations else ["No sources cited: check every fact in this draft"]
    if dropped:
        warnings.append(f"{len(dropped)} citation(s) removed because they named unknown sources")
    full_text = compose_draft(svc.settings, draft, citations, sources, query_id=query_id, warnings=warnings)
    ref: DraftRef | None = None
    kept_old = False
    if mailbox is not None:
        ref = mailbox.create_reply_draft(email, full_text)
        if old and old.get("provider_draft_id"):
            old_ref = DraftRef(old["provider_draft_id"], old.get("web_link"), old.get("provider_version"))
            kept_old = not mailbox.delete_draft(old_ref)
    with svc.db.session() as conn:
        supersede_drafts(conn, query_id, now)
        insert_draft(
            conn,
            query_id,
            {
                "body": draft.body,
                "full_text": full_text,
                "citations": [c.model_dump() for c in citations],
                "missing_info": draft.missing_info,
                "dropped_citations": [c.model_dump() for c in dropped],
                "confidence": draft.confidence,
                "unsupported": not citations,
                "instruction": instruction,
                "provider_draft_id": ref.draft_id if ref else None,
                "provider_version": ref.version if ref else None,
                "web_link": ref.web_link if ref else None,
                "created_at": now,
            },
        )
        if q["status"] == "needs_human":
            update_query(conn, query_id, status="draft_ready")
        audit(
            conn, actor, "draft.regenerated", query_id, at=now, instruction=instruction, kept_edited=kept_old
        )
    return TriageOutcome(
        query_id=query_id,
        status="draft_ready",
        classification=classification,
        draft=draft,
        draft_text=full_text,
        citations=citations,
    )
