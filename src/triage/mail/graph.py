"""Microsoft 365 shared mailbox via Microsoft Graph (app-only access).

Permission: the application permission Mail.ReadWrite, limited to the shared
mailbox with RBAC for Applications (or an ApplicationAccessPolicy). The app is
not granted Mail.Send, so Microsoft 365 itself refuses any attempt to send.
This module only reads messages, sets categories, and creates, reads and
deletes its own reply drafts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Settings
from ..models import EmailAddress, InboundEmail, ThreadMessage
from ..timeutil import parse_iso
from .base import TRIAGED, DraftRef
from .parsing import KEPT_HEADERS, html_to_text, is_team_address, text_to_html

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.microsoft.com/v1.0"
SELECT = (
    "id,conversationId,internetMessageId,subject,from,replyTo,toRecipients,ccRecipients,receivedDateTime,"
    "body,hasAttachments,categories,isDraft,internetMessageHeaders"
)
PREFER_TEXT = {"Prefer": 'outlook.body-content-type="text"'}

CATEGORY_COLORS = {
    "AI/Urgency/Critical": "preset0",  # red
    "AI/Urgency/High": "preset1",  # orange
    "AI/Urgency/Normal": "preset7",  # blue
    "AI/Urgency/Low": "preset4",  # green
    "AI/Draft ready": "preset4",
    "AI/Needs human": "preset8",  # purple
    "AI/Excluded": "preset14",  # dark grey
}


def _address(value: dict[str, Any] | None) -> EmailAddress:
    addr = (value or {}).get("emailAddress") or {}
    return EmailAddress(email=(addr.get("address") or "unknown@invalid").lower(), name=addr.get("name"))


class GraphMailbox:
    name = "graph"

    def __init__(
        self,
        mailbox: str,
        token_provider: Callable[[], str],
        team_domains: list[str],
        client: httpx.Client | None = None,
    ) -> None:
        self.mailbox = mailbox
        self.mailbox_address = mailbox
        self.team_domains = team_domains
        self.token_provider = token_provider
        self.client = client or httpx.Client(base_url=GRAPH_URL, timeout=30)
        self.base = f"/users/{quote(mailbox)}"
        self._categories: set[str] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> GraphMailbox:
        import msal

        missing = [
            name
            for name, value in (
                ("GRAPH_TENANT_ID", settings.graph_tenant_id),
                ("GRAPH_CLIENT_ID", settings.graph_client_id),
                ("GRAPH_CLIENT_SECRET", settings.graph_client_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing Microsoft Graph settings: {', '.join(missing)} (see docs/SETUP-M365.md)"
            )
        app = msal.ConfidentialClientApplication(
            settings.graph_client_id,
            authority=f"https://login.microsoftonline.com/{settings.graph_tenant_id}",
            client_credential=settings.graph_client_secret,
        )

        def token() -> str:
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" not in result:
                raise RuntimeError(f"Could not get a Graph token: {result.get('error_description')}")
            return str(result["access_token"])

        return cls(settings.graph_mailbox or settings.mailbox_address, token, settings.team_domain_list)

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token_provider()}", **kwargs.pop("headers", {})}
        for attempt in range(3):
            response = self.client.request(method, url, headers=headers, **kwargs)
            if response.status_code in (429, 503) and attempt < 2:
                time.sleep(min(float(response.headers.get("Retry-After", "2")), 10.0))
                continue
            return response
        return response

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, url, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    # ------------------------------------------------------------------ reading

    def list_new(self, since: datetime, limit: int) -> list[InboundEmail]:
        params: dict[str, Any] | None = {
            "$filter": f"receivedDateTime ge {since.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$top": min(limit, 50),
            "$select": SELECT,
        }
        url: str | None = f"{self.base}/mailFolders/inbox/messages"
        out: list[InboundEmail] = []
        while url and len(out) < limit:
            page = self._json("GET", url, params=params, headers=PREFER_TEXT)
            for item in page.get("value", []):
                if item.get("isDraft") or TRIAGED in (item.get("categories") or []):
                    continue
                out.append(self._to_email(item))
                if len(out) >= limit:
                    break
            url, params = page.get("@odata.nextLink"), None  # nextLink already carries the query
        return out

    def get(self, message_id: str) -> InboundEmail | None:
        response = self._request(
            "GET", f"{self.base}/messages/{message_id}", params={"$select": SELECT}, headers=PREFER_TEXT
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._to_email(response.json())

    def _to_email(self, item: dict[str, Any]) -> InboundEmail:
        headers = {
            h["name"].lower(): h["value"]
            for h in item.get("internetMessageHeaders") or []
            if h["name"].lower() in KEPT_HEADERS | {"references"}
        }
        body = item.get("body") or {}
        text = body.get("content", "")
        if body.get("contentType", "text").lower() == "html":
            text = html_to_text(text)
        received = parse_iso(item["receivedDateTime"].replace("Z", "+00:00"))
        attachments: list[str] = []
        if item.get("hasAttachments"):
            page = self._json(
                "GET", f"{self.base}/messages/{item['id']}/attachments", params={"$select": "name"}
            )
            attachments = [a["name"] for a in page.get("value", [])]
        reply_to = item.get("replyTo") or []
        return InboundEmail(
            provider_id=item["id"],
            thread_id=item["conversationId"],
            sender=_address(item.get("from")),
            reply_to=_address(reply_to[0]) if reply_to else None,
            to=[_address(r) for r in item.get("toRecipients") or []],
            cc=[_address(r) for r in item.get("ccRecipients") or []],
            subject=item.get("subject") or "",
            body_text=text.strip(),
            received_at=received,
            message_id_header=item.get("internetMessageId"),
            references=headers.pop("references", "").split(),
            attachments=attachments,
            headers=headers,
            provider_labels=list(item.get("categories") or []),
            thread_context=self._thread_context(item["conversationId"], received),
        )

    def _conversation(self, conversation_id: str, select: str, *, text: bool = False) -> list[dict[str, Any]]:
        escaped = conversation_id.replace("'", "''")
        page = self._json(
            "GET",
            f"{self.base}/messages",
            params={"$filter": f"conversationId eq '{escaped}'", "$select": select, "$top": 50},
            headers=PREFER_TEXT if text else {},
        )
        return list(page.get("value", []))

    def _thread_context(self, conversation_id: str, before: datetime) -> list[ThreadMessage]:
        items = self._conversation(
            conversation_id, "from,receivedDateTime,sentDateTime,body,isDraft", text=True
        )
        earlier: list[ThreadMessage] = []
        for m in sorted(items, key=lambda m: m.get("receivedDateTime") or ""):
            if m.get("isDraft"):
                continue
            at = parse_iso((m.get("sentDateTime") or m["receivedDateTime"]).replace("Z", "+00:00"))
            if at >= before:
                continue
            sender = _address(m.get("from")).email
            earlier.append(
                ThreadMessage(
                    sender=sender,
                    sent_at=at,
                    body_text=((m.get("body") or {}).get("content") or "").strip(),
                    outbound=is_team_address(sender, self.mailbox, self.team_domains),
                )
            )
        return earlier[-2:]

    # ------------------------------------------------------------------ writing (categories and drafts only)

    def _ensure_categories(self, names: list[str]) -> None:
        if self._categories is None:
            page = self._json("GET", f"{self.base}/outlook/masterCategories")
            self._categories = {c["displayName"] for c in page.get("value", [])}
        for name in names:
            if name not in self._categories:
                body = {"displayName": name, "color": CATEGORY_COLORS.get(name, "preset12")}
                response = self._request("POST", f"{self.base}/outlook/masterCategories", json=body)
                if response.status_code not in (201, 409):  # 409: created meanwhile
                    response.raise_for_status()
                self._categories.add(name)

    def apply_labels(self, email: InboundEmail, labels: list[str]) -> None:
        self._ensure_categories(labels)
        # Re-read current categories so ones a person added since we fetched the message are kept.
        current = self._json(
            "GET", f"{self.base}/messages/{email.provider_id}", params={"$select": "categories"}
        )
        existing = list(current.get("categories") or [])
        merged = existing + [name for name in labels if name not in existing]
        self._json("PATCH", f"{self.base}/messages/{email.provider_id}", json={"categories": merged})

    def create_reply_draft(self, email: InboundEmail, body_text: str) -> DraftRef:
        # createReply makes a draft in the conversation, addressed to the sender, with the original quoted.
        draft = self._json("POST", f"{self.base}/messages/{email.provider_id}/createReply", json={})
        quoted = (draft.get("body") or {}).get("content", "")
        updated = self._json(
            "PATCH",
            f"{self.base}/messages/{draft['id']}",
            json={"body": {"contentType": "HTML", "content": text_to_html(body_text) + quoted}},
        )
        return DraftRef(
            draft_id=draft["id"],
            web_link=updated.get("webLink") or draft.get("webLink"),
            version=updated.get("changeKey"),
        )

    def delete_draft(self, ref: DraftRef) -> bool:
        response = self._request(
            "GET", f"{self.base}/messages/{ref.draft_id}", params={"$select": "isDraft,changeKey"}
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        current = response.json()
        # Only unedited drafts: a sent message is no longer a draft, and an edit changes the changeKey.
        if not current.get("isDraft") or (ref.version and current.get("changeKey") != ref.version):
            return False
        deleted = self._request("DELETE", f"{self.base}/messages/{ref.draft_id}")
        if deleted.status_code not in (204, 404):
            deleted.raise_for_status()
        return deleted.status_code == 204

    def has_reply_after(self, thread_id: str, after: datetime) -> bool:
        for m in self._conversation(thread_id, "from,sentDateTime,isDraft"):
            if m.get("isDraft") or not m.get("sentDateTime"):
                continue
            sender = _address(m.get("from")).email
            sent = parse_iso(m["sentDateTime"].replace("Z", "+00:00"))
            if sent > after and is_team_address(sender, self.mailbox, self.team_domains):
                return True
        return False
