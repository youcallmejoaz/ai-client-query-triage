"""Dashboard and JSON API (FastAPI).

Pages: the queue, a query's detail with its draft and sources, the daily
summary, the notification outbox, knowledge-base articles and, in demo mode,
a mailbox view for before/after screenshots. There is deliberately no way to
send an email from here: people send replies from Gmail or Outlook.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import connections
from ..ai.base import AssistantError, AssistantRefusal
from ..config import Settings, get_settings
from ..digest import build_digest, digest_message
from ..mail.demo import DemoMailbox
from ..models import CATEGORIES, CATEGORY_LABELS, OPEN_STATUSES, URGENCIES
from ..pipeline import PollReport, Services, poll_safely, regenerate_draft, triage_email
from ..store import (
    audit,
    audit_for,
    current_draft,
    draft_history,
    get_query,
    list_notifications,
    list_queries,
    llm_calls_for,
    sources_for,
    update_query,
)
from ..timeutil import humanize_delta, parse_iso
from .api_models import TriageRequest, TriageResponse, triage_response

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
POLL_WAIT_SECONDS = 20.0
CLOSED_STATUSES = ("replied", "resolved", "no_reply_needed", "superseded")
STATUS_LABELS = {
    "draft_ready": "Draft ready",
    "needs_human": "Needs a person",
    "excluded": "Confidential",
    "no_reply_needed": "No reply needed",
    "replied": "Replied",
    "resolved": "Resolved",
    "superseded": "Superseded",
    "error": "Retrying",
}


# ------------------------------------------------------------------ auth


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return None
    return user, password


def _check_basic(request: Request, settings: Settings) -> str | None:
    creds = _basic_credentials(request)
    if not creds or not settings.basic_auth_user or not settings.basic_auth_password:
        return None
    user_ok = hmac.compare_digest(creds[0], settings.basic_auth_user)
    pass_ok = hmac.compare_digest(creds[1], settings.basic_auth_password)
    return creds[0] if user_ok and pass_ok else None


def auth_problem(settings: Settings) -> str | None:
    """A reason the app must not serve anything, or None."""
    if not settings.is_production or settings.auth_disabled:
        return None
    if not settings.basic_auth_user or not settings.basic_auth_password:
        return "Set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD (or AUTH_DISABLED=true on a private network)."
    if len(settings.basic_auth_password) < 12:
        return "BASIC_AUTH_PASSWORD must be at least 12 characters."
    return None


def csrf_token(settings: Settings) -> str:
    key = settings.secret_key or ""
    return hmac.new(key.encode(), b"triage-csrf", hashlib.sha256).hexdigest()[:32]


def check_csrf(settings: Settings, token: str) -> None:
    if not hmac.compare_digest(token or "", csrf_token(settings)):
        raise HTTPException(status_code=403, detail="Invalid form token. Reload the page and try again.")


# ------------------------------------------------------------------ presentation helpers


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    tz = ZoneInfo(settings.timezone)

    def local(value: str | datetime | None, fmt: str = "%a %d %b, %H:%M") -> str:
        if not value:
            return ""
        dt = parse_iso(value) if isinstance(value, str) else value
        return dt.astimezone(tz).strftime(fmt)

    def ago(value: str | None, now: datetime) -> str:
        if not value:
            return ""
        return humanize_delta((now - parse_iso(value)).total_seconds()) + " ago"

    def mailtime(value: str, now: datetime) -> str:
        dt = parse_iso(value).astimezone(tz)
        return dt.strftime("%H:%M") if dt.date() == now.astimezone(tz).date() else dt.strftime("%-d %b")

    def initials(name: str | None) -> str:
        parts = (name or "?").split()
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()

    templates.env.filters["local"] = local
    templates.env.globals.update(
        ago=ago,
        mailtime=mailtime,
        initials=initials,
        category_label=lambda c: CATEGORY_LABELS.get(c or "", "Unclassified"),
        status_label=lambda s: STATUS_LABELS.get(s, s),
        settings=settings,
        csrf=csrf_token(settings),
        categories=CATEGORIES,
        urgencies=URGENCIES,
    )
    return templates


def due_info(q: dict[str, Any], now: datetime) -> dict[str, str]:
    if q["status"] not in OPEN_STATUSES or not q.get("due_at"):
        return {"text": "", "tone": "none"}
    due = parse_iso(q["due_at"])
    delta = (due - now).total_seconds()
    if delta < 0:
        return {"text": f"Overdue {humanize_delta(-delta)}", "tone": "overdue"}
    tone = "soon" if delta < 4 * 3600 else "ok"
    return {"text": f"in {humanize_delta(delta)}", "tone": tone}


# ------------------------------------------------------------------ app


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    if services is None:
        from ..services import build_services

        services = build_services(settings)
    svc = services
    if not settings.secret_key:
        settings = connections.ensure_secret_key(settings, svc.db)
    templates = build_templates(settings)
    storage_kind, storage_detail = connections.storage_status(settings)
    if storage_kind == "ephemeral":
        log.warning(storage_detail)
    if isinstance(svc.mail, DemoMailbox):
        with svc.db.session() as conn:
            empty = conn.execute("SELECT COUNT(*) FROM demo_messages").fetchone()[0] == 0
        if empty:  # a fresh demo deploy starts with the sample inbox waiting to be triaged
            svc.mail.seed(settings.demo_emails_file, now=svc.clock())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        if settings.scheduler_enabled:
            from ..scheduler import start_scheduler

            scheduler = start_scheduler(svc)
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Client Query Triage", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.services = svc
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if path == "/api/health" or path.startswith("/static/"):
            return await call_next(request)
        problem = auth_problem(settings)
        if problem:
            return Response(f"Login is not configured. {problem}", status_code=503, media_type="text/plain")
        user = _check_basic(request, settings)
        basic_on = bool(settings.basic_auth_user and settings.basic_auth_password)
        if path.startswith("/api/"):
            key = request.headers.get("x-api-key", "")
            key_ok = bool(settings.api_key) and hmac.compare_digest(key, settings.api_key or "")
            open_api = not settings.api_key and not basic_on
            if not (key_ok or user or open_api):
                return JSONResponse({"error": "Unauthorized: send X-API-Key or Basic credentials"}, 401)
            request.state.user = user or ("api-key" if key_ok else "api")
        else:
            if basic_on and not user:
                return Response(
                    "Sign in required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Client Query Triage", charset="UTF-8"'},
                )
            request.state.user = user or "dashboard"
        return await call_next(request)

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        now = svc.clock()
        with svc.db.session() as conn:
            counts = dict(
                conn.execute(
                    f"SELECT 'open', COUNT(*) FROM queries WHERE status IN ({', '.join('?' for _ in OPEN_STATUSES)})",
                    OPEN_STATUSES,
                ).fetchall()
            )
        base = {
            "now": now,
            "open_count": counts.get("open", 0),
            "mail_provider": svc.mail.name if svc.mail else settings.mail_provider,
            "mailbox_address": svc.mail.mailbox_address if svc.mail else None,
            "mail_error": svc.mail_error,
            "last_poll": svc.last_poll,
            "storage_warning": storage_detail if storage_kind == "ephemeral" else None,
            "demo": svc.mail is not None and svc.mail.name == "demo",
            "path": request.url.path,
        }
        return templates.TemplateResponse(request, name, {**base, **context})

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def queue(
        request: Request,
        status: str = "open",
        urgency: str = "",
        category: str = "",
        assignee: str = "",
        flash: str = "",
    ) -> HTMLResponse:
        now = svc.clock()
        statuses = {"open": OPEN_STATUSES, "closed": CLOSED_STATUSES}.get(status)
        with svc.db.session() as conn:
            rows = list_queries(
                conn,
                statuses=statuses,
                urgency=urgency or None,
                category=category or None,
                assignee=assignee or None,
            )
            open_rows = list_queries(conn, statuses=OPEN_STATUSES, limit=1000)
            closed_count = conn.execute(
                f"SELECT COUNT(*) FROM queries WHERE status IN ({', '.join('?' for _ in CLOSED_STATUSES)})",
                CLOSED_STATUSES,
            ).fetchone()[0]
        for q in rows:
            q["due"] = due_info(q, now)
        dues = [due_info(q, now) for q in open_rows]
        stats = {
            "open": len(open_rows),
            "overdue": sum(d["tone"] == "overdue" for d in dues),
            "soon": sum(d["tone"] == "soon" for d in dues),
            "draft_ready": sum(q["status"] == "draft_ready" for q in open_rows),
            "needs_human": sum(q["status"] in ("needs_human", "error") for q in open_rows),
            "excluded": sum(q["status"] == "excluded" for q in open_rows),
            "closed": closed_count,
        }
        return render(
            request,
            "queue.html",
            rows=rows,
            stats=stats,
            filters={"status": status, "urgency": urgency, "category": category, "assignee": assignee},
            team=list(svc.router.team.values()),
            flash=flash,
        )

    def poll_now() -> tuple[PollReport | None, str]:
        return poll_safely(svc)

    @app.post("/poll")
    def poll(request: Request, csrf: str = Form("")) -> RedirectResponse:
        check_csrf(settings, csrf)
        # A first check of a busy inbox can take minutes (every new email gets two AI calls), so wait a
        # little for the result and otherwise let it finish in the background.
        result: list[str] = []
        worker = threading.Thread(target=lambda: result.append(poll_now()[1]), name="poll", daemon=True)
        worker.start()
        worker.join(POLL_WAIT_SECONDS)
        message = (
            result[0]
            if result
            else (
                "Checking the inbox in the background. New queries appear here as they are triaged: "
                "refresh the page in a minute or two."
            )
        )
        return RedirectResponse("/?" + urlencode({"flash": message}), status_code=303)

    # ------------------------------------------------------------------ settings and Connect Gmail

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, flash: str = "") -> HTMLResponse:
        stored = None
        with svc.db.session() as conn:
            row = conn.execute(
                "SELECT account, connected_at, connected_by FROM mail_accounts WHERE provider = 'gmail'"
            ).fetchone()
            if row:
                stored = dict(row)
        return render(
            request,
            "settings.html",
            flash=flash,
            stored=stored,
            oauth_problem=connections.oauth_problem(settings),
            not_connected=connections.NOT_CONNECTED,
            redirect_uri=connections.redirect_uri(settings),
            storage_kind=storage_kind,
            storage_detail=storage_detail,
            gemini_key_set=bool(
                settings.gemini_api_key
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
            ),
        )

    @app.post("/oauth/google/start")
    def oauth_start(request: Request, csrf: str = Form("")) -> RedirectResponse:
        check_csrf(settings, csrf)
        problem = connections.oauth_problem(settings)
        if problem:
            return RedirectResponse("/settings?" + urlencode({"flash": problem}), status_code=303)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        response = RedirectResponse(connections.authorization_url(settings, state, verifier), status_code=303)
        response.set_cookie(
            connections.OAUTH_COOKIE,
            connections.sign_cookie(settings, {"state": state, "verifier": verifier}),
            max_age=connections.OAUTH_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",  # sent on Google's top-level redirect back to us
            secure=settings.public_url.startswith("https://"),
            path="/oauth/google",
        )
        return response

    @app.get("/oauth/google/callback")
    def oauth_callback(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        def done(message: str) -> RedirectResponse:
            response = RedirectResponse("/settings?" + urlencode({"flash": message}), status_code=303)
            response.delete_cookie(connections.OAUTH_COOKIE, path="/oauth/google")
            return response

        if error:
            return done(f"Google sign-in was not completed ({error}).")
        saved = connections.read_cookie(settings, request.cookies.get(connections.OAUTH_COOKIE))
        if not saved or not state or not hmac.compare_digest(saved.get("state", ""), state) or not code:
            return done("The sign-in link expired or didn't match. Press Connect Gmail again.")
        try:
            credentials = connections.exchange_code(settings, code, state, saved["verifier"])
            if connections.missing_scopes(credentials):
                return done(
                    "Google didn't grant permission to manage the mailbox. Press Connect Gmail again and "
                    "tick the box to read, compose and delete Gmail messages."
                )
            account = connections.gmail_address(credentials)
        except Exception as exc:
            log.warning("Google sign-in failed: %s", exc)
            return done(f"Google sign-in failed: {exc}")
        if not credentials.refresh_token:
            return done(
                "Google didn't return a refresh token. Remove the app's access in your Google "
                "account settings, then press Connect Gmail again."
            )
        token = json.loads(credentials.to_json())
        connections.save_account(svc.db, settings, "gmail", account, token, request.state.user)
        with svc.db.session() as conn:
            audit(conn, request.state.user, "mailbox.connected", None, at=svc.clock(), account=account)
        if settings.mail_provider == "gmail":
            from ..mail.gmail import GmailMailbox

            svc.mail = GmailMailbox.from_credentials(credentials, settings, account)
            svc.mail_error = None
            return done(
                f"Connected to {account}. New mail is checked every "
                f"{settings.poll_seconds // 60 or 1} minutes, or press Check inbox now."
            )
        return done(f"Saved the connection to {account}. Set MAIL_PROVIDER=gmail to start using it.")

    @app.post("/settings/mailbox/disconnect")
    def mailbox_disconnect(request: Request, csrf: str = Form("")) -> RedirectResponse:
        check_csrf(settings, csrf)
        try:
            stored = connections.load_account(svc.db, settings, "gmail")
        except connections.TokenUnreadable:
            stored = None
        if stored:
            connections.revoke(stored.token)
        account = connections.delete_account(svc.db, "gmail")
        with svc.db.session() as conn:
            audit(conn, request.state.user, "mailbox.disconnected", None, at=svc.clock(), account=account)
        if settings.mail_provider == "gmail":
            svc.mail = None
            svc.mail_error = connections.NOT_CONNECTED
        message = f"Disconnected {account}." if account else "No Gmail account was connected."
        return RedirectResponse("/settings?" + urlencode({"flash": message}), status_code=303)

    @app.get("/queries/{query_id}", response_class=HTMLResponse)
    def query_detail(request: Request, query_id: int, flash: str = "") -> HTMLResponse:
        now = svc.clock()
        with svc.db.session() as conn:
            q = get_query(conn, query_id)
            if q is None:
                raise HTTPException(404, "Query not found")
            draft = current_draft(conn, query_id)
            history = draft_history(conn, query_id)
            sources = sources_for(conn, query_id)
            activity = audit_for(conn, query_id)
            usage = llm_calls_for(conn, query_id)
            superseded_by = get_query(conn, q["superseded_by"]) if q.get("superseded_by") else None
        by_id = {s.source_id: s for s in sources}
        cited = {c["source_id"] for c in (draft or {}).get("citations", [])}
        client = svc.clients.get(q["client_id"])
        owner = svc.router.member(client.account_owner) if client else None
        q["due"] = due_info(q, now)
        return render(
            request,
            "query.html",
            q=q,
            draft=draft,
            earlier_drafts=[d for d in history if draft is None or d["id"] != draft["id"]],
            sources=sources,
            sources_by_id=by_id,
            cited=cited,
            activity=activity,
            usage=usage,
            client=client,
            owner=owner,
            team=list(svc.router.team.values()),
            superseded_by=superseded_by,
            flash=flash,
        )

    @app.post("/queries/{query_id}/regenerate")
    def regenerate(
        request: Request, query_id: int, instruction: str = Form(""), csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(settings, csrf)
        try:
            regenerate_draft(svc, query_id, instruction.strip() or None, actor=request.state.user)
            flash = "New draft saved in the mailbox."
        except KeyError as exc:
            raise HTTPException(404, "Query not found") from exc
        except ValueError as exc:
            flash = str(exc)
        except (AssistantError, AssistantRefusal) as exc:
            flash = f"Could not write a new draft: {exc}"
        return RedirectResponse(f"/queries/{query_id}?" + urlencode({"flash": flash}), status_code=303)

    @app.post("/queries/{query_id}/status")
    def change_status(
        request: Request, query_id: int, action: str = Form(...), csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(settings, csrf)
        now = svc.clock()
        with svc.db.session() as conn:
            q = get_query(conn, query_id)
            if q is None:
                raise HTTPException(404, "Query not found")
            if action == "resolve":
                update_query(conn, query_id, status="resolved", closed_at=now, closed_by=request.state.user)
            elif action == "reopen":
                reopened = "excluded" if q["excluded_reason"] else "needs_human"
                if current_draft(conn, query_id):
                    reopened = "draft_ready"
                update_query(conn, query_id, status=reopened, closed_at=None, closed_by=None)
            else:
                raise HTTPException(400, "Unknown action")
            audit(conn, request.state.user, f"query.{action}", query_id, at=now)
        return RedirectResponse(f"/queries/{query_id}", status_code=303)

    @app.post("/queries/{query_id}/assign")
    def assign(
        request: Request, query_id: int, assignee: str = Form(...), csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(settings, csrf)
        member = svc.router.member(assignee)
        if member is None:
            raise HTTPException(400, "Unknown team member")
        with svc.db.session() as conn:
            if get_query(conn, query_id) is None:
                raise HTTPException(404, "Query not found")
            update_query(
                conn,
                query_id,
                assignee_key=member.key,
                assignee_name=member.name,
                route_rule="manual",
                route_reason=f"Reassigned by {request.state.user}",
            )
            audit(conn, request.state.user, "query.reassigned", query_id, at=svc.clock(), to=member.key)
        return RedirectResponse(f"/queries/{query_id}", status_code=303)

    @app.get("/digest", response_class=HTMLResponse)
    def digest_page(request: Request, flash: str = "") -> HTMLResponse:
        digest = build_digest(svc.db, svc.clock())
        max_open = max((o for _, o, _ in digest.by_assignee), default=1) or 1
        return render(request, "digest.html", digest=digest, max_open=max_open, flash=flash)

    @app.post("/digest/send")
    def digest_send(request: Request, csrf: str = Form("")) -> RedirectResponse:
        check_csrf(settings, csrf)
        digest = build_digest(svc.db, svc.clock())
        delivered = svc.notifier.send(digest_message(digest, settings))
        channel = settings.notify
        if channel == "log":
            flash = "Summary added to the outbox (NOTIFY=log)."
        else:
            flash = f"Summary posted to {channel.title()}." if delivered else f"Could not post to {channel}."
        return RedirectResponse("/digest?" + urlencode({"flash": flash}), status_code=303)

    @app.get("/outbox", response_class=HTMLResponse)
    def outbox(request: Request) -> HTMLResponse:
        with svc.db.session() as conn:
            notes = list_notifications(conn, limit=100)
        return render(request, "outbox.html", notes=notes)

    @app.get("/kb/{article_id}", response_class=HTMLResponse)
    def kb_article(request: Request, article_id: str) -> HTMLResponse:
        with svc.db.session() as conn:
            row = conn.execute("SELECT * FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
            chunks = conn.execute(
                "SELECT chunk_id, heading, body FROM kb_chunks WHERE article_id = ? ORDER BY rowid",
                (article_id,),
            ).fetchall()
        if row is None:
            raise HTTPException(404, "Article not found")
        return render(request, "kb.html", article=dict(row), chunks=[dict(c) for c in chunks])

    # ------------------------------------------------------------------ demo mailbox

    def demo_box() -> DemoMailbox:
        if not isinstance(svc.mail, DemoMailbox):
            raise HTTPException(404, "The demo mailbox is only available with MAIL_PROVIDER=demo")
        return svc.mail

    @app.get("/demo/inbox", response_class=HTMLResponse)
    def demo_inbox(request: Request, state: str = "after", thread: str = "") -> HTMLResponse:
        box = demo_box()
        threads = box.threads()
        after = state != "before"
        label_counts: dict[str, int] = {}
        if after:
            for t in threads:
                for lbl in t["labels"]:
                    label_counts[lbl] = label_counts.get(lbl, 0) + 1
        # Sidebar groups as Gmail nests them: "AI" for AI/Draft ready etc., "AI/Urgency" for AI/Urgency/High ...
        groups: dict[str, list[tuple[str, str, int]]] = {}
        order = {"Urgency": 0, "Category": 2, "Assigned": 3, "Client": 4}
        for full, count in label_counts.items():
            parts = full.split("/")
            group = "/".join(parts[:-1])
            groups.setdefault(group, []).append((parts[-1], full, count))
        label_groups = sorted(groups.items(), key=lambda g: order.get(g[0].split("/")[-1], 1))
        urgency_rank = {u.title(): i for i, u in enumerate(URGENCIES)}
        for _, items in label_groups:
            items.sort(key=lambda i: (urgency_rank.get(i[0], 99), i[0]))
        selected = next((t for t in threads if t["thread_id"] == thread), None) if thread else None
        return render(
            request,
            "demo_inbox.html",
            threads=threads,
            after=after,
            label_groups=label_groups,
            selected=selected,
            mailbox=settings.mailbox_address,
        )

    @app.post("/demo/reset")
    def demo_reset(request: Request, csrf: str = Form("")) -> RedirectResponse:
        check_csrf(settings, csrf)
        box = demo_box()
        svc.db.reset(demo=True)
        box.seed(settings.demo_emails_file, now=svc.clock())
        from ..context.knowledge import LocalKB

        if isinstance(svc.kb, LocalKB):
            svc.kb.index()
        flash = "Demo mailbox reset: 23 new emails are waiting. Press 'Check inbox now'."
        return RedirectResponse("/?" + urlencode({"flash": flash}), status_code=303)

    # ------------------------------------------------------------------ JSON API

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        # Only liveness: a disconnected mailbox must not make Render restart the service in a loop.
        return {"ok": True}

    @app.post("/api/triage", response_model=TriageResponse)
    def api_triage(request: Request, body: TriageRequest) -> TriageResponse:
        """Triage one email for an external workflow (n8n). Nothing is written to any mailbox:
        the caller saves the returned draft and labels itself."""
        email = body.to_email()
        provider = f"n8n-{body.provider}"
        outcome = triage_email(svc, email, provider=provider, source="api", mailbox=None)
        return triage_response(outcome, email, settings)

    @app.post("/api/poll")
    def api_poll() -> dict[str, Any]:
        report, message = poll_now()
        if report is None:
            raise HTTPException(409, message)
        return {**report.as_dict(), "message": message}

    @app.get("/api/digest")
    def api_digest(send: bool = False) -> dict[str, Any]:
        digest = build_digest(svc.db, svc.clock())
        message = digest_message(digest, settings)
        if send:
            svc.notifier.send(message)
        return {
            **digest.as_dict(),
            "text": message.title + "\n" + "\n".join(message.lines),
            "slack": svc.notifier.payload(message) if settings.notify != "teams" else None,
            "teams": svc.notifier.payload(message) if settings.notify == "teams" else None,
        }

    @app.get("/api/queries")
    def api_queries(status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        statuses = {"open": OPEN_STATUSES, "closed": CLOSED_STATUSES}.get(status)
        now = svc.clock()
        with svc.db.session() as conn:
            rows = list_queries(conn, statuses=statuses, limit=min(limit, 500))
        for q in rows:
            q.pop("body_text", None)
            q["due"] = due_info(q, now)["text"]
        return rows

    @app.post("/api/queries/{query_id}/status")
    def api_status(request: Request, query_id: int, payload: dict[str, str]) -> dict[str, Any]:
        action = payload.get("action")
        if action not in ("resolve", "replied"):
            raise HTTPException(400, "action must be 'resolve' or 'replied'")
        now = svc.clock()
        with svc.db.session() as conn:
            if get_query(conn, query_id) is None:
                raise HTTPException(404, "Query not found")
            status = "resolved" if action == "resolve" else "replied"
            update_query(conn, query_id, status=status, closed_at=now, closed_by=request.state.user)
            audit(conn, request.state.user, f"query.{status}", query_id, at=now)
        return {"id": query_id, "status": status}

    @app.get("/api/labels")
    def api_labels() -> dict[str, list[str]]:
        """Every label/category name the assistant may apply, for creating them up front (n8n setup)."""
        from ..mail.base import DRAFT_READY, EXCLUDED, NEEDS_HUMAN, NO_REPLY, TRIAGED, label

        names = [TRIAGED, DRAFT_READY, NEEDS_HUMAN, EXCLUDED, NO_REPLY]
        names += [label("Urgency", u.title()) for u in URGENCIES]
        names += [label("Category", CATEGORY_LABELS[c]) for c in CATEGORIES]
        names += [label("Assigned", m.name) for m in svc.router.team.values()]
        names += [label("Client", c.name) for c in svc.clients.clients]
        return {"labels": names}

    return app
