"""Builds the pipeline's services from settings."""

from __future__ import annotations

from .ai.base import Assistant
from .config import Settings
from .context.clients import ClientDirectory
from .context.knowledge import HttpKB, KnowledgeSource, LocalKB
from .db import Database
from .mail.base import MailProvider
from .notify.base import Notifier
from .pipeline import Services
from .privacy import PrivacyPolicy
from .routing import Router


def build_assistant(settings: Settings) -> Assistant:
    if settings.ai_provider == "claude":
        from .ai.claude import ClaudeAssistant

        return ClaudeAssistant(settings)
    from .ai.mock import MockAssistant

    return MockAssistant(settings.demo_responses_file)


def build_mailbox(settings: Settings, db: Database) -> MailProvider:
    if settings.mail_provider == "gmail":
        from .mail.gmail import GmailMailbox

        return GmailMailbox.from_settings(settings)
    if settings.mail_provider == "graph":
        from .mail.graph import GraphMailbox

        return GraphMailbox.from_settings(settings)
    from .mail.demo import DemoMailbox

    return DemoMailbox(db, settings.mailbox_address)


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
    return Services(
        settings=settings,
        db=db,
        mail=mailbox or (build_mailbox(settings, db) if with_mailbox else None),
        assistant=assistant or build_assistant(settings),
        clients=ClientDirectory.from_csv(settings.clients_file),
        kb=build_knowledge(settings, db),
        privacy=PrivacyPolicy.from_yaml(settings.privacy_file),
        router=Router.from_yaml(settings.routing_file),
        notifier=Notifier(settings, db),
    )
