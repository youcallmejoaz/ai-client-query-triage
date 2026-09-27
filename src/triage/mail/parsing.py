"""Helpers shared by the Gmail and Graph providers."""

from __future__ import annotations

import html
import re
from email.utils import formataddr, getaddresses

from ..models import EmailAddress

KEPT_HEADERS = {"auto-submitted", "list-unsubscribe", "precedence", "x-autoreply", "x-autorespond"}


def parse_addresses(value: str) -> list[EmailAddress]:
    return [
        EmailAddress(email=addr.lower(), name=name or None) for name, addr in getaddresses([value]) if addr
    ]


def parse_address(value: str) -> EmailAddress:
    found = parse_addresses(value)
    return found[0] if found else EmailAddress(email="unknown@invalid")


def format_address(addr: EmailAddress) -> str:
    return formataddr((addr.name or "", addr.email))


def reply_subject(subject: str) -> str:
    return subject if re.match(r"^\s*re\s*:", subject, re.IGNORECASE) else f"Re: {subject}"


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|head).*?</\1>", "", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\xa0]+", " ", value)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", value).strip()


def text_to_html(value: str) -> str:
    paragraphs = [html.escape(p).replace("\n", "<br>") for p in value.split("\n\n")]
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def is_team_address(address: str, mailbox: str, team_domains: list[str]) -> bool:
    address = address.lower()
    return address == mailbox.lower() or address.rpartition("@")[2] in team_domains
