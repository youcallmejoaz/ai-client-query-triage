from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from triage.ai.mock import MockAssistant
from triage.config import DEMO_DIR, Settings
from triage.db import Database
from triage.mail.demo import DemoMailbox
from triage.pipeline import Services
from triage.services import build_services

NOW = datetime(2026, 9, 24, 9, 30, tzinfo=UTC)  # a Thursday morning


@dataclass
class Env:
    settings: Settings
    db: Database
    mailbox: DemoMailbox
    assistant: MockAssistant
    svc: Services


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": tmp_path / "test.db",
        "mail_provider": "demo",
        "ai_provider": "mock",
        "notify": "log",
        "public_url": "https://triage.test",
        "timezone": "Europe/London",
        "secret_key": "test-secret",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def env(settings: Settings) -> Iterator[Env]:
    db = Database(settings.database_path)
    mailbox = DemoMailbox(db, settings.mailbox_address)
    mailbox.seed(DEMO_DIR / "emails.json", now=NOW)
    assistant = MockAssistant(DEMO_DIR / "scripted_responses.json")
    svc = build_services(settings, db, assistant=assistant, mailbox=mailbox)
    svc.clock = lambda: NOW
    yield Env(settings=settings, db=db, mailbox=mailbox, assistant=assistant, svc=svc)
