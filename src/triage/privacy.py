"""Confidential-client exclusion: decides, before any AI call, whether a message may be processed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import ClientRecord, InboundEmail


@dataclass(frozen=True)
class Exclusion:
    reason: str


@dataclass
class PrivacyPolicy:
    excluded_domains: set[str] = field(default_factory=set)
    excluded_addresses: set[str] = field(default_factory=set)
    subject_markers: list[str] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> PrivacyPolicy:
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            excluded_domains={d.lower() for d in data.get("excluded_domains") or []},
            excluded_addresses={a.lower() for a in data.get("excluded_addresses") or []},
            subject_markers=[m.lower() for m in data.get("subject_markers") or []],
        )

    def check(self, email: InboundEmail, client: ClientRecord | None) -> Exclusion | None:
        sender = email.sender.email.lower()
        domain = sender.rpartition("@")[2]
        if client is not None and client.ai_excluded:
            return Exclusion(f"client {client.name} is excluded from AI processing")
        if sender in self.excluded_addresses:
            return Exclusion("sender address is on the exclusion list")
        if any(domain == d or domain.endswith("." + d) for d in self.excluded_domains):
            return Exclusion(f"sender domain {domain} is on the exclusion list")
        subject = email.subject.lower()
        for marker in self.subject_markers:
            if marker in subject:
                return Exclusion(f"subject is marked {marker!r}")
        # Everyone else on the thread counts too: a confidential client cc'd on a thread keeps it private.
        for addr in [*email.to, *email.cc]:
            other = addr.email.lower()
            other_domain = other.rpartition("@")[2]
            if other in self.excluded_addresses or any(
                other_domain == d or other_domain.endswith("." + d) for d in self.excluded_domains
            ):
                return Exclusion("an excluded party is on the thread")
        return None
