"""Connect Gmail from the dashboard, and running cleanly while the mailbox isn't connected."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient

from triage import connections
from triage.ai.mock import MockAssistant
from triage.config import DEMO_DIR, Settings
from triage.db import Database
from triage.mail.gmail import GmailMailbox
from triage.pipeline import Services
from triage.services import build_services
from triage.web.app import create_app, csrf_token


class FakeCredentials:
    refresh_token = "refresh-123"

    def to_json(self) -> str:
        return json.dumps({"token": "access-abc", "refresh_token": self.refresh_token, "client_id": "cid"})


class FakeGmail:
    name = "gmail"

    def __init__(self, address: str) -> None:
        self.mailbox_address = address

    def list_new(self, since: Any, limit: int) -> list[Any]:
        return []

    def has_reply_after(self, thread_id: str, after: Any) -> bool:
        return False


def gmail_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mail_provider": "gmail",
        "google_oauth_client_id": "cid.apps.googleusercontent.com",
        "google_oauth_client_secret": "csecret",
        "public_url": "https://triage.onrender.com",
        "secret_key": None,
        **overrides,
    }
    return make_settings(tmp_path, **values)


def services(settings: Settings) -> Services:
    return build_services(settings, Database(settings.database_path), assistant=MockAssistant())


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"revoked": []}
    monkeypatch.setattr(connections, "exchange_code", lambda s, code, state, verifier: FakeCredentials())
    monkeypatch.setattr(connections, "gmail_address", lambda creds: "joel@example.com")
    monkeypatch.setattr(connections, "credentials_from_token", lambda token: FakeCredentials())
    monkeypatch.setattr(GmailMailbox, "from_credentials", classmethod(lambda cls, c, s, a: FakeGmail(a)))
    monkeypatch.setattr(connections, "revoke", lambda token: calls["revoked"].append(token))
    return calls


def test_app_starts_and_explains_when_gmail_is_not_connected(tmp_path: Path) -> None:
    svc = services(gmail_settings(tmp_path))
    assert svc.mail is None and svc.mail_error == connections.NOT_CONNECTED
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    assert client.get("/api/health").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "Gmail isn&#39;t connected yet" in page.text
    polled = client.post("/poll", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False)
    assert polled.status_code == 303 and "connected" in polled.headers["location"]
    assert client.post("/api/poll").status_code == 409


def test_settings_page_shows_the_redirect_uri_to_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://my-triage.onrender.com")
    settings = gmail_settings(tmp_path, public_url="", google_oauth_client_id=None)
    assert settings.public_url == "https://my-triage.onrender.com"
    svc = services(settings)
    page = TestClient(create_app(svc.settings, svc), base_url="https://testserver").get("/settings").text
    assert "https://my-triage.onrender.com/oauth/google/callback" in page
    assert "GOOGLE_OAUTH_CLIENT_ID" in page


def test_oauth_start_redirects_to_google_with_pkce(tmp_path: Path) -> None:
    svc = services(gmail_settings(tmp_path))
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    response = client.post(
        "/oauth/google/start", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False
    )
    assert response.status_code == 303
    url = urlparse(response.headers["location"])
    query = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == ["cid.apps.googleusercontent.com"]
    assert query["redirect_uri"] == ["https://triage.onrender.com/oauth/google/callback"]
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.modify"]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    saved = connections.read_cookie(svc.settings, response.cookies[connections.OAUTH_COOKIE])
    assert saved and saved["state"] == query["state"][0]
    assert client.post("/oauth/google/start", data={"csrf": "wrong"}).status_code == 403


def test_callback_connects_stores_encrypted_and_survives_a_restart(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    settings = gmail_settings(tmp_path)
    svc = services(settings)
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    start = client.post(
        "/oauth/google/start", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = client.get(f"/oauth/google/callback?state={state}&code=auth-code", follow_redirects=False)
    assert done.status_code == 303 and "Connected+to+joel%40example.com" in done.headers["location"]
    assert svc.mail is not None and svc.mail.mailbox_address == "joel@example.com"
    assert svc.mail_error is None
    with svc.db.session() as conn:
        row = conn.execute("SELECT account, token FROM mail_accounts").fetchone()
    assert row["account"] == "joel@example.com"
    assert "refresh-123" not in row["token"]  # encrypted at rest
    assert "Connected" in client.get("/settings").text

    # A restart (new process, same database and SECRET_KEY) reconnects without signing in again.
    again = services(settings)
    assert again.mail is not None and again.mail.mailbox_address == "joel@example.com"


def test_callback_rejects_a_mismatched_state_or_denied_consent(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    svc = services(gmail_settings(tmp_path))
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    client.post("/oauth/google/start", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False)
    forged = client.get("/oauth/google/callback?state=forged&code=x", follow_redirects=False)
    assert "expired" in forged.headers["location"]
    denied = client.get("/oauth/google/callback?error=access_denied", follow_redirects=False)
    assert "access_denied" in denied.headers["location"]
    with svc.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mail_accounts").fetchone()[0] == 0
    assert svc.mail is None


def test_disconnect_revokes_and_forgets(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    settings = gmail_settings(tmp_path)
    svc = services(settings)
    connections.save_account(svc.db, svc.settings, "gmail", "joel@example.com", {"refresh_token": "r"}, "me")
    svc = services(settings)
    assert svc.mail is not None
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    done = client.post(
        "/settings/mailbox/disconnect", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False
    )
    assert "Disconnected+joel%40example.com" in done.headers["location"]
    assert fake_google["revoked"] == [{"refresh_token": "r"}]
    assert svc.mail is None
    with svc.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mail_accounts").fetchone()[0] == 0


def test_secret_key_is_generated_once_and_kept(tmp_path: Path, fake_google: dict[str, Any]) -> None:
    settings = gmail_settings(tmp_path)
    db = Database(settings.database_path)
    first = connections.ensure_secret_key(settings, db).secret_key
    assert first and connections.ensure_secret_key(settings, db).secret_key == first
    # A token saved under one key can't be read under another: the app starts and asks to reconnect.
    connections.save_account(db, connections.ensure_secret_key(settings, db), "gmail", "a@b.c", {}, "me")
    svc = services(settings.model_copy(update={"secret_key": "a-different-key"}))
    assert svc.mail is None and "SECRET_KEY changed" in (svc.mail_error or "")


def test_revoked_access_during_a_poll_disconnects_cleanly(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    from google.auth.exceptions import RefreshError

    class Revoked(FakeGmail):
        def list_new(self, since: Any, limit: int) -> list[Any]:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    svc = services(gmail_settings(tmp_path))
    svc.mail = Revoked("joel@example.com")
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    polled = client.post("/poll", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False)
    assert polled.status_code == 303
    assert svc.mail is None and "Connect Gmail again" in (svc.mail_error or "")

    from triage.scheduler import run_poll

    svc.mail = Revoked("joel@example.com")
    run_poll(svc)  # the background job must not crash either
    assert svc.mail is None


def gmail_http_error(status: int, message: str, reason: str = "") -> Exception:
    import httplib2
    from googleapiclient.errors import HttpError

    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


def test_mailbox_errors_are_explained() -> None:
    disabled = gmail_http_error(
        403, "Gmail API has not been used in project 123 before or it is disabled.", "accessNotConfigured"
    )
    assert "Gmail API is not enabled" in connections.describe_mail_error(disabled)
    scope = gmail_http_error(
        403, "Request had insufficient authentication scopes.", "insufficientPermissions"
    )
    assert "tick the box" in connections.describe_mail_error(scope)
    assert "rate limit" in connections.describe_mail_error(gmail_http_error(429, "Too many requests"))
    assert "HTTP 500: Backend Error" in connections.describe_mail_error(
        gmail_http_error(500, "Backend Error")
    )
    assert connections.is_auth_failure(gmail_http_error(401, "Invalid Credentials"))
    assert not connections.is_auth_failure(disabled)


def test_a_failing_inbox_check_is_shown_not_a_server_error(
    tmp_path: Path, fake_google: dict[str, Any]
) -> None:
    class Broken(FakeGmail):
        def list_new(self, since: Any, limit: int) -> list[Any]:
            raise gmail_http_error(
                403, "Gmail API has not been used in project 123 before or it is disabled."
            )

    svc = services(gmail_settings(tmp_path))
    svc.mail, svc.mail_error = Broken("joel@example.com"), None
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    polled = client.post("/poll", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False)
    assert polled.status_code == 303 and "Gmail+API+is+not+enabled" in polled.headers["location"]
    assert svc.mail is not None  # still connected: the problem is elsewhere
    assert "Gmail API is not enabled" in client.get("/").text  # and it stays visible on the queue
    assert client.post("/api/poll").status_code == 409

    from triage.scheduler import run_poll

    run_poll(svc)  # the background job records it too
    assert svc.last_poll is not None and not svc.last_poll[2]


def test_a_long_inbox_check_continues_in_the_background(
    tmp_path: Path, fake_google: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from triage.web import app as web

    release = threading.Event()

    class Slow(FakeGmail):
        def list_new(self, since: Any, limit: int) -> list[Any]:
            release.wait(5)
            return []

    monkeypatch.setattr(web, "POLL_WAIT_SECONDS", 0.05)
    svc = services(gmail_settings(tmp_path))
    svc.mail = Slow("joel@example.com")
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    polled = client.post("/poll", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False)
    assert "in+the+background" in polled.headers["location"]
    release.set()


def test_consent_without_the_gmail_permission_is_refused(
    tmp_path: Path, fake_google: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Partial(FakeCredentials):
        granted_scopes = ("openid",)

    monkeypatch.setattr(connections, "exchange_code", lambda s, code, state, verifier: Partial())
    svc = services(gmail_settings(tmp_path))
    client = TestClient(create_app(svc.settings, svc), base_url="https://testserver")
    start = client.post(
        "/oauth/google/start", data={"csrf": csrf_token(svc.settings)}, follow_redirects=False
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = client.get(f"/oauth/google/callback?state={state}&code=auth-code", follow_redirects=False)
    assert "tick+the+box" in done.headers["location"]
    assert svc.mail is None


def test_storage_status_on_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    assert connections.storage_status(settings)[0] == "local"
    monkeypatch.setenv("RENDER", "true")
    kind, detail = connections.storage_status(settings)
    assert kind == "ephemeral" and "persistent disk" in detail


def test_demo_mode_is_unchanged(tmp_path: Path) -> None:
    svc = services(make_settings(tmp_path, secret_key=None))
    assert svc.mail is not None and svc.mail.name == "demo" and svc.mail_error is None
    assert DEMO_DIR.exists()
