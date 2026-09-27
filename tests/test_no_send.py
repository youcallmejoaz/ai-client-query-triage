"""The assistant must never send email. These checks fail the build if a send path appears."""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from triage.mail import base, demo, gmail, graph

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = [gmail, graph, demo]
FORBIDDEN_STRINGS = ("sendmail", "/send", "messages.send", "drafts.send")
# send(), send_message(), sendMail() and similar; not "sender" or "sent".
SEND_NAME = re.compile(r"^send(_\w+|mail|message|draft|reply)?$", re.IGNORECASE)


def test_mail_provider_interface_has_no_send_method() -> None:
    members = [name for name, _ in inspect.getmembers(base.MailProvider) if not name.startswith("_")]
    assert members, "MailProvider should declare its methods"
    assert not [m for m in members if "send" in m.lower()]


@pytest.mark.parametrize("module", PROVIDERS, ids=lambda m: m.__name__)
def test_providers_never_call_a_send_endpoint(module: object) -> None:
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert not SEND_NAME.match(node.attr), f"{node.attr} at line {node.lineno}"
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            assert not SEND_NAME.match(node.name), f"def {node.name} at line {node.lineno}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            lowered = node.value.lower()
            assert not any(s in lowered for s in FORBIDDEN_STRINGS), f"{node.value!r} at line {node.lineno}"


def test_gmail_scope_and_graph_permission_are_documented() -> None:
    assert gmail.SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]
    setup = (ROOT / "docs" / "SETUP-M365.md").read_text(encoding="utf-8")
    assert "Mail.ReadWrite" in setup and "Do not grant `Mail.Send`" in setup


N8N_SEND_OPERATIONS = {"send", "reply", "sendAndWait", "forward"}


@pytest.mark.parametrize("path", sorted((ROOT / "n8n").glob("*.json")), ids=lambda p: p.name)
def test_n8n_workflows_contain_no_send_steps(path: Path) -> None:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    for node in workflow["nodes"]:
        node_type = node["type"].lower()
        if "gmail" in node_type or "outlook" in node_type:
            operation = node.get("parameters", {}).get("operation")
            assert operation not in N8N_SEND_OPERATIONS, f"{path.name}: {node['name']} uses {operation}"
            assert node.get("parameters", {}).get("resource") != "message" or operation != "send"
