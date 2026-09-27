"""An offline mailbox backed by SQLite, seeded from fixtures/demo/emails.json.

It behaves like the real providers (labels, drafts in the thread, reply
detection), so the whole pipeline and the before/after inbox screenshots run
without any account.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..db import Database
from ..models import EmailAddress, InboundEmail, ThreadMessage
from ..timeutil import iso, parse_iso, utcnow
from .base import TRIAGED, DraftRef


def _version(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:16]


class DemoMailbox:
    name = "demo"

    def __init__(self, db: Database, mailbox_address: str) -> None:
        self.db = db
        self.mailbox_address = mailbox_address

    # ------------------------------------------------------------------ seeding

    def seed(self, fixture: Path, now: datetime | None = None) -> int:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        now = now or utcnow()
        with self.db.session() as conn:
            conn.execute("DELETE FROM demo_drafts")
            conn.execute("DELETE FROM demo_messages")
            for m in data["messages"]:
                received = now - timedelta(minutes=int(m["minutes_ago"]))
                conn.execute(
                    """INSERT INTO demo_messages (id, thread_id, direction, from_name, from_email, to_email,
                         subject, body, received_at, message_id, headers, attachments)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        m["id"],
                        m["thread_id"],
                        m["direction"],
                        m.get("from_name"),
                        m["from_email"],
                        m.get("to_email") or data.get("mailbox") or self.mailbox_address,
                        m["subject"],
                        m["body"],
                        iso(received),
                        f"<{m['id']}@mail.demo>",
                        json.dumps(m.get("headers") or {}),
                        json.dumps(m.get("attachments") or []),
                    ),
                )
        return len(data["messages"])

    # ------------------------------------------------------------------ MailProvider

    def _to_email(self, row: Any, thread: list[Any]) -> InboundEmail:
        earlier = [r for r in thread if r["received_at"] < row["received_at"]][-2:]
        return InboundEmail(
            provider_id=row["id"],
            thread_id=row["thread_id"],
            sender=EmailAddress(email=row["from_email"], name=row["from_name"]),
            to=[EmailAddress(email=row["to_email"])],
            subject=row["subject"],
            body_text=row["body"],
            received_at=parse_iso(row["received_at"]),
            message_id_header=row["message_id"],
            references=[r["message_id"] for r in thread if r["received_at"] < row["received_at"]],
            attachments=json.loads(row["attachments"]),
            headers=json.loads(row["headers"]),
            provider_labels=json.loads(row["labels"]),
            thread_context=[
                ThreadMessage(
                    sender=r["from_email"],
                    sent_at=parse_iso(r["received_at"]),
                    body_text=r["body"],
                    outbound=r["direction"] == "out",
                )
                for r in earlier
            ],
        )

    def list_new(self, since: datetime, limit: int) -> list[InboundEmail]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM demo_messages WHERE direction = 'in' AND received_at >= ? ORDER BY received_at",
                (iso(since),),
            ).fetchall()
            out: list[InboundEmail] = []
            for row in rows:
                if TRIAGED in json.loads(row["labels"]):
                    continue
                thread = conn.execute(
                    "SELECT * FROM demo_messages WHERE thread_id = ? ORDER BY received_at",
                    (row["thread_id"],),
                ).fetchall()
                out.append(self._to_email(row, thread))
                if len(out) >= limit:
                    break
        return out

    def get(self, message_id: str) -> InboundEmail | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM demo_messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                return None
            thread = conn.execute(
                "SELECT * FROM demo_messages WHERE thread_id = ? ORDER BY received_at", (row["thread_id"],)
            ).fetchall()
            return self._to_email(row, thread)

    def apply_labels(self, email: InboundEmail, labels: list[str]) -> None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT labels FROM demo_messages WHERE id = ?", (email.provider_id,)
            ).fetchone()
            if row is None:
                return
            current = json.loads(row["labels"])
            merged = current + [lbl for lbl in labels if lbl not in current]
            conn.execute(
                "UPDATE demo_messages SET labels = ? WHERE id = ?", (json.dumps(merged), email.provider_id)
            )

    def create_reply_draft(self, email: InboundEmail, body_text: str) -> DraftRef:
        draft_id = f"draft-{uuid.uuid4().hex[:10]}"
        subject = email.subject if email.subject.lower().startswith("re:") else f"Re: {email.subject}"
        with self.db.session() as conn:
            conn.execute(
                """INSERT INTO demo_drafts (id, thread_id, in_reply_to, to_email, subject, body, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft_id,
                    email.thread_id,
                    email.message_id_header or "",
                    (email.reply_to or email.sender).email,
                    subject,
                    body_text,
                    iso(utcnow()),
                ),
            )
        return DraftRef(
            draft_id=draft_id,
            web_link=f"/demo/inbox?state=after&thread={email.thread_id}",
            version=_version(body_text),
        )

    def delete_draft(self, ref: DraftRef) -> bool:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT body FROM demo_drafts WHERE id = ? AND deleted_at IS NULL", (ref.draft_id,)
            ).fetchone()
            if row is None or (ref.version and _version(row["body"]) != ref.version):
                return False
            conn.execute("UPDATE demo_drafts SET deleted_at = ? WHERE id = ?", (iso(utcnow()), ref.draft_id))
        return True

    def edit_draft(self, draft_id: str, body: str) -> None:
        """Simulate a person editing a draft in their mail client (demo and tests)."""
        with self.db.session() as conn:
            conn.execute("UPDATE demo_drafts SET body = ? WHERE id = ?", (body, draft_id))

    def has_reply_after(self, thread_id: str, after: datetime) -> bool:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT 1 FROM demo_messages WHERE thread_id = ? AND direction = 'out' AND received_at > ?",
                (thread_id, iso(after)),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ demo-only helpers

    def add_outbound_reply(self, thread_id: str, body: str, at: datetime | None = None) -> None:
        """Simulate a team member sending a reply from the mailbox (used by tests and the demo)."""
        with self.db.session() as conn:
            last = conn.execute(
                "SELECT * FROM demo_messages WHERE thread_id = ? AND direction = 'in' ORDER BY received_at DESC",
                (thread_id,),
            ).fetchone()
            if last is None:
                return
            conn.execute(
                """INSERT INTO demo_messages (id, thread_id, direction, from_name, from_email, to_email, subject,
                     body, received_at, message_id)
                   VALUES (?, ?, 'out', 'Support', ?, ?, ?, ?, ?, ?)""",
                (
                    f"demo-out-{uuid.uuid4().hex[:8]}",
                    thread_id,
                    self.mailbox_address,
                    last["from_email"],
                    f"Re: {last['subject']}",
                    body,
                    iso(at or utcnow()),
                    f"<{uuid.uuid4().hex}@mail.demo>",
                ),
            )

    def threads(self) -> list[dict[str, Any]]:
        """Inbox view: one row per thread, newest first, with labels and draft state."""
        with self.db.session() as conn:
            messages = conn.execute("SELECT * FROM demo_messages ORDER BY received_at").fetchall()
            drafts = conn.execute("SELECT * FROM demo_drafts WHERE deleted_at IS NULL").fetchall()
        threads: dict[str, dict[str, Any]] = {}
        for m in messages:
            t = threads.setdefault(
                m["thread_id"],
                {"thread_id": m["thread_id"], "messages": [], "labels": [], "has_draft": False, "drafts": []},
            )
            t["messages"].append(dict(m))
            for lbl in json.loads(m["labels"]):
                if lbl not in t["labels"]:
                    t["labels"].append(lbl)
        for d in drafts:
            if d["thread_id"] in threads:
                threads[d["thread_id"]]["has_draft"] = True
                threads[d["thread_id"]]["drafts"].append(dict(d))
        for t in threads.values():
            inbound = [m for m in t["messages"] if m["direction"] == "in"]
            last = t["messages"][-1]
            first_in = inbound[0] if inbound else last
            t["subject"] = first_in["subject"].removeprefix("Re: ")
            t["from_name"] = first_in["from_name"] or first_in["from_email"]
            t["snippet"] = " ".join(last["body"].split())[:140]
            t["last_at"] = last["received_at"]
            t["count"] = len(t["messages"])
            t["replied"] = any(m["direction"] == "out" for m in t["messages"])
        return sorted(threads.values(), key=lambda t: t["last_at"], reverse=True)
