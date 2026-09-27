"""GmailMailbox against an in-memory fake of the Gmail API client."""

from __future__ import annotations

import base64
import email
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from triage.mail.base import TRIAGED, DraftRef
from triage.mail.gmail import GmailMailbox, extract_body, search_label

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(
    mid: str,
    thread: str,
    sender: str,
    subject: str,
    body: str,
    at: datetime,
    labels: list[str] | None = None,
    headers: dict[str, str] | None = None,
    html: bool = False,
    attachment: str | None = None,
) -> dict[str, Any]:
    all_headers = {
        "From": sender,
        "To": "support@tidewater.example",
        "Subject": subject,
        "Message-ID": f"<{mid}@mail>",
        **(headers or {}),
    }
    text_part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}
    parts: list[dict[str, Any]] = [{"mimeType": "multipart/alternative", "parts": [text_part]}]
    if attachment:
        parts.append({"mimeType": "application/pdf", "filename": attachment, "body": {"attachmentId": "a1"}})
    return {
        "id": mid,
        "threadId": thread,
        "internalDate": str(int(at.timestamp() * 1000)),
        "labelIds": labels or ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": k, "value": v} for k, v in all_headers.items()],
            "parts": parts,
        },
    }


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"error")


class Req:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn

    def execute(self) -> Any:
        return self.fn()


class FakeGmail:
    def __init__(self) -> None:
        self.msgs: dict[str, dict[str, Any]] = {}
        self.label_store: dict[str, str] = {"INBOX": "INBOX", "SENT": "SENT", "DRAFT": "DRAFT"}
        self.draft_store: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reject_colors = False

    def add(self, message: dict[str, Any]) -> None:
        self.msgs[message["id"]] = message

    # users() -> resources
    def users(self) -> FakeGmail:
        return self

    def labels(self) -> Any:
        fake = self

        class Labels:
            def list(self, **kw: Any) -> Req:
                return Req(lambda: {"labels": [{"name": n, "id": i} for n, i in fake.label_store.items()]})

            def create(self, userId: str, body: dict[str, Any]) -> Req:
                def run() -> dict[str, Any]:
                    fake.calls.append(("labels.create", body))
                    if fake.reject_colors and "color" in body:
                        raise http_error(400)
                    label_id = f"Label_{len(fake.label_store)}"
                    fake.label_store[body["name"]] = label_id
                    return {"id": label_id, "name": body["name"]}

                return Req(run)

        return Labels()

    def messages(self) -> Any:
        fake = self

        class Messages:
            def list(self, userId: str, q: str, maxResults: int, pageToken: str | None = None) -> Req:
                fake.calls.append(("messages.list", {"q": q}))
                ids = [{"id": m["id"]} for m in fake.msgs.values() if "INBOX" in m["labelIds"]]
                return Req(lambda: {"messages": ids})

            def get(self, userId: str, id: str, format: str) -> Req:
                def run() -> dict[str, Any]:
                    if id not in fake.msgs:
                        raise http_error(404)
                    return fake.msgs[id]

                return Req(run)

            def modify(self, userId: str, id: str, body: dict[str, Any]) -> Req:
                def run() -> dict[str, Any]:
                    fake.calls.append(("messages.modify", {"id": id, **body}))
                    fake.msgs[id]["labelIds"] += body["addLabelIds"]
                    return {}

                return Req(run)

        return Messages()

    def threads(self) -> Any:
        fake = self

        class Threads:
            def get(self, userId: str, id: str, format: str, metadataHeaders: list[str] | None = None) -> Req:
                msgs = sorted(
                    (m for m in fake.msgs.values() if m["threadId"] == id),
                    key=lambda m: int(m["internalDate"]),
                )
                return Req(lambda: {"id": id, "messages": msgs})

        return Threads()

    def drafts(self) -> Any:
        fake = self

        class Drafts:
            def create(self, userId: str, body: dict[str, Any]) -> Req:
                def run() -> dict[str, Any]:
                    n = len(fake.draft_store) + 1
                    draft = {
                        "id": f"r-{n}",
                        "message": {"id": f"dm-{n}", "threadId": body["message"]["threadId"]},
                    }
                    fake.draft_store[draft["id"]] = {**draft, "raw": body["message"]["raw"]}
                    fake.calls.append(("drafts.create", body))
                    return draft

                return Req(run)

            def get(self, userId: str, id: str, format: str) -> Req:
                def run() -> dict[str, Any]:
                    if id not in fake.draft_store:
                        raise http_error(404)
                    return fake.draft_store[id]

                return Req(run)

            def delete(self, userId: str, id: str) -> Req:
                def run() -> dict[str, Any]:
                    fake.calls.append(("drafts.delete", {"id": id}))
                    fake.draft_store.pop(id)
                    return {}

                return Req(run)

        return Drafts()


@pytest.fixture
def fake() -> FakeGmail:
    fake = FakeGmail()
    fake.add(
        gmail_message(
            "m1",
            "t1",
            "Sarah Okafor <sarah.okafor@acmelogistics.example>",
            "Payroll failed",
            "Our run failed.\n\nOn Mon, Support wrote:\n> old",
            T0,
            attachment="report.pdf",
        )
    )
    fake.add(
        gmail_message(
            "m0",
            "t1",
            "support@tidewater.example",
            "Re: earlier",
            "Earlier reply from us",
            T0 - timedelta(hours=2),
            labels=["SENT"],
        )
    )
    fake.add(
        gmail_message(
            "d1",
            "t1",
            "support@tidewater.example",
            "Re: Payroll failed",
            "draft text",
            T0 + timedelta(minutes=5),
            labels=["DRAFT"],
        )
    )
    fake.add(
        gmail_message(
            "m2",
            "t2",
            "Evergreen <finance@evergreenvet.example>",
            "Automatic reply",
            "<p>Out of office&nbsp;until Monday</p>",
            T0 - timedelta(hours=1),
            html=True,
            headers={"Auto-Submitted": "auto-replied"},
        )
    )
    return fake


