"""Routing rules (routing.yaml): who handles a query, whether to alert now, and when it is due."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .models import Classification, ClientRecord, RouteDecision, TeamMember


@dataclass
class RouteFacts:
    """Everything a rule can match on."""

    category: str | None = None
    urgency: str | None = None
    complexity: str | None = None
    tier: str | None = None
    escalation_flags: list[str] = field(default_factory=list)
    excluded: bool = False
    needs_human: bool = False
    unknown_client: bool = False

    @classmethod
    def build(
        cls,
        classification: Classification | None,
        client: ClientRecord | None,
        *,
        excluded: bool = False,
        needs_human: bool = False,
    ) -> RouteFacts:
        return cls(
            category=classification.category if classification else None,
            urgency=classification.urgency if classification else None,
            complexity=classification.complexity if classification else None,
            tier=client.tier if client else None,
            escalation_flags=list(classification.escalation_flags) if classification else [],
            excluded=excluded,
            needs_human=needs_human,
            unknown_client=client is None,
        )


@dataclass
class Rule:
    name: str
    when: dict[str, Any]
    assign: str
    alert: bool = False

    def matches(self, facts: RouteFacts) -> bool:
        for key, expected in self.when.items():
            if key == "escalation_flags_any":
                if not set(expected) & set(facts.escalation_flags):
                    return False
            elif key in ("excluded", "needs_human", "unknown_client"):
                if bool(getattr(facts, key)) != bool(expected):
                    return False
            elif key in ("category", "urgency", "complexity", "tier"):
                allowed = expected if isinstance(expected, list) else [expected]
                if getattr(facts, key) not in allowed:
                    return False
            else:
                raise ValueError(f"Unknown routing condition {key!r} in rule {self.name!r}")
        return True


class Router:
    def __init__(
        self,
        team: list[TeamMember],
        rules: list[Rule],
        triage_lead: str,
        sla_hours: dict[str, float],
        tier_multiplier: dict[str, float],
        alert_urgencies: list[str],
        low_confidence_below: float,
    ) -> None:
        self.team = {m.key: m for m in team}
        self.rules = rules
        self.triage_lead = triage_lead
        self.sla_hours = sla_hours
        self.tier_multiplier = tier_multiplier
        self.alert_urgencies = alert_urgencies
        self.low_confidence_below = low_confidence_below
        if triage_lead not in self.team:
            raise ValueError(f"triage_lead {triage_lead!r} is not in the team list")
        for rule in rules:
            if rule.assign not in ("account_owner", "triage_lead") and rule.assign not in self.team:
                raise ValueError(f"Rule {rule.name!r} assigns to unknown team member {rule.assign!r}")

    @classmethod
    def from_yaml(cls, path: Path) -> Router:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        sla = data.get("sla") or {}
        return cls(
            team=[TeamMember(**m) for m in data["team"]],
            rules=[
                Rule(name=r["name"], when=r.get("when") or {}, assign=r["assign"], alert=bool(r.get("alert")))
                for r in data["rules"]
            ],
            triage_lead=data["triage_lead"],
            sla_hours={k: float(v) for k, v in (sla.get("hours") or {}).items()},
            tier_multiplier={k: float(v) for k, v in (sla.get("tier_multiplier") or {}).items()},
            alert_urgencies=list(data.get("alert_urgencies") or ["critical", "high"]),
            low_confidence_below=float(data.get("low_confidence_below", 0.55)),
        )

    def member(self, key: str | None) -> TeamMember | None:
        return self.team.get(key) if key else None

    def _resolve(self, assign: str, client: ClientRecord | None) -> TeamMember:
        if assign == "account_owner" and client and client.account_owner in self.team:
            return self.team[client.account_owner]
        if assign in self.team:
            return self.team[assign]
        return self.team[self.triage_lead]

    def route(self, facts: RouteFacts, client: ClientRecord | None) -> RouteDecision:
        for rule in self.rules:
            if rule.matches(facts):
                assignee = self._resolve(rule.assign, client)
                alert = rule.alert or (facts.urgency in self.alert_urgencies)
                return RouteDecision(
                    assignee=assignee, rule=rule.name, alert=alert, reason=self._reason(rule, facts)
                )
        lead = self.team[self.triage_lead]
        return RouteDecision(
            assignee=lead,
            rule="fallback",
            alert=facts.urgency in self.alert_urgencies,
            reason="No rule matched",
        )

    @staticmethod
    def _reason(rule: Rule, facts: RouteFacts) -> str:
        if not rule.when:
            return "Default: the client's account owner"
        parts = []
        for key, value in rule.when.items():
            if key == "escalation_flags_any":
                hit = sorted(set(value) & set(facts.escalation_flags))
                parts.append("flags: " + ", ".join(f.replace("_", " ") for f in hit))
            elif isinstance(value, bool):
                parts.append(key.replace("_", " "))
            else:
                parts.append(f"{key} = {getattr(facts, key)}")
        return f"Rule {rule.name!r} ({'; '.join(parts)})"

    def due_at(self, received_at: datetime, urgency: str | None, tier: str | None) -> datetime:
        hours = self.sla_hours.get(urgency or "normal", self.sla_hours.get("normal", 24.0))
        multiplier = self.tier_multiplier.get(tier or "standard", 1.0)
        return received_at + timedelta(hours=hours * multiplier)
