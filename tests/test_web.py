from __future__ import annotations

import base64

import pytest
from conftest import Env
from fastapi.testclient import TestClient

from triage.pipeline import process_inbox
from triage.web.app import create_app, csrf_token


@pytest.fixture
def client(env: Env) -> TestClient:
    return TestClient(create_app(env.settings, env.svc))


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_pages_render(env: Env, client: TestClient) -> None:
    assert "Check inbox now" in client.get("/").text
    process_inbox(env.svc)
    queue = client.get("/")
    assert queue.status_code == 200
    assert "Payroll run failed" in queue.text and "Confidential email" in queue.text
    assert "settlement" not in queue.text.lower()  # excluded content never reaches the dashboard
    with env.db.session() as conn:
        qid = conn.execute("SELECT id FROM queries WHERE provider_message_id = 'demo-e02'").fetchone()[0]
    detail = client.get(f"/queries/{qid}")
    assert detail.status_code == 200
    assert "Draft reply" in detail.text and "Fixing a failed payroll run" in detail.text
    assert "Send" not in [b.strip() for b in detail.text.split("<button")[1:]]
    for path in [
        "/digest",
        "/outbox",
        "/kb/refund-policy",
        "/demo/inbox?state=before",
        "/demo/inbox?state=after",
        "/demo/inbox?thread=t-acme-payroll",
        "/?status=closed",
        "/?urgency=high&category=billing",
    ]:
        assert client.get(path).status_code == 200, path
    assert client.get("/queries/9999").status_code == 404


def test_forms_require_the_csrf_token(env: Env, client: TestClient) -> None:
    assert client.post("/poll", data={}).status_code == 403
    ok = client.post("/poll", data={"csrf": csrf_token(env.settings)}, follow_redirects=False)
    assert ok.status_code == 303 and "Checked+the+inbox" in ok.headers["location"]


def test_resolve_reassign_and_regenerate(env: Env, client: TestClient) -> None:
    process_inbox(env.svc)
    token = csrf_token(env.settings)
    with env.db.session() as conn:
        qid = conn.execute("SELECT id FROM queries WHERE provider_message_id = 'demo-e03'").fetchone()[0]
    client.post(f"/queries/{qid}/assign", data={"assignee": "maya", "csrf": token})
    client.post(f"/queries/{qid}/regenerate", data={"instruction": "Be brief", "csrf": token})
    client.post(f"/queries/{qid}/status", data={"action": "resolve", "csrf": token})
    with env.db.session() as conn:
        row = conn.execute("SELECT status, assignee_key FROM queries WHERE id = ?", (qid,)).fetchone()
        drafts = conn.execute("SELECT COUNT(*) FROM drafts WHERE query_id = ?", (qid,)).fetchone()[0]
    assert tuple(row) == ("resolved", "maya") and drafts == 2
    client.post(f"/queries/{qid}/status", data={"action": "reopen", "csrf": token})
    with env.db.session() as conn:
        assert conn.execute("SELECT status FROM queries WHERE id = ?", (qid,)).fetchone()[0] == "draft_ready"


def test_api_triage_for_n8n(client: TestClient) -> None:
    payload = {
        "provider": "gmail",
        "message_id": "18c2f",
        "thread_id": "18c2f",
        "from": "Raj Patel <raj.patel@bluefindental.example>",
        "subject": "Invoice question",
        "text": "Can you resend our invoice with the PO number? It is urgent, our deadline is today.",
        "message_id_header": "<abc@mail.gmail.com>",
    }
    response = client.post("/api/triage", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft_ready"
    assert body["category"] == "billing"
    assert "AI/Triaged" in body["labels"] and "AI/Client/Bluefin Dental Group" in body["labels"]
    draft = body["draft"]
    assert draft["to"] == "Raj Patel <raj.patel@bluefindental.example>"
    assert draft["subject"] == "Re: Invoice question" and draft["in_reply_to"] == "<abc@mail.gmail.com>"
    assert draft["body_text"].endswith("Tidewater Payroll Client Support")
    assert body["dashboard_url"].endswith(f"/queries/{body['query_id']}")
    # Same message again: stored once.
    again = client.post("/api/triage", json=payload).json()
    assert again["query_id"] == body["query_id"]


def test_api_triage_confidential(client: TestClient) -> None:
    body = client.post(
        "/api/triage",
        json={"message_id": "x9", "from": "j@harrowpike.example", "subject": "Private", "text": "Secret"},
    ).json()
    assert body["status"] == "excluded" and body["draft"] is None
    assert body["alert"] and "Secret" not in (body["alert_text"] or "")


def test_api_labels_digest_queries(env: Env, client: TestClient) -> None:
    labels = client.get("/api/labels").json()["labels"]
    assert "AI/Urgency/Critical" in labels and "AI/Assigned/Lena Novak" in labels
    process_inbox(env.svc)
    digest = client.get("/api/digest").json()
    assert digest["open_total"] == 18 and digest["slack"]["blocks"]
    rows = client.get("/api/queries").json()
    assert len(rows) == 18 and "body_text" not in rows[0]
    status = client.post(f"/api/queries/{rows[0]['id']}/status", json={"action": "replied"})
    assert status.json()["status"] == "replied"


def test_login_and_api_key(env: Env) -> None:
    settings = env.settings.model_copy(
        update={"basic_auth_user": "team", "basic_auth_password": "correct-horse-battery", "api_key": "k-123"}
    )
    client = TestClient(create_app(settings, env.svc))
    assert client.get("/").status_code == 401
    assert client.get("/", headers=basic("team", "wrong")).status_code == 401
    assert client.get("/", headers=basic("team", "correct-horse-battery")).status_code == 200
    assert client.get("/api/queries").status_code == 401
    assert client.get("/api/queries", headers={"X-API-Key": "k-123"}).status_code == 200
    assert client.get("/api/health").json() == {"ok": True}


def test_production_refuses_to_run_without_a_login(env: Env) -> None:
    prod = env.settings.model_copy(update={"app_env": "production"})
    client = TestClient(create_app(prod, env.svc))
    assert client.get("/").status_code == 503
    assert client.get("/api/health").status_code == 200
    weak = prod.model_copy(update={"basic_auth_user": "a", "basic_auth_password": "short"})
    assert TestClient(create_app(weak, env.svc)).get("/").status_code == 503
