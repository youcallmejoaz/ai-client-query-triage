"""Builds the pipeline's services from settings."""

from __future__ import annotations

import logging

from .ai.base import Assistant
from .config import Settings
from .connections import TokenUnreadable, ensure_secret_key, open_mailbox
from .context.clients import ClientDirectory
from .context.knowledge import HttpKB, KnowledgeSource, LocalKB
from .db import Database
from .mail.base import MailboxNotConnected, MailProvider
from .notify.base import Notifier
from .pipeline import Services
from .privacy import PrivacyPolicy
from .routing import Router

log = logging.getLogger(__name__)


def build_assistant(settings: Settings) -> Assistant:
    if settings.ai_provider == "gemini":
        from .ai.gemini import GeminiAssistant

        return GeminiAssistant(settings)
    from .ai.mock import MockAssistant

    return MockAssistant(settings.demo_responses_file)


def build_mailbox(settings: Settings, db: Database) -> tuple[MailProvider | None, str | None]:
    """The mailbox, or (None, reason) when it isn't connected: the app still starts."""
    try:
        return open_mailbox(settings, db), None
    except MailboxNotConnected as exc:
        return None, str(exc)
    except TokenUnreadable as exc:
        return None, str(exc)
    except Exception as exc:
        log.exception("Could not open the %s mailbox", settings.mail_provider)
        return None, f"Could not open the {settings.mail_provider} mailbox: {exc}"


def build_knowledge(settings: Settings, db: Database) -> KnowledgeSource:
    if settings.kb_url:
        return HttpKB(settings.kb_url, settings.kb_api_key)
    kb = LocalKB(db, settings.kb_dir)
    kb.ensure_indexed()
    return kb


def build_services(
    settings: Settings,
    db: Database | None = None,
    *,
    assistant: Assistant | None = None,
    mailbox: MailProvider | None = None,
    with_mailbox: bool = True,
) -> Services:
    db = db or Database(settings.database_path)
    settings = ensure_secret_key(settings, db)
    mail_error: str | None = None
    if mailbox is None and with_mailbox:
        mailbox, mail_error = build_mailbox(settings, db)
    return Services(
        settings=settings,
        db=db,
        mail=mailbox,
        mail_error=mail_error,
        assistant=assistant or build_assistant(settings),
        clients=ClientDirectory.from_csv(settings.clients_file),
        kb=build_knowledge(settings, db),
        privacy=PrivacyPolicy.from_yaml(settings.privacy_file),
        router=Router.from_yaml(settings.routing_file),
        notifier=Notifier(settings, db),
    )
