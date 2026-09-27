from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from triage.config import DEMO_DIR
from triage.context.knowledge import HttpKB, LocalKB, chunk_article, fts_query, parse_article
from triage.db import Database


@pytest.fixture(scope="module")
def kb() -> LocalKB:
    kb = LocalKB(Database(":memory:"), DEMO_DIR / "kb")
    assert kb.index() == len(list((DEMO_DIR / "kb").glob("*.md")))
    return kb


def test_articles_are_chunked_by_section() -> None:
    article = parse_article(DEMO_DIR / "kb" / "payroll-run-errors.md")
    ids = [c[0] for c in chunk_article(article)]
    assert ids == [
        "payroll-run-errors#overview",
        "payroll-run-errors#common-error-codes",
        "payroll-run-errors#payment-cut-off-times",
        "payroll-run-errors#what-support-can-do",
    ]


@pytest.mark.parametrize(
    ("queries", "expected"),
    [
        (["charged twice duplicate direct debit refund"], "KB:refund-policy#duplicate-or-incorrect-charges"),
        (["lost phone two-factor reset"], "KB:sign-in-and-2fa#lost-two-factor-device"),
        (["QuickBooks authorization expired"], "KB:integrations#quickbooks-sync-failures"),
        (["purchase order number on invoice"], "KB:invoices-and-payments#purchase-order-numbers"),
    ],
)
def test_search_finds_the_right_section(kb: LocalKB, queries: list[str], expected: str) -> None:
    results = kb.search(queries, 4)
    assert results[0].source_id == expected
    assert all(r.kind == "kb" and r.url for r in results)


def test_search_limits_sections_per_article(kb: LocalKB) -> None:
    results = kb.search(["refund credit charge annual monthly approve"], 10)
    per_article: dict[str, int] = {}
    for r in results:
        article = r.source_id.split(":", 1)[1].split("#")[0]
        per_article[article] = per_article.get(article, 0) + 1
    assert max(per_article.values()) <= 3


def test_fts_query_is_safe_for_arbitrary_text() -> None:
    # FTS5 operators and punctuation become plain quoted terms; stopwords and single letters are dropped.
    assert fts_query('What "is" AND the OR NEAR(x) -- refund?') == '"near" OR "refund"'
    assert fts_query("the of a") is None


def test_http_kb_adapter(tmp_path: Path) -> None:
    seen: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(
            200,
            json={"results": [{"id": "refunds#annual", "title": "Refunds", "text": "30 days", "url": None}]},
        )

    client = httpx.Client(
        base_url="https://kb.test",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer k"},
    )
    results = HttpKB("https://kb.test", "k", client=client).search(["refund", "annual refund"], 4)
    assert [r.source_id for r in results] == ["KB:refunds#annual"]
    assert seen == [{"query": "refund", "k": 4}, {"query": "annual refund", "k": 4}]
