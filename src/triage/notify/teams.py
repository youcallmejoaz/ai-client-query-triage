"""Microsoft Teams payloads: an Adaptive Card for a Teams Workflows ("When a Teams webhook request is
received") webhook. The retired Office 365 connector webhooks are not used."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import Message

COLOR = {"critical": "Attention", "high": "Warning", "normal": "Default", "info": "Accent"}


def teams_payload(message: Message) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": message.title,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "color": COLOR.get(message.severity, "Default"),
        }
    ]
    entities: list[dict[str, Any]] = []
    if message.mention:
        if message.mention.teams_upn:
            body.append(
                {"type": "TextBlock", "text": f"Assigned to <at>{message.mention.name}</at>", "wrap": True}
            )
            entities.append(
                {
                    "type": "mention",
                    "text": f"<at>{message.mention.name}</at>",
                    "mentioned": {"id": message.mention.teams_upn, "name": message.mention.name},
                }
            )
        else:
            body.append({"type": "TextBlock", "text": f"Assigned to {message.mention.name}", "wrap": True})
    for line in message.lines:
        body.append({"type": "TextBlock", "text": line, "wrap": True, "spacing": "Small"})
    if message.facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in message.facts]})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": [
            {"type": "Action.OpenUrl", "title": label, "url": url} for label, url in message.links[:5]
        ],
    }
    if entities:
        card["msteams"] = {"entities": entities}
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }
