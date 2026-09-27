from __future__ import annotations

import json
from datetime import timedelta

import httpx
from conftest import NOW, Env

from triage.db import Database
from triage.digest import build_digest, digest_message
from triage.models import TeamMember
from triage.notify.base import Message, Notifier
from triage.notify.slack import slack_payload
from triage.notify.teams import teams_payload
from triage.pipeline import process_inbox
from triage.store import list_notifications, update_query

MAYA = TeamMember(key="maya", name="Maya Chen", email="maya@x", slack_id="U04MAYA", teams_upn="maya@x")
MESSAGE = Message(
    kind="alert",
    title="Critical: Technical issue from Acme <Logistics>",
    severity="critical",
    lines=["Payroll failed"],
    facts=[("Assigned to", "Lena")],
    links=[("Open in dashboard", "https://triage.test/queries/1")],
    mention=MAYA,
)


def test_slack_payload() -> None:
    payload = slack_payload(MESSAGE)
    assert payload["text"].startswith(":rotating_light:")
    blocks = payload["blocks"]
    assert blocks[0]["type"] == "header"
    assert "<@U04MAYA>" in blocks[1]["text"]["text"]
    assert blocks[-1]["elements"][0]["url"] == "https://triage.test/queries/1"


def test_teams_payload_is_an_adaptive_card_with_a_mention() -> None:
    payload = teams_payload(MESSAGE)
    card = payload["attachments"][0]["content"]
    assert payload["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["type"] == "AdaptiveCard" and card["body"][0]["color"] == "Attention"
    assert card["msteams"]["entities"][0]["mentioned"]["id"] == "maya@x"
    assert card["actions"][0]["type"] == "Action.OpenUrl"


def test_webhook_delivery_and_failure_are_recorded(env: Env) -> None:
    posted: list[dict] = []  # type: ignore[type-arg]

    def ok(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    settings = env.settings.model_copy(
        update={"notify": "slack", "slack_webhook_url": "https://hooks.test/x"}
    )
    assert Notifier(settings, env.db, httpx.Client(transport=httpx.MockTransport(ok))).send(MESSAGE)
    assert posted and posted[0]["blocks"][0]["type"] == "header"

    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert not Notifier(settings, env.db, failing).send(MESSAGE)
    missing = env.settings.model_copy(update={"notify": "teams"})
    assert not Notifier(missing, env.db).send(MESSAGE)
    with env.db.session() as conn:
        rows = list_notifications(conn)
    assert [r["delivered"] for r in rows] == [0, 0, 1]
    assert "TEAMS_WEBHOOK_URL is not set" in rows[0]["error"]


def test_digest_counts(env: Env) -> None:
    process_inbox(env.svc)
    digest = build_digest(env.db, NOW)
    assert digest.open_total == 18  # 17 drafts + 1 confidential
    assert digest.excluded == 1
    # Overdue at NOW: Bluefin duplicate charge (high, growth: 4h), Acme PO invoice and Marlow credit
    # (normal, enterprise: 12h) and Marlow's subject access request (high, enterprise: 2h).
    assert digest.overdue_total == 4, [i.summary for i in digest.overdue]
    assert [i.who for i in digest.overdue][:2] == ["Marlow Hotels", "Acme Logistics"]
    assert digest.by_assignee[0][0] == "Omar Haddad"

    # Resolving one moves it out of the open count.
    with env.db.session() as conn:
        qid = conn.execute("SELECT id FROM queries WHERE provider_message_id = 'demo-e03'").fetchone()[0]
        update_query(conn, qid, status="resolved", closed_at=NOW)
    later = build_digest(env.db, NOW + timedelta(minutes=1))
    assert later.open_total == 17
    assert later.closed_24h == 2  # the resolved one + the P60 question already answered in the mailbox

    message = digest_message(later, env.settings)
    assert message.title == "Inbox summary: 17 open, 3 overdue"
    assert any(line.startswith("Longest overdue") for line in message.lines)


def test_empty_digest() -> None:
    digest = build_digest(Database(":memory:"), NOW)
    assert digest.open_total == 0 and digest.overdue == []
