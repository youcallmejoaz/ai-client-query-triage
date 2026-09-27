"""Knowledge base lookup.

LocalKB indexes a folder of Markdown articles into SQLite FTS5 (BM25 ranking),
one chunk per "## " section, so a draft can cite the exact section it used.
HttpKB calls an external knowledge service (for example the Project 4
knowledge base API) with the same interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from ..db import Database
from ..models import ContextSource

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "my",
    "of",
    "on",
    "or",
    "our",
    "so",
    "that",
    "the",
    "their",
    "there",
    "this",
    "to",
    "we",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "you",
    "your",
}


class KnowledgeSource(Protocol):
    def search(self, queries: list[str], k: int) -> list[ContextSource]: ...


@dataclass
class Article:
    id: str
    title: str
    category: str | None
    url: str | None
    updated: str | None
    body: str


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_article(path: Path) -> Article:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, object] = {}
    body = raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front) or {}
    return Article(
        id=str(meta.get("id") or path.stem),
        title=str(meta.get("title") or path.stem.replace("-", " ").title()),
        category=str(meta["category"]) if meta.get("category") else None,
        url=str(meta["url"]) if meta.get("url") else None,
        updated=str(meta["updated"]) if meta.get("updated") else None,
        body=body.strip(),
    )


def chunk_article(article: Article) -> list[tuple[str, str, str]]:
    """Split on '## ' headings -> [(chunk_id, heading, text)]. Text before the first heading is 'overview'."""
    chunks: list[tuple[str, str, str]] = []
    heading = "Overview"
    lines: list[str] = []

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            chunks.append((f"{article.id}#{slugify(heading)}", heading, text))

    for line in article.body.splitlines():
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    flush()
    return chunks


def fts_query(text: str) -> str | None:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


class LocalKB:
    def __init__(self, db: Database, kb_dir: Path) -> None:
        self.db = db
        self.kb_dir = kb_dir

    def index(self) -> int:
        """(Re)build the index from kb_dir. Returns the number of articles indexed."""
        articles = [parse_article(p) for p in sorted(self.kb_dir.glob("*.md"))]
        with self.db.session() as conn:
            conn.execute("DELETE FROM kb_chunks")
            conn.execute("DELETE FROM kb_articles")
            for a in articles:
                conn.execute(
                    "INSERT INTO kb_articles (id, title, category, url, updated, body) VALUES (?, ?, ?, ?, ?, ?)",
                    (a.id, a.title, a.category, a.url, a.updated, a.body),
                )
                for chunk_id, heading, text in chunk_article(a):
                    conn.execute(
                        "INSERT INTO kb_chunks (chunk_id, article_id, title, heading, body, url) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (chunk_id, a.id, a.title, heading, text, a.url),
                    )
        return len(articles)

    def ensure_indexed(self) -> None:
        with self.db.session() as conn:
            count = conn.execute("SELECT COUNT(*) FROM kb_articles").fetchone()[0]
        if not count:
            self.index()

    def search(self, queries: list[str], k: int) -> list[ContextSource]:
        """Best k sections across all queries, at most 3 per article, best first."""
        scores: dict[str, float] = {}
        rows: dict[str, tuple[str, str, str, str, str | None]] = {}
        with self.db.session() as conn:
            for q in queries:
                match = fts_query(q)
                if not match:
                    continue
                result = conn.execute(
                    """SELECT chunk_id, article_id, title, heading, body, url,
                              bm25(kb_chunks, 0, 0, 2.0, 3.0, 1.0) AS score
                       FROM kb_chunks WHERE kb_chunks MATCH ? ORDER BY score LIMIT 8""",
                    (match,),
                ).fetchall()
                for row in result:
                    # bm25() is lower-is-better; accumulate as a positive relevance across queries.
                    scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0.0) - float(row["score"])
                    rows[row["chunk_id"]] = (
                        row["article_id"],
                        row["title"],
                        row["heading"],
                        row["body"],
                        row["url"],
                    )
        ranked = sorted(scores, key=lambda cid: scores[cid], reverse=True)
        per_article: dict[str, int] = {}
        out: list[ContextSource] = []
        for chunk_id in ranked:
            article_id, title, heading, body, url = rows[chunk_id]
            if per_article.get(article_id, 0) >= 3:
                continue
            per_article[article_id] = per_article.get(article_id, 0) + 1
            out.append(
                ContextSource(
                    source_id=f"KB:{chunk_id}",
                    kind="kb",
                    title=f"{title} › {heading}",
                    text=body,
                    url=url,
                )
            )
            if len(out) >= k:
                break
        return out

    def article(self, article_id: str) -> Article | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
        return Article(**dict(row)) if row else None


class HttpKB:
    """Adapter for an external knowledge service.

    Expected contract: POST {base_url}/search with {"query": str, "k": int} returns
    {"results": [{"id": str, "title": str, "text": str, "url": str | null}]}.
    """

    def __init__(self, base_url: str, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = client or httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=15)

    def search(self, queries: list[str], k: int) -> list[ContextSource]:
        seen: dict[str, ContextSource] = {}
        for q in queries:
            response = self.client.post("/search", json={"query": q, "k": k})
            response.raise_for_status()
            for item in response.json().get("results", []):
                source_id = f"KB:{item['id']}"
                if source_id not in seen:
                    seen[source_id] = ContextSource(
                        source_id=source_id,
                        kind="kb",
                        title=str(item.get("title") or item["id"]),
                        text=str(item.get("text") or ""),
                        url=item.get("url"),
                    )
        return list(seen.values())[:k]
