"""Connecting the mailbox from the dashboard, and keeping the app running while it isn't connected.

The Connect Gmail button runs Google's web OAuth flow (authorization code + PKCE):

1. POST /oauth/google/start signs a short-lived cookie with a random state and PKCE verifier and
   redirects to Google.
2. Google redirects back to {PUBLIC_URL}/oauth/google/callback. The state is checked, the code is
   exchanged for tokens, and the token is stored in the database, encrypted with a key derived from
   SECRET_KEY.

Nothing has to run on a laptop and no token file has to be uploaded, so it works on Render.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .db import Database
from .mail.base import MailboxNotConnected, MailProvider
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CALLBACK_PATH = "/oauth/google/callback"
OAUTH_COOKIE = "triage_oauth"
OAUTH_COOKIE_MAX_AGE = 600
NOT_CONNECTED = "Gmail isn't connected yet. Open Settings and press Connect Gmail."


# ------------------------------------------------------------------ the app's own secret


def ensure_secret_key(settings: Settings, db: Database) -> Settings:
    """Use SECRET_KEY when set; otherwise generate one once and keep it in the database.

    A stable key keeps dashboard forms and the encrypted Gmail token valid across restarts.
    """
    if settings.secret_key:
        return settings
    with db.session() as conn:
        row = conn.execute("SELECT value FROM app_secrets WHERE name = 'secret_key'").fetchone()
        if row is None:
            value = secrets.token_urlsafe(48)
            conn.execute(
                "INSERT OR IGNORE INTO app_secrets (name, value, created_at) VALUES ('secret_key', ?, ?)",
                (value, iso(utcnow())),
            )
            row = conn.execute("SELECT value FROM app_secrets WHERE name = 'secret_key'").fetchone()
    return settings.model_copy(update={"secret_key": row["value"]})


def _secret(settings: Settings) -> str:
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY has not been resolved (call ensure_secret_key first)")
    return settings.secret_key


def _fernet(settings: Settings) -> Fernet:
    digest = hashlib.sha256(b"triage:mail-token:" + _secret(settings).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


# ------------------------------------------------------------------ stored mail accounts


@dataclass
class StoredAccount:
    provider: str
    account: str
    token: dict[str, Any]
    connected_at: str


class TokenUnreadable(Exception):
    """The stored token can't be decrypted (SECRET_KEY changed). Connect again."""


def save_account(
    db: Database, settings: Settings, provider: str, account: str, token: dict[str, Any], actor: str
) -> None:
    encrypted = _fernet(settings).encrypt(json.dumps(token).encode()).decode()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO mail_accounts (provider, account, token, connected_at, connected_by)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET account = excluded.account, token = excluded.token,
                 connected_at = excluded.connected_at, connected_by = excluded.connected_by""",
            (provider, account, encrypted, iso(utcnow()), actor),
        )


def load_account(db: Database, settings: Settings, provider: str) -> StoredAccount | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM mail_accounts WHERE provider = ?", (provider,)).fetchone()
    if row is None:
        return None
    try:
        token = json.loads(_fernet(settings).decrypt(row["token"].encode()))
    except InvalidToken as exc:
        raise TokenUnreadable(
            f"The saved {provider} connection for {row['account']} can't be read because SECRET_KEY changed. "
            "Connect again."
        ) from exc
    return StoredAccount(provider, row["account"], token, row["connected_at"])


def delete_account(db: Database, provider: str) -> str | None:
    """Remove the stored connection; returns the account address if there was one."""
    with db.session() as conn:
        row = conn.execute("SELECT account FROM mail_accounts WHERE provider = ?", (provider,)).fetchone()
        conn.execute("DELETE FROM mail_accounts WHERE provider = ?", (provider,))
    return row["account"] if row else None


# ------------------------------------------------------------------ Google OAuth (web flow)


def redirect_uri(settings: Settings) -> str:
    return settings.public_url + CALLBACK_PATH


def oauth_problem(settings: Settings) -> str | None:
    """Why the Connect Gmail button can't be used yet, or None."""
    missing = [
        name
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.google_oauth_client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.google_oauth_client_secret),
        )
        if not value
    ]
    if missing:
        return "Set " + " and ".join(missing) + " (a Google OAuth client of type Web application)."
    return None


def _client_config(settings: Settings) -> dict[str, Any]:
    return {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri(settings)],
        }
    }


def _flow(settings: Settings, verifier: str, state: str | None = None) -> Any:
    from google_auth_oauthlib.flow import Flow

    # Google may return previously granted scopes too; don't treat that as an error.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    if settings.public_url.startswith(("http://localhost", "http://127.0.0.1")):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # local development only
    return Flow.from_client_config(
        _client_config(settings),
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri(settings),
        state=state,
        code_verifier=verifier,
        autogenerate_code_verifier=False,
    )


def authorization_url(settings: Settings, state: str, verifier: str) -> str:
    url, _ = _flow(settings, verifier).authorization_url(
        access_type="offline",  # a refresh token, so the service keeps access
        prompt="consent",  # always return a refresh token, also when reconnecting
        include_granted_scopes="true",
        state=state,
    )
    return str(url)


def exchange_code(settings: Settings, code: str, state: str, verifier: str) -> Any:
    """Swap the authorization code for Google credentials (google.oauth2.credentials.Credentials)."""
    flow = _flow(settings, verifier, state)
    flow.fetch_token(code=code)
    return flow.credentials


