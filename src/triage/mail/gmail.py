"""Gmail / Google Workspace mailbox via the Gmail API.

Access: one OAuth token for the shared mailbox (`triage gmail-auth`), or a
Workspace service account with domain-wide delegation impersonating it.

Scope: gmail.modify (read, label, create and delete drafts). Google has no
narrower scope that allows drafts without also allowing sending, so the
no-send guarantee is in this code: it only calls messages.list/get/modify,
threads.get, labels.list/create and drafts.create/get/delete.
tests/test_no_send.py enforces that.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..models import InboundEmail, ThreadMessage
from .base import TRIAGED, DraftRef, MailboxNotConnected
from .parsing import (
    KEPT_HEADERS,
    format_address,
    html_to_text,
    is_team_address,
    parse_address,
    parse_addresses,
    reply_subject,
)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Label colours must come from Gmail's fixed palette.
LABEL_COLORS = {
    "AI/Urgency/Critical": {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"},
    "AI/Urgency/High": {"backgroundColor": "#ffad47", "textColor": "#ffffff"},
    "AI/Urgency/Normal": {"backgroundColor": "#4a86e8", "textColor": "#ffffff"},
    "AI/Urgency/Low": {"backgroundColor": "#16a766", "textColor": "#ffffff"},
    "AI/Draft ready": {"backgroundColor": "#16a766", "textColor": "#ffffff"},
    "AI/Needs human": {"backgroundColor": "#a479e2", "textColor": "#ffffff"},
    "AI/Excluded": {"backgroundColor": "#000000", "textColor": "#ffffff"},
}


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def extract_body(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """(plain-text body, attachment names) from a Gmail message payload."""
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        filename = part.get("filename")
        body = part.get("body") or {}
        if filename:
            attachments.append(filename)
        elif mime == "text/plain" and body.get("data"):
            plain.append(_b64decode(body["data"]).decode("utf-8", errors="replace"))
        elif mime == "text/html" and body.get("data"):
            html_parts.append(_b64decode(body["data"]).decode("utf-8", errors="replace"))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain).strip() or html_to_text("\n".join(html_parts))
    return text, attachments


def search_label(name: str) -> str:
    """Gmail search syntax for a label name: 'AI/Triaged' -> 'ai-triaged'."""
    return name.lower().replace("/", "-").replace(" ", "-")


class GmailMailbox:
    name = "gmail"

    def __init__(
        self, service: Any, mailbox_address: str, team_domains: list[str], user_id: str = "me"
    ) -> None:
        self.service = service
        self.mailbox_address = mailbox_address
        self.team_domains = team_domains
        self.user_id = user_id
        self._labels: dict[str, str] | None = None  # name -> id

    @classmethod
    def from_settings(cls, settings: Settings) -> GmailMailbox:
        """Service account or token file (the command-line setup). The dashboard's Connect Gmail
        button stores its token in the database instead; see connections.py."""
        return cls.from_credentials(load_credentials(settings), settings, settings.mailbox_address)

    @classmethod
    def from_credentials(cls, credentials: Any, settings: Settings, address: str) -> GmailMailbox:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(service, address, settings.team_domain_list)

    def _users(self) -> Any:
        return self.service.users()

    # ------------------------------------------------------------------ labels

    def _label_map(self) -> dict[str, str]:
        if self._labels is None:
            response = self._users().labels().list(userId=self.user_id).execute()
            self._labels = {lbl["name"]: lbl["id"] for lbl in response.get("labels", [])}
        return self._labels

    def _ensure_label(self, name: str) -> str:
        labels = self._label_map()
        if name in labels:
            return labels[name]
        # Create parents first so Gmail shows the labels nested under "AI".
        parts = name.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent not in labels:
                self._create_label(parent)
        return self._create_label(name)

    def _create_label(self, name: str) -> str:
        from googleapiclient.errors import HttpError

        body: dict[str, Any] = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        if name in LABEL_COLORS:
            body["color"] = LABEL_COLORS[name]
        try:
            created = self._users().labels().create(userId=self.user_id, body=body).execute()
        except HttpError:
            if "color" not in body:
                raise
            body.pop("color")
            created = self._users().labels().create(userId=self.user_id, body=body).execute()
        self._label_map()[name] = created["id"]
        return str(created["id"])

    # ------------------------------------------------------------------ reading

    def list_new(self, since: datetime, limit: int) -> list[InboundEmail]:
        query = f"in:inbox after:{int(since.timestamp())} -label:{search_label(TRIAGED)}"
        ids: list[str] = []
        token: str | None = None
        while len(ids) < limit:
            response = (
                self._users()
                .messages()
                .list(userId=self.user_id, q=query, maxResults=min(100, limit), pageToken=token)
                .execute()
            )
            ids.extend(m["id"] for m in response.get("messages", []))
            token = response.get("nextPageToken")
            if not token:
                break
        emails = [e for e in (self.get(message_id) for message_id in ids[:limit]) if e is not None]
        return sorted((e for e in emails if TRIAGED not in e.provider_labels), key=lambda e: e.received_at)

    def get(self, message_id: str) -> InboundEmail | None:
        from googleapiclient.errors import HttpError

        try:
            message = (
                self._users().messages().get(userId=self.user_id, id=message_id, format="full").execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise
        return self._to_email(message, with_thread=True)

    def _to_email(self, message: dict[str, Any], *, with_thread: bool) -> InboundEmail:
        payload = message.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        body, attachments = extract_body(payload)
        received = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)
        id_to_name = {v: k for k, v in self._label_map().items()}
        email = InboundEmail(
            provider_id=message["id"],
            thread_id=message["threadId"],
            sender=parse_address(headers.get("from", "")),
            reply_to=parse_address(headers["reply-to"]) if headers.get("reply-to") else None,
            to=parse_addresses(headers.get("to", "")),
            cc=parse_addresses(headers.get("cc", "")),
            subject=headers.get("subject", ""),
            body_text=body,
            received_at=received,
            message_id_header=headers.get("message-id"),
            references=headers.get("references", "").split(),
            attachments=attachments,
            headers={k: v for k, v in headers.items() if k in KEPT_HEADERS},
            provider_labels=[id_to_name.get(i, i) for i in message.get("labelIds", [])],
        )
        if with_thread:
            email.thread_context = self._thread_context(message["threadId"], received)
        return email

    def _thread_context(self, thread_id: str, before: datetime) -> list[ThreadMessage]:
        thread = self._users().threads().get(userId=self.user_id, id=thread_id, format="full").execute()
        earlier: list[ThreadMessage] = []
        for m in thread.get("messages", []):
            if "DRAFT" in m.get("labelIds", []):
                continue
            sent = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=UTC)
            if sent >= before:
                continue
            headers = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
            sender = parse_address(headers.get("from", "")).email
            earlier.append(
                ThreadMessage(
                    sender=sender,
                    sent_at=sent,
                    body_text=extract_body(m.get("payload") or {})[0],
                    outbound="SENT" in m.get("labelIds", [])
                    or is_team_address(sender, self.mailbox_address, self.team_domains),
                )
            )
        return earlier[-2:]

    # ------------------------------------------------------------------ writing (labels and drafts only)

    def apply_labels(self, email: InboundEmail, labels: list[str]) -> None:
        ids = [self._ensure_label(name) for name in labels]
        self._users().messages().modify(
            userId=self.user_id, id=email.provider_id, body={"addLabelIds": ids}
        ).execute()

    def create_reply_draft(self, email: InboundEmail, body_text: str) -> DraftRef:
        mime = EmailMessage()
        mime["From"] = self.mailbox_address
        mime["To"] = format_address(email.reply_to or email.sender)
        mime["Subject"] = reply_subject(email.subject)
        if email.message_id_header:
            mime["In-Reply-To"] = email.message_id_header
            mime["References"] = " ".join([*email.references, email.message_id_header])
        mime.set_content(body_text)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = (
            self._users()
            .drafts()
            .create(userId=self.user_id, body={"message": {"raw": raw, "threadId": email.thread_id}})
            .execute()
        )
        message_id = draft["message"]["id"]
        link = f"https://mail.google.com/mail/?authuser={quote(self.mailbox_address)}#drafts?compose={message_id}"
        return DraftRef(draft_id=draft["id"], web_link=link, version=message_id)

    def delete_draft(self, ref: DraftRef) -> bool:
        from googleapiclient.errors import HttpError

        try:
            current = (
                self._users().drafts().get(userId=self.user_id, id=ref.draft_id, format="minimal").execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:  # sent or deleted already
                return False
            raise
        # Gmail gives an edited draft a new message id; leave edited drafts alone.
        if ref.version and current.get("message", {}).get("id") != ref.version:
            return False
        self._users().drafts().delete(userId=self.user_id, id=ref.draft_id).execute()
        return True

    def has_reply_after(self, thread_id: str, after: datetime) -> bool:
        thread = (
            self._users()
            .threads()
            .get(userId=self.user_id, id=thread_id, format="metadata", metadataHeaders=["From"])
            .execute()
        )
        for m in thread.get("messages", []):
            label_ids = m.get("labelIds", [])
            if "DRAFT" in label_ids:
                continue
            if int(m["internalDate"]) / 1000 <= after.timestamp():
                continue
            headers = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
            sender = parse_address(headers.get("from", "")).email
            if "SENT" in label_ids or is_team_address(sender, self.mailbox_address, self.team_domains):
                return True
        return False


# ------------------------------------------------------------------ credentials


def load_credentials(settings: Settings) -> Any:
    if settings.gmail_service_account_file:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(settings.gmail_service_account_file),
            scopes=SCOPES,
            subject=settings.gmail_delegated_user or settings.mailbox_address,
        )
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = settings.gmail_token_file
    if not token_file.exists():
        raise MailboxNotConnected(
            "Gmail isn't connected yet. Open Settings in the dashboard and press Connect Gmail "
            "(see docs/SETUP-GMAIL.md)."
        )
    credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        try:
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        except OSError as exc:
            # Read-only mounts (e.g. Render Secret Files) are fine: the refresh token in the file keeps
            # working and the Google client refreshes the access token in memory as needed.
            log.info("Could not save the refreshed Gmail token to %s: %s", token_file, exc)
    return credentials


def run_oauth_flow(client_secrets: Path, token_file: Path) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    credentials = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
