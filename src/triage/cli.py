"""Command line: `triage serve | poll | sync | digest | kb-index | seed-demo | gmail-auth | check`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import get_settings


def _services(with_mailbox: bool = True):  # type: ignore[no-untyped-def]
    from .services import build_services

    return build_services(get_settings(), with_mailbox=with_mailbox)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("triage.web.app:create_app", factory=True, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    from .pipeline import process_inbox

    report = process_inbox(_services())
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .pipeline import sync_replies

    print(json.dumps({"closed_by_reply": sync_replies(_services())}))
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from .digest import build_digest, digest_message

    svc = _services(with_mailbox=False)
    digest = build_digest(svc.db, svc.clock())
    if args.send:
        svc.notifier.send(digest_message(digest, svc.settings))
    print(json.dumps(digest.as_dict(), indent=2))
    return 0


def cmd_kb_index(args: argparse.Namespace) -> int:
    from .context.knowledge import LocalKB
    from .db import Database

    settings = get_settings()
    count = LocalKB(Database(settings.database_path), settings.kb_dir).index()
    print(f"Indexed {count} articles from {settings.kb_dir}")
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    from .context.knowledge import LocalKB
    from .db import Database
    from .mail.demo import DemoMailbox

    settings = get_settings()
    db = Database(settings.database_path)
    db.reset(demo=True)
    count = DemoMailbox(db, settings.mailbox_address).seed(settings.demo_emails_file)
    articles = LocalKB(db, settings.kb_dir).index()
    print(f"Demo mailbox seeded with {count} messages; {articles} knowledge-base articles indexed.")
    print("Run `triage poll` (or press 'Check inbox now' in the dashboard) to triage them.")
    return 0


def cmd_gmail_auth(args: argparse.Namespace) -> int:
    from .mail.gmail import run_oauth_flow

    settings = get_settings()
    run_oauth_flow(settings.gmail_client_secrets_file, settings.gmail_token_file)
    print(f"Saved Gmail token to {settings.gmail_token_file}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate configuration files without touching the mailbox or the model."""
    from .context.clients import ClientDirectory
    from .privacy import PrivacyPolicy
    from .routing import Router

    settings = get_settings()
    router = Router.from_yaml(settings.routing_file)
    clients = ClientDirectory.from_csv(settings.clients_file)
    privacy = PrivacyPolicy.from_yaml(settings.privacy_file)
    unknown_owners = sorted(
        {c.account_owner for c in clients.clients if c.account_owner and c.account_owner not in router.team}
    )
    print(f"Mailbox: {settings.mail_provider} ({settings.mailbox_address}); AI: {settings.ai_provider}")
    print(f"Routing: {len(router.rules)} rules, {len(router.team)} team members")
    print(f"Clients: {len(clients.clients)} ({sum(c.ai_excluded for c in clients.clients)} excluded from AI)")
    print(
        f"Privacy: {len(privacy.excluded_domains)} excluded domains, {len(privacy.subject_markers)} markers"
    )
    if unknown_owners:
        print(f"Warning: account owners not in the team list: {', '.join(unknown_owners)}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="triage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the dashboard and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("poll", help="Triage new mail once").set_defaults(func=cmd_poll)
    sub.add_parser("sync", help="Close queries that were answered in the mailbox").set_defaults(func=cmd_sync)
    digest = sub.add_parser("digest", help="Print the daily summary (--send posts it)")
    digest.add_argument("--send", action="store_true")
    digest.set_defaults(func=cmd_digest)
    sub.add_parser("kb-index", help="Rebuild the knowledge-base index").set_defaults(func=cmd_kb_index)
    sub.add_parser("seed-demo", help="Reset the database and load the demo mailbox").set_defaults(
        func=cmd_seed_demo
    )
    sub.add_parser("gmail-auth", help="Authorize Gmail access (OAuth, opens a browser)").set_defaults(
        func=cmd_gmail_auth
    )
    sub.add_parser("check", help="Validate the configuration files").set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
