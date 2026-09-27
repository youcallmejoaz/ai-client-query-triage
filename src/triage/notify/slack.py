"""Slack incoming-webhook payloads (Block Kit)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import Message

EMOJI = {"critical": ":rotating_light:", "high": ":warning:", "normal": ":envelope:", "info": ":bar_chart:"}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(message: Message) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": message.title[:150], "emoji": True}}
    ]
    text_lines = [_escape(line) for line in message.lines]
    if message.mention:
        who = f"<@{message.mention.slack_id}>" if message.mention.slack_id else _escape(message.mention.name)
        text_lines.insert(0, f"Assigned to {who}")
    if text_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(text_lines)[:3000]}})
    if message.facts:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{_escape(k)}*\n{_escape(v)}"} for k, v in message.facts[:10]
                ],
            }
        )
    if message.links:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": label[:75]}, "url": url}
                    for label, url in message.links[:5]
                ],
            }
        )
    fallback = f"{EMOJI.get(message.severity, '')} {message.title}".strip()
    return {"text": fallback, "blocks": blocks}