def gmail_address(credentials: Any) -> str:
    """The connected account's address, from the Gmail profile (covered by the gmail.modify scope)."""
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return str(service.users().getProfile(userId="me").execute()["emailAddress"]).lower()


def missing_scopes(credentials: Any) -> list[str]:
    """Scopes we asked for that the person unticked on Google's consent screen."""
    granted = getattr(credentials, "granted_scopes", None)
    if not granted:  # Google didn't say; assume everything was granted
        return []
    granted = set(granted.split() if isinstance(granted, str) else granted)
    return [scope for scope in GMAIL_SCOPES if scope not in granted]


def credentials_from_token(token: dict[str, Any]) -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(token, GMAIL_SCOPES)


def revoke(token: dict[str, Any]) -> None:
    """Best effort: tell Google to revoke access. The local copy is deleted either way."""
    value = token.get("refresh_token") or token.get("token")
    if not value:
        return
    try:
        httpx.post("https://oauth2.googleapis.com/revoke", params={"token": value}, timeout=10)
    except httpx.HTTPError as exc:
        log.info("Could not revoke the Google token: %s", exc)


# ------------------------------------------------------------------ signed OAuth cookie


def sign_cookie(settings: Settings, data: dict[str, Any]) -> str:
    raw = json.dumps({**data, "t": int(time.time())}).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")  # no padding: cookie-safe characters only
    sig = hmac.new(_secret(settings).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_cookie(settings: Settings, value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    expected = hmac.new(_secret(settings).encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if time.time() - int(data.get("t", 0)) > OAUTH_COOKIE_MAX_AGE:
        return None
    return data


# ------------------------------------------------------------------ opening the mailbox


def open_mailbox(settings: Settings, db: Database) -> MailProvider:
    """The configured mailbox, or MailboxNotConnected with a reason the dashboard can show."""
    if settings.mail_provider == "gmail":
        from .mail.gmail import GmailMailbox

        stored = load_account(db, settings, "gmail")
        if stored is not None:
            return GmailMailbox.from_credentials(
                credentials_from_token(stored.token), settings, stored.account
            )
        if settings.gmail_service_account_file or Path(settings.gmail_token_file).exists():
            return GmailMailbox.from_settings(settings)
        raise MailboxNotConnected(NOT_CONNECTED)
    if settings.mail_provider == "graph":
        from .mail.graph import GraphMailbox

        try:
            return GraphMailbox.from_settings(settings)
        except RuntimeError as exc:
            raise MailboxNotConnected(str(exc)) from exc
    from .mail.demo import DemoMailbox

    return DemoMailbox(db, settings.mailbox_address)


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status of a Gmail API (googleapiclient) or Graph (httpx) error, if it is one."""
    resp = getattr(exc, "resp", None) or getattr(exc, "response", None)
    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    return int(status) if status else None


def is_auth_failure(exc: BaseException) -> bool:
    """True when the mailbox's credentials stopped working (revoked, expired test-mode token)."""
    from google.auth.exceptions import RefreshError

    return isinstance(exc, RefreshError | MailboxNotConnected) or _http_status(exc) == 401


def describe_mail_error(exc: BaseException) -> str:
    """A reason a person can act on, for an error while reading or writing the mailbox."""
    status = _http_status(exc)
    reason = str(getattr(exc, "reason", "") or exc).strip()
    text = f"{reason} {getattr(exc, 'error_details', '')}".lower()
    if status == 403 and (
        "has not been used" in text or "is disabled" in text or "accessnotconfigured" in text
    ):
        return (
            "The Gmail API is not enabled in the Google Cloud project of your OAuth client. Enable it "
            "(APIs & Services → Library → Gmail API → Enable), wait a minute and try again."
        )
    if status == 403 and ("insufficient" in text or "scope" in text):
        return (
            "Google didn't grant permission to manage the mailbox. Open Settings, press Connect Gmail "
            "again and tick the box to read, compose and delete Gmail messages."
        )
    if status == 429 or "ratelimitexceeded" in text or "quota" in text:
        return "Gmail's rate limit was reached. Wait a few minutes and check again."
    if status:
        return f"The mailbox returned HTTP {status}: {reason}"
    return f"{type(exc).__name__}: {reason}"


def auth_failure_message(settings: Settings) -> str:
    if settings.mail_provider == "gmail":
        return (
            "Google no longer accepts the saved Gmail access (it was revoked, or it expired because the "
            "OAuth app is still in Testing). Open Settings and press Connect Gmail again."
        )
    return "The mailbox credentials stopped working. Check the settings and restart."


# ------------------------------------------------------------------ storage


def storage_status(settings: Settings) -> tuple[str, str]:
    """('persistent' | 'ephemeral' | 'local', explanation) for where the database lives."""
    path = Path(settings.database_path).resolve()
    if os.environ.get("RENDER"):
        mount = path.parent
        while mount != mount.parent and not os.path.ismount(mount):
            mount = mount.parent
        if mount != Path("/") and os.path.ismount(mount):
            return "persistent", f"On the Render disk mounted at {mount}."
        return (
            "ephemeral",
            "Not on a persistent disk: every deploy or restart wipes the database, including the Gmail "
            "connection and triage history. Add a disk mounted at /app/data (see render.yaml).",
        )
    return "local", f"SQLite file at {path}."
