"""ClaudeAssistant against a local stub of the Messages API: request shape, parsing and failure handling."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anthropic
import pytest
from conftest import NOW, make_settings

from triage.ai.base import AssistantError, AssistantRefusal
from triage.ai.claude import FALLBACK_BETA, ClaudeAssistant
from triage.models import Classification, ContextSource, EmailAddress, InboundEmail

CLASSIFICATION = {
    "category": "billing",
    "urgency": "high",
    "urgency_reason": "Duplicate charge.",
    "sentiment": "neutral",
    "summary": "Charged twice.",
    "needs_reply": True,
    "complexity": "simple",
    "confidence": 1.4,
    "questions": ["Refund?"],
    "escalation_flags": [],
    "kb_queries": ["duplicate charge refund"],
    "client_hint": None,
}


class Stub:
    def __init__(self) -> None:
        self.responses: list[tuple[int, dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []


def message(content: list[dict[str, Any]], stop_reason: str = "end_turn", **extra: Any) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 250,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
        **extra,
    }


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            status, payload = state.responses.pop(0)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def assistant(tmp_path: Path, base_url: str, **overrides: Any) -> ClaudeAssistant:
    client = anthropic.Anthropic(api_key="test-key", base_url=base_url, max_retries=0)
    return ClaudeAssistant(make_settings(tmp_path, ai_provider="claude", **overrides), client=client)


EMAIL = InboundEmail(
    provider_id="m1",
    thread_id="t1",
    sender=EmailAddress(email="raj@bluefindental.example", name="Raj Patel"),
    subject="Charged twice",
    body_text="We were charged twice.\n\nOn Mon, 1 Sep 2026, Support wrote:\n> old quoted text",
    received_at=NOW,
)


def test_classify_request_shape_and_parsing(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.responses.append((200, message([{"type": "text", "text": json.dumps(CLASSIFICATION)}])))
    result = assistant(tmp_path, url).classify(EMAIL, None)

    assert result.value.category == "billing"
    assert result.value.confidence == 1.0  # clamped
    assert result.usage and result.usage.cache_read_tokens == 900

    request = state.requests[0]
    body = request["body"]
    assert request["path"].startswith("/v1/messages")
    assert FALLBACK_BETA in request["headers"].get("anthropic-beta", "")
    assert body["model"] == "claude-opus-5"
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "category" in body["output_config"]["format"]["schema"]["properties"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Tidewater Payroll" in body["system"][0]["text"]
    user = body["messages"][0]["content"]
    assert "We were charged twice." in user
    assert "old quoted text" not in user  # quoted history is stripped


def test_draft_uses_draft_effort_and_includes_sources(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, url = stub
    draft = {"body": "Hi Raj,\n\nSorted.", "citations": [], "missing_info": [], "confidence": 0.8}
    state.responses.append(
        (
            200,
            message(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": json.dumps(draft)},
                ]
            ),
        )
    )
    source = ContextSource(source_id="KB:refund-policy#x", kind="kb", title="Refunds", text="Five days.")
    result = assistant(tmp_path, url).draft(
        EMAIL, Classification.model_validate(CLASSIFICATION | {"confidence": 0.9}), None, [source], "Be brief"
    )
    assert result.value.body.startswith("Hi Raj")
    body = state.requests[0]["body"]
    assert body["output_config"]["effort"] == "medium"
    content = body["messages"][0]["content"]
    assert '<source id="KB:refund-policy#x"' in content and "Be brief" in content


def test_fallbacks_can_be_turned_off(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.responses.append((200, message([{"type": "text", "text": json.dumps(CLASSIFICATION)}])))
    assistant(tmp_path, url, anthropic_fallbacks="off").classify(EMAIL, None)
    assert "fallbacks" not in state.requests[0]["body"]


def test_refusal_is_reported(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.responses.append(
        (
            200,
            message([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}),
        )
    )
    with pytest.raises(AssistantRefusal, match="cyber"):
        assistant(tmp_path, url).classify(EMAIL, None)


@pytest.mark.parametrize(
    ("status", "payload", "retryable"),
    [
        (200, message([{"type": "text", "text": '{"category": "bil'}], "max_tokens"), True),
        (200, message([{"type": "text", "text": '{"category": "nonsense"}'}]), True),
        (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, True),
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, True),
        (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, False),
    ],
)
def test_failures_become_assistant_errors(
    tmp_path: Path, stub: tuple[Stub, str], status: int, payload: dict[str, Any], retryable: bool
) -> None:
    state, url = stub
    state.responses.append((status, payload))
    with pytest.raises(AssistantError) as info:
        assistant(tmp_path, url).classify(EMAIL, None)
    assert info.value.retryable is retryable
