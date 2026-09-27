"""Client records: who is writing, which tier they are on, who owns the account."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..models import CATEGORY_LABELS, ClientRecord, ContextSource

# Free-mail domains never identify a client by domain; only an exact contact address does.
PERSONAL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "gmail.example",
}


def _split(value: str) -> list[str]:
    return [part.strip().lower() for part in value.replace(",", ";").split(";") if part.strip()]


class ClientDirectory:
    """Matches senders to client records loaded from a CSV export (a CRM adapter can replace it)."""

    def __init__(self, clients: Sequence[ClientRecord]) -> None:
        self.clients = list(clients)
        self._by_id = {c.id: c for c in self.clients}
        self._by_contact = {addr: c for c in self.clients for addr in c.contacts}
        self._by_domain = {d: c for c in self.clients for d in c.domains}

    @classmethod
    def from_csv(cls, path: Path) -> ClientDirectory:
        if not path.exists():
            return cls([])
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        clients = [
            ClientRecord(
                id=row["id"].strip(),
                name=row["name"].strip(),
                domains=_split(row.get("domains", "")),
                contacts=_split(row.get("contacts", "")),
                tier=(row.get("tier") or "standard").strip().lower(),
                plan=(row.get("plan") or "").strip(),
                account_owner=(row.get("account_owner") or "").strip() or None,
                ai_excluded=(row.get("ai_excluded") or "").strip().lower() in {"true", "yes", "1"},
                status=(row.get("status") or "active").strip(),
                renewal_date=(row.get("renewal_date") or "").strip() or None,
                notes=(row.get("notes") or "").strip(),
            )
            for row in rows
            if row.get("id")
        ]
        return cls(clients)

    def get(self, client_id: str | None) -> ClientRecord | None:
        return self._by_id.get(client_id) if client_id else None

    def match(self, email: str) -> ClientRecord | None:
        """Exact contact address first, then the sender's domain (never a free-mail domain)."""
        email = email.strip().lower()
        if email in self._by_contact:
            return self._by_contact[email]
        domain = email.rpartition("@")[2]
        if domain in PERSONAL_DOMAINS:
            return None
        # Also match subdomains, e.g. uk.acmelogistics.example.
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self._by_domain:
                return self._by_domain[candidate]
        return None

    def match_hint(self, hint: str | None) -> ClientRecord | None:
        """Accept the model's client_hint only when it names a client exactly (case-insensitive)."""
        if not hint:
            return None
        wanted = hint.strip().lower()
        for client in self.clients:
            if client.name.lower() == wanted or client.id == wanted:
                return client
        return None


def client_sources(
    client: ClientRecord | None, history: Sequence[dict[str, Any]], owner_name: str | None = None
) -> list[ContextSource]:
    """The client record and recent queries from the same client, as citable sources."""
    if client is None:
        return []
    lines = [
        f"Client: {client.name}",
        f"Tier: {client.tier}",
        f"Plan: {client.plan}" if client.plan else "",
        f"Account manager: {owner_name}" if owner_name else "",
        f"Account status: {client.status}",
        f"Renewal date: {client.renewal_date}" if client.renewal_date else "",
        f"Notes: {client.notes}" if client.notes else "",
    ]
    sources = [
        ContextSource(
            source_id=f"CLIENT:{client.id}",
            kind="client",
            title=f"Client record: {client.name}",
            text="\n".join(line for line in lines if line),
        )
    ]
    if history:
        items = []
        for row in history:
            received = str(row.get("received_at", ""))[:10]
            category = CATEGORY_LABELS.get(str(row.get("category")), str(row.get("category") or ""))
            items.append(f"- {received} [{category}, {row.get('status')}] {row.get('summary')}")
        sources.append(
            ContextSource(
                source_id=f"HISTORY:{client.id}",
                kind="history",
                title=f"Recent queries from {client.name}",
                text="\n".join(items),
            )
        )
    return sources
