"""Alerts and digests: one channel-neutral message, formatted for Slack or Teams, always kept in an outbox."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx

from ..config import Settings
from ..db import Database
from ..models import TeamMember
from ..store import insert_notification
from ..timeutil import utcnow
from .slack import slack_payload
from .teams import teams_payload

log = logging.getLogger(__name__)


@dataclass
class Message:
    kind: Literal["alert", "digest"]
    title: str
    severity: Literal["critical", "high", "normal", "info"] = "info"
    lines: list[str] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    mention: TeamMember | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mention"] = self.mention.model_dump() if self.mention else None
        return data


class Notifier:
    def __init__(self, settings: Settings, db: Database, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.db = db
        self.channel = settings.notify
        self.http = http or httpx.Client(timeout=10)

    def payload(self, message: Message) -> dict[str, Any]:
        if self.channel == "teams":
            return teams_payload(message)
        return slack_payload(message)

    def send(self, message: Message, query_id: int | None = None) -> bool:
        payload = self.payload(message)
        delivered, error = False, None
        url = {"slack": self.settings.slack_webhook_url, "teams": self.settings.teams_webhook_url}.get(
            self.channel
        )
        if self.channel != "log":
            if not url:
                error = f"{self.channel.upper()}_WEBHOOK_URL is not set"
            else:
                try:
                    response = self.http.post(url, json=payload)
                    response.raise_for_status()
                    delivered = True
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    log.warning("Could not post %s notification: %s", self.channel, error)
        with self.db.session() as conn:
            insert_notification(
                conn,
                {
                    "channel": self.channel,
                    "kind": message.kind,
                    "query_id": query_id,
                    "message": message.to_dict(),
                    "payload": payload,
                    "delivered": delivered,
                    "error": error,
                    "created_at": utcnow(),
                },
            )
        return delivered