def mailbox(fake: FakeGmail) -> GmailMailbox:
    return GmailMailbox(fake, "support@tidewater.example", ["tidewater.example"])


def test_list_new_parses_messages_and_thread_context(fake: FakeGmail) -> None:
    emails = mailbox(fake).list_new(T0 - timedelta(days=3), 50)
    assert [e.provider_id for e in emails] == [
        "m2",
        "m1",
    ]  # oldest first; SENT and DRAFT are not in the inbox
    m1 = emails[1]
    assert m1.sender.email == "sarah.okafor@acmelogistics.example" and m1.sender.name == "Sarah Okafor"
    assert m1.body_text.startswith("Our run failed.")
    assert m1.attachments == ["report.pdf"]
    assert m1.message_id_header == "<m1@mail>"
    assert [(c.body_text, c.outbound) for c in m1.thread_context] == [("Earlier reply from us", True)]
    m2 = emails[0]
    assert m2.body_text == "Out of office until Monday"
    assert m2.headers == {"auto-submitted": "auto-replied"}
    assert f"-label:{search_label(TRIAGED)}" in fake.calls[0][1]["q"]


def test_already_triaged_messages_are_skipped(fake: FakeGmail) -> None:
    box = mailbox(fake)
    box.apply_labels(box.get("m1"), [TRIAGED])  # type: ignore[arg-type]
    assert [e.provider_id for e in box.list_new(T0 - timedelta(days=3), 50)] == ["m2"]


def test_labels_are_created_nested_with_colours(fake: FakeGmail) -> None:
    box = mailbox(fake)
    email = box.get("m1")
    assert email
    box.apply_labels(email, ["AI/Urgency/Critical", "AI/Triaged"])
    created = [body for name, body in fake.calls if name == "labels.create"]
    assert [b["name"] for b in created] == ["AI", "AI/Urgency", "AI/Urgency/Critical", "AI/Triaged"]
    assert created[2]["color"]["backgroundColor"] == "#fb4c2f"
    modify = next(body for name, body in fake.calls if name == "messages.modify")
    assert modify["id"] == "m1" and len(modify["addLabelIds"]) == 2


def test_label_colour_rejection_falls_back_to_no_colour(fake: FakeGmail) -> None:
    fake.reject_colors = True
    box = mailbox(fake)
    box.apply_labels(box.get("m1"), ["AI/Draft ready"])  # type: ignore[arg-type]
    assert "AI/Draft ready" in fake.label_store


def test_reply_draft_is_threaded_mime(fake: FakeGmail) -> None:
    box = mailbox(fake)
    email = box.get("m1")
    assert email
    email.references = ["<m0@mail>"]
    ref = box.create_reply_draft(email, "Hi Sarah,\n\nFixed.")
    body = next(body for name, body in fake.calls if name == "drafts.create")
    assert body["message"]["threadId"] == "t1"
    mime = email_parse(body["message"]["raw"])
    assert mime["To"] == "Sarah Okafor <sarah.okafor@acmelogistics.example>"
    assert mime["From"] == "support@tidewater.example"
    assert mime["Subject"] == "Re: Payroll failed"
    assert mime["In-Reply-To"] == "<m1@mail>"
    assert mime["References"] == "<m0@mail> <m1@mail>"
    assert mime.get_payload(decode=True).decode().startswith("Hi Sarah,")
    assert ref.version == "dm-1" and ref.web_link and "compose=dm-1" in ref.web_link


def email_parse(raw: str) -> email.message.Message:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_delete_draft_only_when_unedited(fake: FakeGmail) -> None:
    box = mailbox(fake)
    email = box.get("m1")
    assert email
    ref = box.create_reply_draft(email, "one")
    fake.draft_store[ref.draft_id]["message"]["id"] = "dm-edited"  # someone edited it in Gmail
    assert box.delete_draft(ref) is False
    ref2 = box.create_reply_draft(email, "two")
    assert box.delete_draft(ref2) is True
    assert box.delete_draft(DraftRef("missing", None, "x")) is False


def test_has_reply_after_ignores_drafts(fake: FakeGmail) -> None:
    box = mailbox(fake)
    assert box.has_reply_after("t1", T0) is False  # only our own draft came later
    fake.add(
        gmail_message(
            "m3",
            "t1",
            "tom@tidewater.example",
            "Re: Payroll failed",
            "Sent from Tom",
            T0 + timedelta(minutes=30),
            labels=["INBOX"],
        )
    )
    assert box.has_reply_after("t1", T0) is True
    assert box.has_reply_after("t1", T0 + timedelta(hours=1)) is False


def test_extract_body_prefers_plain_text() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<b>HTML</b>")}},
            {"mimeType": "text/plain", "body": {"data": b64("Plain")}},
        ],
    }
    assert extract_body(payload) == ("Plain", [])
