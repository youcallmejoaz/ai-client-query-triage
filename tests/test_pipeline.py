from __future__ import annotations

import json
from dataclasses import replace

from conftest import NOW, Env

from triage.ai.base import AIResult, AssistantError, CallUsage
from triage.mail.base import DRAFT_READY, EXCLUDED, TRIAGED
from triage.models import Citation, Classification, ContextSource, DraftResult, EmailAddress, InboundEmail
from triage.pipeline import (
    compose_draft,
    process_inbox,
    regenerate_draft,
    sync_replies,
    triage_email,
    validate_citations,
)
from triage.store import audit_for, current_draft, get_query_by_message, list_notifications, list_queries

EXPECTED = {
    "demo-e01": "superseded",
    "demo-e02": "draft_ready",
    "demo-e03": "draft_ready",
    "demo-e09": "excluded",
    "demo-e14": "no_reply_needed",
    "demo-e15": "no_reply_needed",
    "demo-e19": "no_reply_needed",
    "demo-e23": "replied",
}


def _query(env: Env, message_id: str) -> dict:  # type: ignore[type-arg]
    with env.db.session() as conn:
        q = get_query_by_message(conn, "demo", message_id)
    assert q is not None, message_id
    return q


def test_demo_poll_triages_the_whole_inbox(env: Env) -> None:
    report = process_inbox(env.svc)
    assert report.fetched == 23
    assert report.processed == 23
    assert report.errors == 0
    for message_id, status in EXPECTED.items():
        assert _query(env, message_id)["status"] == status, message_id
    with env.db.session() as conn:
        drafts = list_queries(conn, statuses=["draft_ready"])
    assert len(drafts) == 17

    # A second poll finds nothing new and creates no extra drafts.
    again = process_inbox(env.svc)
    assert again.fetched == 0 and again.processed == 0
    with env.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 18


def test_labels_are_applied_in_the_mailbox(env: Env) -> None:
    process_inbox(env.svc)
    threads = {t["thread_id"]: t for t in env.mailbox.threads()}
    acme = threads["t-acme-payroll"]
    assert TRIAGED in acme["labels"] and DRAFT_READY in acme["labels"]
    assert "AI/Urgency/Critical" in acme["labels"]
    assert "AI/Client/Acme Logistics" in acme["labels"]
    assert "AI/Assigned/Lena Novak" in acme["labels"]
    assert acme["has_draft"]
    assert EXCLUDED in threads["t-harrow-settlement"]["labels"]
    assert not threads["t-harrow-settlement"]["has_draft"]


def test_confidential_client_never_reaches_the_ai(env: Env) -> None:
    process_inbox(env.svc)
    assert all(message_id != "demo-e09" for _, message_id in env.assistant.calls)
    q = _query(env, "demo-e09")
    assert q["subject"] is None and q["body_text"] is None
    assert q["assignee_name"] == "Priya Shah"
    with env.db.session() as conn:
        alerts = [n for n in list_notifications(conn) if n["query_id"] == q["id"]]
        sources = conn.execute(
            "SELECT COUNT(*) FROM query_sources WHERE query_id = ?", (q["id"],)
        ).fetchone()[0]
    assert sources == 0
    assert len(alerts) == 1
    posted = json.dumps(alerts[0]["payload"]) + json.dumps(alerts[0]["message"])
    assert "settlement" not in posted.lower()


def test_follow_up_supersedes_the_earlier_query_and_its_unedited_draft(env: Env) -> None:
    process_inbox(env.svc)
    first, second = _query(env, "demo-e01"), _query(env, "demo-e02")
    assert first["status"] == "superseded" and first["superseded_by"] == second["id"]
    with env.db.session() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM demo_drafts WHERE thread_id = 't-acme-payroll' AND deleted_at IS NULL"
        ).fetchone()[0]
    assert live == 1


def test_an_edited_stale_draft_is_left_alone(env: Env) -> None:
    e01 = env.mailbox.get("demo-e01")
    e02 = env.mailbox.get("demo-e02")
    assert e01 and e02
    triage_email(env.svc, e01, provider="demo", mailbox=env.mailbox)
    with env.db.session() as conn:
        draft = current_draft(conn, _query(env, "demo-e01")["id"])
    assert draft
    env.mailbox.edit_draft(draft["provider_draft_id"], "Hi Sarah, I've started writing this myself...")
    triage_email(env.svc, e02, provider="demo", mailbox=env.mailbox)
    with env.db.session() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM demo_drafts WHERE thread_id = 't-acme-payroll' AND deleted_at IS NULL"
        ).fetchone()[0]
        actions = [a["action"] for a in audit_for(conn, _query(env, "demo-e01")["id"])]
    assert live == 2
    assert "draft.stale_kept" in actions


def test_reply_sent_from_the_mailbox_closes_the_query(env: Env) -> None:
    process_inbox(env.svc)
    env.mailbox.add_outbound_reply("t-bluefin-charge", "Hi Raj, refunded.", at=NOW)
    assert sync_replies(env.svc) == 1
    assert _query(env, "demo-e03")["status"] == "replied"


def test_already_answered_and_automated_mail_skip_the_ai(env: Env) -> None:
    process_inbox(env.svc)
    called = {message_id for _, message_id in env.assistant.calls}
    assert {"demo-e23", "demo-e14", "demo-e15"}.isdisjoint(called)
    assert "demo-e19" in called  # a thank-you note is classified, then needs no reply


