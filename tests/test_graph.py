"""GraphMailbox against a fake Microsoft Graph (httpx.MockTransport)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from triage.mail.base import TRIAGED, DraftRef
from triage.mail.graph import GRAPH_URL, GraphMailbox

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
MAILBOX = "support@tidewater.example"


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def graph_message(mid: str, conv: str, sender: str, at: datetime, **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "conversationId": conv,
        "internetMessageId": f"<{mid}@mail>",
        "subject": extra.pop("subject", "Payroll failed"),
        "from": {"emailAddress": {"address": sender, "name": extra.pop("name", None)}},
        "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
        "ccRecipients": [],
        "receivedDateTime": z(at),
        "sentDateTime": z(at),
        "body": {"contentType": "text", "content": extra.pop("body", "Our run failed.")},
        "hasAttachments": False,
        "categories": [],
        "isDraft": False,
        "internetMessageHeaders": [{"name": "Auto-Submitted", "value": "no"}],
        **extra,
    }


class FakeGraph:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.categories: list[str] = []
        self.requests: list[httpx.Request] = []
        self.drafts = 0

    def add(self, message: dict[str, Any]) -> None:
        self.messages[message["id"]] = message

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer token-1"
        path = request.url.path.removeprefix("/v1.0")
        assert path.startswith(f"/users/{MAILBOX}")
        path = path.removeprefix(f"/users/{MAILBOX}")
        params = request.url.params
        method = request.method

        if method == "GET" and path == "/mailFolders/inbox/messages":
            assert request.headers["Prefer"] == 'outlook.body-content-type="text"'
            items = sorted(self.messages.values(), key=lambda m: m["receivedDateTime"])
            if params.get("page") == "2":
                return httpx.Response(200, json={"value": items[2:]})
            return httpx.Response(
                200,
                json={
                    "value": items[:2],
                    "@odata.nextLink": f"{GRAPH_URL}/users/{MAILBOX}/mailFolders/inbox/messages?page=2",
                },
            )
        if method == "GET" and path == "/messages" and "conversationId" in params.get("$filter", ""):
            conv = params["$filter"].split("'")[1]
            return httpx.Response(
                200, json={"value": [m for m in self.messages.values() if m["conversationId"] == conv]}
            )
        if method == "GET" and path.endswith("/attachments"):
            return httpx.Response(200, json={"value": [{"name": "report.pdf"}]})
        if method == "GET" and path == "/outlook/masterCategories":
            return httpx.Response(200, json={"value": [{"displayName": c} for c in self.categories]})
        if method == "POST" and path == "/outlook/masterCategories":
            self.categories.append(json.loads(request.content)["displayName"])
            return httpx.Response(201, json={})
        if method == "POST" and path.endswith("/createReply"):
            self.drafts += 1
            source = self.messages[path.split("/")[2]]
            draft = {
                **source,
                "id": f"draft-{self.drafts}",
                "isDraft": True,
                "changeKey": "ck-1",
                "body": {"contentType": "html", "content": "<div>quoted original</div>"},
                "webLink": f"https://outlook.office365.com/owa/?ItemID=draft-{self.drafts}",
            }
            self.add(draft)
            return httpx.Response(201, json=draft)
        if path.startswith("/messages/"):
            mid = path.split("/")[2]
            if mid not in self.messages:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            message = self.messages[mid]
            if method == "GET":
                return httpx.Response(200, json=message)
            if method == "PATCH":
                message.update(json.loads(request.content))
                message["changeKey"] = "ck-2"
                return httpx.Response(200, json=message)
            if method == "DELETE":
                del self.messages[mid]
                return httpx.Response(204)
        raise AssertionError(f"Unexpected request {method} {request.url}")


@pytest.fixture
def graph() -> FakeGraph:
    graph = FakeGraph()
    graph.add(graph_message("m0", "c1", MAILBOX, T0 - timedelta(hours=2), body="Earlier reply from us"))
    graph.add(
        graph_message(
            "m1", "c1", "sarah.okafor@acmelogistics.example", T0, name="Sarah Okafor", hasAttachments=True
        )
    )
    graph.add(
        graph_message(
            "m2", "c2", "finance@evergreenvet.example", T0 + timedelta(minutes=5), categories=[TRIAGED]
        )
    )
    graph.add(
        graph_message(
            "m3",
            "c3",
            "raj.patel@bluefindental.example",
            T0 + timedelta(minutes=10),
            body="<p>Charged&nbsp;twice</p>",
            subject="Charged twice",
        )
    )
    graph.messages["m3"]["body"]["contentType"] = "html"
    return graph


def mailbox(graph: FakeGraph) -> GraphMailbox:
    client = httpx.Client(base_url=GRAPH_URL, transport=httpx.MockTransport(graph.handler))
    return GraphMailbox(MAILBOX, lambda: "token-1", ["tidewater.example"], client=client)


def test_list_new_follows_pages_and_skips_triaged(graph: FakeGraph) -> None:
    emails = mailbox(graph).list_new(T0 - timedelta(days=1), 50)
    # m0 is ours (the pipeline skips it as outbound); m2 is already triaged.
    assert [e.provider_id for e in emails] == ["m0", "m1", "m3"]
    m1 = emails[1]
    assert m1.sender.name == "Sarah Okafor" and m1.thread_id == "c1"
    assert m1.attachments == ["report.pdf"]
    assert [(c.body_text, c.outbound) for c in m1.thread_context] == [("Earlier reply from us", True)]
    assert emails[2].body_text == "Charged twice"
    first = graph.requests[0]
    assert first.url.params["$filter"] == f"receivedDateTime ge {z(T0 - timedelta(days=1))}"


def test_categories_are_created_and_merged(graph: FakeGraph) -> None:
    box = mailbox(graph)
    graph.messages["m1"]["categories"] = ["Red category"]
    email = box.get("m1")
    assert email
    box.apply_labels(email, [TRIAGED, "AI/Urgency/Critical"])
    assert graph.categories == [TRIAGED, "AI/Urgency/Critical"]
    assert graph.messages["m1"]["categories"] == ["Red category", TRIAGED, "AI/Urgency/Critical"]


def test_reply_draft_keeps_the_quoted_original(graph: FakeGraph) -> None:
    box = mailbox(graph)
    email = box.get("m1")
    assert email
    ref = box.create_reply_draft(email, "Hi Sarah,\n\nFixed & done.")
    draft = graph.messages[ref.draft_id]
    assert draft["body"]["contentType"] == "HTML"
    assert draft["body"]["content"] == "<p>Hi Sarah,</p><p>Fixed &amp; done.</p><div>quoted original</div>"
    assert ref.version == "ck-2" and ref.web_link and "draft-1" in ref.web_link


def test_delete_draft_only_when_unedited(graph: FakeGraph) -> None:
    box = mailbox(graph)
    email = box.get("m1")
    assert email
    ref = box.create_reply_draft(email, "one")
    graph.messages[ref.draft_id]["changeKey"] = "ck-edited"
    assert box.delete_draft(ref) is False and ref.draft_id in graph.messages
    ref2 = box.create_reply_draft(email, "two")
    assert box.delete_draft(ref2) is True and ref2.draft_id not in graph.messages
    assert box.delete_draft(DraftRef("gone", None, "x")) is False
    # A draft that was sent is no longer a draft: never delete it.
    ref3 = box.create_reply_draft(email, "three")
    graph.messages[ref3.draft_id]["isDraft"] = False
    assert box.delete_draft(ref3) is False


def test_has_reply_after_counts_sent_team_messages_only(graph: FakeGraph) -> None:
    box = mailbox(graph)
    email = box.get("m1")
    assert email
    box.create_reply_draft(email, "draft only")
    assert box.has_reply_after("c1", T0) is False
    graph.add(graph_message("m4", "c1", "priya@tidewater.example", T0 + timedelta(minutes=20)))
    assert box.has_reply_after("c1", T0) is True
