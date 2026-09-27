"""Routing, SLA, privacy and client matching."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW

from triage.config import DEMO_DIR
from triage.context.clients import ClientDirectory
from triage.models import ClientRecord, EmailAddress, InboundEmail, TeamMember
from triage.privacy import PrivacyPolicy
from triage.routing import RouteFacts, Router, Rule

router = Router.from_yaml(DEMO_DIR / "routing.yaml")
clients = ClientDirectory.from_csv(DEMO_DIR / "clients.csv")
privacy = PrivacyPolicy.from_yaml(DEMO_DIR / "privacy.yaml")


@pytest.mark.parametrize(
    ("facts", "client_id", "rule", "assignee", "alert"),
    [
        (RouteFacts(excluded=True), "harrow-pike", "confidential-client", "priya", True),
        (
            RouteFacts(category="legal_compliance", urgency="high", escalation_flags=["regulatory"]),
            "marlow-hotels",
            "legal-security-regulatory",
            "dana",
            True,
        ),
        (
            RouteFacts(category="complaint", urgency="normal", escalation_flags=["churn_risk"]),
            "kestrel-retail",
            "churn-risk",
            "priya",
            True,
        ),
        (RouteFacts(needs_human=True), "bluefin-dental", "needs-human", "maya", True),
        (
            RouteFacts(category="technical_issue", urgency="critical"),
            "acme-logistics",
            "payroll-emergency",
            "lena",
            True,
        ),
        (RouteFacts(category="billing", urgency="normal"), "bluefin-dental", "billing", "omar", False),
        (RouteFacts(category="billing", urgency="high"), "bluefin-dental", "billing", "omar", True),
        (RouteFacts(category="cancellation", urgency="normal"), "delta-build", "cancellation", "priya", True),
        (RouteFacts(category="account_access", urgency="normal"), "delta-build", "technical", "lena", False),
        (
            RouteFacts(category="onboarding", tier="enterprise", complexity="complex"),
            "acme-logistics",
            "complex-enterprise",
            "priya",
            True,
        ),
        (RouteFacts(category="general_question", unknown_client=True), None, "unknown-sender", "maya", False),
        (RouteFacts(category="scheduling", urgency="low"), "orchard-schools", "default", "tom", False),
    ],
)
def test_routing_rules(
    facts: RouteFacts, client_id: str | None, rule: str, assignee: str, alert: bool
) -> None:
    client = clients.get(client_id)
    if client:
        facts.tier = facts.tier or client.tier
    decision = router.route(facts, client)
    assert (decision.rule, decision.assignee.key, decision.alert) == (rule, assignee, alert)


def test_route_facts_from_classification_and_client() -> None:
    client = clients.get("delta-build")
    facts = RouteFacts.build(None, client, excluded=True)
    assert facts.tier == "enterprise" and facts.excluded and not facts.unknown_client


def test_sla_due_times_scale_with_urgency_and_tier() -> None:
    assert router.due_at(NOW, "critical", "enterprise") == NOW + timedelta(hours=1)
    assert router.due_at(NOW, "high", "growth") == NOW + timedelta(hours=4)
    assert router.due_at(NOW, "low", None) == NOW + timedelta(hours=72)
    assert router.due_at(NOW, None, "starter") == NOW + timedelta(hours=24)


def test_unknown_team_member_or_condition_is_rejected() -> None:
    team = [TeamMember(key="a", name="A", email="a@x")]
    with pytest.raises(ValueError, match="unknown team member"):
        Router(team, [Rule("r", {}, "nobody")], "a", {}, {}, [], 0.5)
    bad = Router(team, [Rule("r", {"colour": "red"}, "a")], "a", {}, {}, [], 0.5)
    with pytest.raises(ValueError, match="Unknown routing condition"):
        bad.route(RouteFacts(), None)


def _email(sender: str, subject: str = "Hello", cc: list[str] | None = None) -> InboundEmail:
    return InboundEmail(
        provider_id="m",
        thread_id="t",
        sender=EmailAddress(email=sender),
        cc=[EmailAddress(email=a) for a in cc or []],
        subject=subject,
        received_at=NOW,
    )


def test_privacy_exclusions() -> None:
    assert privacy.check(_email("j@harrowpike.example"), None)
    assert privacy.check(_email("j@london.harrowpike.example"), None)
    assert privacy.check(_email("a@acmelogistics.example", "[Confidential] merger"), None)
    assert privacy.check(_email("a@acmelogistics.example", cc=["j@harrowpike.example"]), None)
    excluded_client = ClientRecord(id="x", name="X", ai_excluded=True)
    assert privacy.check(_email("a@x.example"), excluded_client)
    assert privacy.check(_email("a@acmelogistics.example", "Invoice"), clients.get("acme-logistics")) is None


def test_client_matching() -> None:
    assert clients.match("Sarah.Okafor@acmelogistics.example").id == "acme-logistics"  # type: ignore[union-attr]
    assert clients.match("ops@uk.acmelogistics.example").id == "acme-logistics"  # type: ignore[union-attr]
    assert clients.match("hello.copperleafcafe@gmail.example").id == "copperleaf-cafe"  # type: ignore[union-attr]
    # A free-mail domain never matches a client by domain alone.
    assert clients.match("someone.else@gmail.example") is None
    assert clients.match("person@unknown.example") is None
    assert clients.get("harrow-pike").ai_excluded  # type: ignore[union-attr]
