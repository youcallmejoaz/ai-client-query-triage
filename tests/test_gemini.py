"""GeminiAssistant against a local stub of the Gemini API: request shape, parsing and failure handling."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import NOW, make_settings
from google import genai
from google.genai import types

from triage.ai.base import AssistantError, AssistantRefusal
from triage.ai.gemini import GeminiAssistant
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


def reply(text: str | None, finish: str = "STOP", **extra: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {"finishReason": finish}
    if text is not None:
        candidate["content"] = {"parts": [{"text": text}], "role": "model"}
    return {
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": 1200,
            "candidatesTokenCount": 250,
            "thoughtsTokenCount": 40,
            "cachedContentTokenCount": 900,
        },
        "modelVersion": "gemini-3.8-flash",
        **extra,
    }


def error(code: int, status: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": status.lower(), "status": status}}


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


def assistant(tmp_path: Path, base_url: str, **overrides: Any) -> GeminiAssistant:
    client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(base_url=base_url, retry_options=types.HttpRetryOptions(attempts=1)),
    )
    return GeminiAssistant(make_settings(tmp_path, ai_provider="gemini", **overrides), client=client)


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
    state.responses.append((200, reply(json.dumps(CLASSIFICATION))))
    result = assistant(tmp_path, url).classify(EMAIL, None)

    assert result.value.category == "billing"
    assert result.value.confidence == 1.0  # clamped
    assert result.usage is not None
    assert result.usage.model == "gemini-3.8-flash"
    assert result.usage.cache_read_tokens == 900
    assert result.usage.output_tokens == 290  # answer + thinking tokens

    request = state.requests[0]
    body = request["body"]
    assert request["path"] == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert request["headers"]["x-goog-api-key"] == "test-key"
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "category" in config["responseSchema"]["properties"]
    assert json.dumps(config["thinkingConfig"]).count("LOW") == 1
    assert "Tidewater Payroll" in body["systemInstruction"]["parts"][0]["text"]
    user = body["contents"][0]["parts"][0]["text"]
    assert "We were charged twice." in user
    assert "old quoted text" not in user  # quoted history is stripped


def test_draft_uses_draft_thinking_and_includes_sources(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, url = stub
    draft = {"body": "Hi Raj,\n\nSorted.", "citations": [], "missing_info": [], "confidence": 0.8}
    state.responses.append((200, reply(json.dumps(draft))))
    source = ContextSource(source_id="KB:refund-policy#x", kind="kb", title="Refunds", text="Five days.")
    result = assistant(tmp_path, url).draft(
        EMAIL, Classification.model_validate(CLASSIFICATION | {"confidence": 0.9}), None, [source], "Be brief"
    )
    assert result.value.body.startswith("Hi Raj")
    body = state.requests[0]["body"]
    assert json.dumps(body["generationConfig"]["thinkingConfig"]).count("MEDIUM") == 1
    content = body["contents"][0]["parts"][0]["text"]
    assert '<source id="KB:refund-policy#x"' in content and "Be brief" in content


def test_model_is_configurable(tmp_path: Path, stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.responses.append((200, reply(json.dumps(CLASSIFICATION))))
    assistant(tmp_path, url, gemini_model="gemini-3.5-flash-lite").classify(EMAIL, None)
    assert state.requests[0]["path"] == "/v1beta/models/gemini-3.5-flash-lite:generateContent"


@pytest.mark.parametrize(
    "payload",
    [
        reply(None, "SAFETY"),
        reply(None, "PROHIBITED_CONTENT"),
        {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 10}},
    ],
)
def test_blocked_responses_are_refusals(
    tmp_path: Path, stub: tuple[Stub, str], payload: dict[str, Any]
) -> None:
    state, url = stub
    state.responses.append((200, payload))
    with pytest.raises(AssistantRefusal):
        assistant(tmp_path, url).classify(EMAIL, None)


@pytest.mark.parametrize(
    ("status", "payload", "retryable"),
    [
        (200, reply('{"category": "bil', "MAX_TOKENS"), True),
        (200, reply('{"category": "nonsense"}'), True),
        (429, error(429, "RESOURCE_EXHAUSTED"), True),
        (503, error(503, "UNAVAILABLE"), True),
        (400, error(400, "INVALID_ARGUMENT"), False),
        (403, error(403, "PERMISSION_DENIED"), False),
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


def test_connection_failure_is_retryable(tmp_path: Path) -> None:
    with pytest.raises(AssistantError) as info:
        assistant(tmp_path, "http://127.0.0.1:9").classify(EMAIL, None)
    assert info.value.retryable