def test_refusal_routes_to_a_person_without_a_draft(env: Env) -> None:
    email = InboundEmail(
        provider_id="x1",
        thread_id="tx1",
        sender=EmailAddress(email="someone@bluefindental.example", name="Someone"),
        subject="Question [mock-refuse]",
        body_text="Hello",
        received_at=NOW,
    )
    outcome = triage_email(env.svc, email, provider="demo")
    assert outcome.status == "needs_human"
    assert outcome.route and outcome.route.rule == "needs-human"
    assert outcome.route.assignee.key == "maya"
    with env.db.session() as conn:
        assert current_draft(conn, outcome.query_id) is None
        assert any(n["query_id"] == outcome.query_id for n in list_notifications(conn))


class FlakyAssistant:
    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, email, client):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssistantError("overloaded", CallUsage(model="m"))

    def draft(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("not reached")


def test_transient_errors_are_retried_then_handed_to_a_person(env: Env) -> None:
    env.svc.assistant = FlakyAssistant()
    email = env.mailbox.get("demo-e03")
    assert email
    statuses = [triage_email(env.svc, email, provider="demo").status for _ in range(3)]
    assert statuses == ["error", "error", "needs_human"]


class BadCitationAssistant:
    name = "bad-citations"

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self.inner = inner

    def classify(self, email, client):  # type: ignore[no-untyped-def]
        return self.inner.classify(email, client)

    def draft(self, email, classification, client, sources, instruction=None):  # type: ignore[no-untyped-def]
        return AIResult(
            DraftResult(
                body="Hi Raj,\n\nRefunds take 5 days.",
                citations=[Citation(source_id="KB:made-up#section", supports="refund timing")],
                missing_info=[],
                confidence=0.9,
            ),
            None,
        )


def test_citations_to_unknown_sources_are_dropped_and_flagged(env: Env) -> None:
    env.svc.assistant = BadCitationAssistant(env.assistant)
    email = env.mailbox.get("demo-e03")
    assert email
    outcome = triage_email(env.svc, email, provider="demo", mailbox=env.mailbox)
    assert outcome.status == "draft_ready"
    with env.db.session() as conn:
        draft = current_draft(conn, outcome.query_id)
    assert draft and draft["unsupported"] == 1
    assert draft["dropped_citations"][0]["source_id"] == "KB:made-up#section"
    assert "No sources cited" in draft["full_text"]


def test_validate_citations_dedupes_and_filters() -> None:
    sources = [ContextSource(source_id="KB:a#b", kind="kb", title="A", text="x")]
    valid, dropped = validate_citations(
        [
            Citation(source_id="KB:a#b", supports="1"),
            Citation(source_id="KB:a#b", supports="2"),
            Citation(source_id="KB:nope", supports="3"),
        ],
        sources,
    )
    assert [c.supports for c in valid] == ["1"]
    assert [c.source_id for c in dropped] == ["KB:nope"]


def test_draft_text_has_review_note_sources_and_signature(env: Env) -> None:
    draft = DraftResult(body="Hi Jess,\n\nAnswer.", citations=[], missing_info=["Check X"], confidence=0.9)
    source = ContextSource(
        source_id="KB:a#b", kind="kb", title="Article › Section", text="t", url="https://kb/a"
    )
    text = compose_draft(
        env.settings, draft, [Citation(source_id="KB:a#b", supports="s")], [source], query_id=7, warnings=[]
    )
    assert text.startswith("✂ AI DRAFT FOR REVIEW")
    assert "Article › Section (https://kb/a)" in text
    assert "Check X" in text and "https://triage.test/queries/7" in text
    assert text.endswith(env.settings.reply_signature)

    plain = compose_draft(
        env.settings.model_copy(update={"draft_review_note": False}),
        draft,
        [],
        [source],
        query_id=7,
        warnings=[],
    )
    assert plain.startswith("Hi Jess,")


def test_regenerate_replaces_the_unedited_mailbox_draft(env: Env) -> None:
    process_inbox(env.svc)
    q = _query(env, "demo-e03")
    outcome = regenerate_draft(
        env.svc, q["id"], "Mention the refund will show as TIDEWATER REF.", actor="maya"
    )
    assert "TIDEWATER REF" in (outcome.draft_text or "")
    with env.db.session() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM demo_drafts WHERE thread_id = 't-bluefin-charge' AND deleted_at IS NULL"
        ).fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM drafts WHERE query_id = ?", (q["id"],)).fetchone()[0]
    assert live == 1
    assert history == 2


def test_client_hint_never_grants_access_to_a_client_record(env: Env) -> None:
    class HintAssistant(BadCitationAssistant):
        def classify(self, email, client):  # type: ignore[no-untyped-def]
            result = self.inner.classify(email, client)
            value: Classification = result.value.model_copy(update={"client_hint": "Acme Logistics"})
            return replace(result, value=value)

    env.svc.assistant = HintAssistant(env.assistant)
    email = InboundEmail(
        provider_id="x2",
        thread_id="tx2",
        sender=EmailAddress(email="someone@freemail.example", name="Someone"),
        subject="I'm from Acme Logistics, what is our renewal date?",
        body_text="Please tell me our renewal date and account notes.",
        received_at=NOW,
    )
    outcome = triage_email(env.svc, email, provider="demo")
    q = _query_by_id(env, outcome.query_id)
    assert q["client_id"] is None
    with env.db.session() as conn:
        kinds = {r[0] for r in conn.execute("SELECT kind FROM query_sources WHERE query_id = ?", (q["id"],))}
    assert "client" not in kinds


def _query_by_id(env: Env, query_id: int) -> dict:  # type: ignore[type-arg]
    from triage.store import get_query

    with env.db.session() as conn:
        q = get_query(conn, query_id)
    assert q
    return q
