"""The daily summary of open and overdue queries. Numbers come from the database, not from the model."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database
from .models import CATEGORY_LABELS, OPEN_STATUSES
from .notify.base import Message
from .store import URGENCY_ORDER
from .timeutil import humanize_delta, iso, parse_iso


@dataclass
class DigestItem:
    id: int
    who: str
    summary: str
    urgency: str | None
    status: str
    assignee: str | None
    received_at: datetime
    due_at: datetime | None
    overdue_by: str | None


@dataclass
class Digest:
    generated_at: datetime
    open_total: int = 0
    overdue_total: int = 0
    due_next_4h: int = 0
    needs_human: int = 0
    excluded: int = 0
    new_24h: int = 0
    drafts_24h: int = 0
    closed_24h: int = 0
    by_category: list[tuple[str, int, int]] = field(default_factory=list)
    by_assignee: list[tuple[str, int, int]] = field(default_factory=list)
    overdue: list[DigestItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": iso(self.generated_at),
            "open_total": self.open_total,
            "overdue_total": self.overdue_total,
            "due_next_4h": self.due_next_4h,
            "needs_human": self.needs_human,
            "excluded": self.excluded,
            "new_24h": self.new_24h,
            "drafts_24h": self.drafts_24h,
            "closed_24h": self.closed_24h,
            "by_category": [{"category": c, "open": o, "overdue": d} for c, o, d in self.by_category],
            "by_assignee": [{"assignee": a, "open": o, "overdue": d} for a, o, d in self.by_assignee],
            "overdue": [
                {
                    "id": i.id,
                    "who": i.who,
                    "summary": i.summary,
                    "urgency": i.urgency,
                    "assignee": i.assignee,
                    "due_at": iso(i.due_at) if i.due_at else None,
                    "overdue_by": i.overdue_by,
                }
                for i in self.overdue
            ],
        }


def build_digest(db: Database, now: datetime, oldest: int = 5) -> Digest:
    marks = ", ".join("?" for _ in OPEN_STATUSES)
    since = iso(now - timedelta(hours=24))
    digest = Digest(generated_at=now)
    with db.session() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM queries WHERE status IN ({marks}) ORDER BY {URGENCY_ORDER}, due_at",
                OPEN_STATUSES,
            ).fetchall()
        ]
        digest.new_24h = conn.execute(
            "SELECT COUNT(*) FROM queries WHERE received_at >= ? AND status NOT IN ('no_reply_needed', 'superseded')",
            (since,),
        ).fetchone()[0]
        digest.drafts_24h = conn.execute(
            "SELECT COUNT(DISTINCT query_id) FROM drafts WHERE created_at >= ?", (since,)
        ).fetchone()[0]
        digest.closed_24h = conn.execute(
            "SELECT COUNT(*) FROM queries WHERE status IN ('replied', 'resolved') AND closed_at >= ?",
            (since,),
        ).fetchone()[0]

    categories: dict[str, list[int]] = {}
    assignees: dict[str, list[int]] = {}
    overdue: list[DigestItem] = []
    for r in rows:
        due = parse_iso(r["due_at"]) if r["due_at"] else None
        is_overdue = due is not None and due < now
        digest.open_total += 1
        digest.overdue_total += is_overdue
        digest.due_next_4h += bool(due and now <= due < now + timedelta(hours=4))
        digest.needs_human += r["status"] in ("needs_human", "error")
        digest.excluded += r["status"] == "excluded"
        cat = CATEGORY_LABELS.get(
            r["category"], "Confidential" if r["status"] == "excluded" else "Unclassified"
        )
        categories.setdefault(cat, [0, 0])
        categories[cat][0] += 1
        categories[cat][1] += is_overdue
        who = r["assignee_name"] or "Unassigned"
        assignees.setdefault(who, [0, 0])
        assignees[who][0] += 1
        assignees[who][1] += is_overdue
        if is_overdue and due is not None:
            overdue.append(
                DigestItem(
                    id=r["id"],
                    who=r["client_name"] or r["sender_name"] or r["sender_email"],
                    summary=r["summary"]
                    or ("Confidential: open in the mailbox" if r["status"] == "excluded" else ""),
                    urgency=r["urgency"],
                    status=r["status"],
                    assignee=r["assignee_name"],
                    received_at=parse_iso(r["received_at"]),
                    due_at=due,
                    overdue_by=humanize_delta((now - due).total_seconds()),
                )
            )
    digest.by_category = sorted(((c, o, d) for c, (o, d) in categories.items()), key=lambda x: (-x[2], -x[1]))
    digest.by_assignee = sorted(((a, o, d) for a, (o, d) in assignees.items()), key=lambda x: (-x[2], -x[1]))
    digest.overdue = sorted(overdue, key=lambda i: i.due_at or now)[:oldest]
    return digest


def digest_message(digest: Digest, settings: Settings) -> Message:
    base = settings.public_url.rstrip("/")
    lines = [
        f"{digest.open_total} open · {digest.overdue_total} overdue · {digest.due_next_4h} due in the next 4 hours",
        f"Last 24h: {digest.new_24h} new, {digest.drafts_24h} drafted, {digest.closed_24h} answered or resolved",
    ]
    if digest.needs_human or digest.excluded:
        lines.append(
            f"Without an AI draft: {digest.needs_human} need a person, {digest.excluded} confidential"
        )
    if digest.overdue:
        lines.append("Longest overdue:")
        for item in digest.overdue:
            summary = textwrap.shorten(item.summary, 90, placeholder="…")
            lines.append(f"• {item.who}: {summary} ({item.assignee or 'unassigned'}, {item.overdue_by} over)")
    facts = [(name, f"{o} open, {d} overdue") for name, o, d in digest.by_assignee]
    return Message(
        kind="digest",
        title=f"Inbox summary: {digest.open_total} open, {digest.overdue_total} overdue",
        severity="info",
        lines=lines,
        facts=facts,
        links=[("Open the queue", f"{base}/"), ("Full summary", f"{base}/digest")],
    )
