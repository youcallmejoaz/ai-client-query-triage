#!/usr/bin/env python3
"""Record the README screenshots in docs/media by driving the running app in headless Chromium.

    triage serve &                               # MAIL_PROVIDER=demo; AI_PROVIDER=mock or gemini
    python scripts/capture_media.py              # APP_URL defaults to http://localhost:8000

It resets the demo mailbox, captures the inbox before triage, presses "Check inbox now", then
captures the queue, a draft, the mailbox after triage, the daily summary, the alerts, a phone
view and dark mode. Set BASIC_AUTH_USER / BASIC_AUTH_PASSWORD if the login is on, and
CHROMIUM_PATH to use a specific browser build.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

APP_URL = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")
OUT = Path(__file__).resolve().parents[1] / "docs" / "media"
SCALE = float(os.environ.get("MEDIA_SCALE", "1.5"))


def goto(page: Page, path: str) -> None:
    page.goto(APP_URL + path)
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")


def shot(page: Page, name: str, *, full: bool = False, height: int | None = None) -> None:
    path = OUT / f"{name}.png"
    if height:
        width = page.viewport_size["width"] if page.viewport_size else 1440
        page.screenshot(path=path, clip={"x": 0, "y": 0, "width": width, "height": height}, full_page=True)
    else:
        page.screenshot(path=path, full_page=full)
    print("saved", path.relative_to(OUT.parents[1]))


def query_link(page: Page, subject: str) -> str:
    href = page.get_by_role("link", name=re.compile(re.escape(subject))).first.get_attribute("href")
    assert href, subject
    return href


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    auth = None
    if os.environ.get("BASIC_AUTH_USER"):
        auth = {
            "username": os.environ["BASIC_AUTH_USER"],
            "password": os.environ.get("BASIC_AUTH_PASSWORD", ""),
        }
    with sync_playwright() as pw:
        launch = {"executable_path": os.environ["CHROMIUM_PATH"]} if os.environ.get("CHROMIUM_PATH") else {}
        browser = pw.chromium.launch(**launch)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=SCALE, http_credentials=auth
        )
        page = ctx.new_page()

        # Start from a fresh demo inbox.
        goto(page, "/")
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Reset demo").click()
        page.wait_for_load_state("networkidle")

        goto(page, "/demo/inbox?state=before")
        shot(page, "inbox-before", height=1000)

        goto(page, "/")
        page.get_by_role("button", name="Check inbox now").click()
        page.wait_for_url(re.compile(r"flash="), timeout=600_000)
        page.wait_for_load_state("networkidle")
        shot(page, "queue", height=1180)

        detail = query_link(page, "Re: Payroll run failed")
        kestrel = query_link(page, "Third QuickBooks sync failure")
        goto(page, detail)
        shot(page, "draft-reply", full=True)

        goto(page, "/demo/inbox?state=after")
        shot(page, "inbox-after", height=1000)
        goto(page, "/demo/inbox?state=after&thread=t-acme-payroll")
        shot(page, "mailbox-draft", full=True)

        goto(page, "/digest")
        shot(page, "daily-summary", full=True)
        goto(page, "/outbox")
        shot(page, "alerts", height=1000)

        dark = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=SCALE,
            color_scheme="dark",
            http_credentials=auth,
        )
        dark_page = dark.new_page()
        goto(dark_page, kestrel)
        shot(dark_page, "dark-mode", height=1000)

        phone = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            http_credentials=auth,
        )
        phone_page = phone.new_page()
        goto(phone_page, "/")
        shot(phone_page, "mobile")
        browser.close()


if __name__ == "__main__":
    main()
