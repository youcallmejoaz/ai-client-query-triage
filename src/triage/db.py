"""SQLite schema and connection handling.

One short-lived connection per unit of work (a poll, a request) keeps the web
server, the scheduler thread and the CLI from sharing a connection. WAL mode
lets the dashboard read while a poll writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
  id                  INTEGER PRIMARY KEY,
  provider            TEXT NOT NULL,              -- demo | gmail | graph | n8n-gmail | n8n-outlook
  provider_message_id TEXT NOT NULL,
  thread_id           TEXT NOT NULL,
  source              TEXT NOT NULL DEFAULT 'poll', -- poll | api
  sender_email        TEXT NOT NULL,
  sender_name         TEXT,
  subject             TEXT,                       -- NULL for excluded (confidential) clients
  body_text           TEXT,                       -- NULL for excluded (confidential) clients
  received_at         TEXT NOT NULL,
  client_id           TEXT,
  client_name         TEXT,
  client_tier         TEXT,
  status              TEXT NOT NULL,
  category            TEXT,
  urgency             TEXT,
  urgency_reason      TEXT,
  sentiment           TEXT,
  complexity          TEXT,
  confidence          REAL,
  summary             TEXT,
  client_hint         TEXT,                       -- company the sender claims (unverified, display only)
  escalation_flags    TEXT NOT NULL DEFAULT '[]',
  questions           TEXT NOT NULL DEFAULT '[]',
  assignee_key        TEXT,
  assignee_name       TEXT,
  route_rule          TEXT,
  route_reason        TEXT,
  alerted             INTEGER NOT NULL DEFAULT 0,
  due_at              TEXT,
  labels              TEXT NOT NULL DEFAULT '[]',
  excluded_reason     TEXT,
  error               TEXT,
  attempts            INTEGER NOT NULL DEFAULT 0,
  processed_at        TEXT,
  closed_at           TEXT,
  closed_by           TEXT,
  superseded_by       INTEGER REFERENCES queries(id),
  UNIQUE (provider, provider_message_id)
);
CREATE INDEX IF NOT EXISTS queries_status_idx ON queries (status, due_at);
CREATE INDEX IF NOT EXISTS queries_thread_idx ON queries (provider, thread_id);
CREATE INDEX IF NOT EXISTS queries_client_idx ON queries (client_id, received_at);

CREATE TABLE IF NOT EXISTS drafts (
  id                INTEGER PRIMARY KEY,
  query_id          INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
  body              TEXT NOT NULL,              -- the reply as written by the model
  full_text         TEXT NOT NULL,              -- what was saved in the mailbox (note + body + signature)
  citations         TEXT NOT NULL DEFAULT '[]',
  missing_info      TEXT NOT NULL DEFAULT '[]',
  dropped_citations TEXT NOT NULL DEFAULT '[]',
  confidence        REAL,
  unsupported       INTEGER NOT NULL DEFAULT 0,
  instruction       TEXT,
  provider_draft_id TEXT,
  provider_version  TEXT,                       -- changes when someone edits the draft in the mailbox
  web_link          TEXT,
  created_at        TEXT NOT NULL,
  superseded_at     TEXT
);
CREATE INDEX IF NOT EXISTS drafts_query_idx ON drafts (query_id);

CREATE TABLE IF NOT EXISTS query_sources (
  query_id  INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  kind      TEXT NOT NULL,
  title     TEXT NOT NULL,
  text      TEXT NOT NULL,
  url       TEXT,
  PRIMARY KEY (query_id, source_id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id                    INTEGER PRIMARY KEY,
  query_id              INTEGER REFERENCES queries(id) ON DELETE SET NULL,
  purpose               TEXT NOT NULL,          -- classify | draft
  model                 TEXT NOT NULL,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  stop_reason           TEXT,
  created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY,
  channel    TEXT NOT NULL,                     -- log | slack | teams
  kind       TEXT NOT NULL,                     -- alert | digest
  query_id   INTEGER REFERENCES queries(id) ON DELETE SET NULL,
  message    TEXT NOT NULL,                     -- channel-neutral JSON (title, lines, links)
  payload    TEXT NOT NULL,                     -- what was (or would be) posted
  delivered  INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id       INTEGER PRIMARY KEY,
  at       TEXT NOT NULL,
  actor    TEXT NOT NULL,
  action   TEXT NOT NULL,
  query_id INTEGER,
  detail   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kb_articles (
  id       TEXT PRIMARY KEY,
  title    TEXT NOT NULL,
  category TEXT,
  url      TEXT,
  updated  TEXT,
  body     TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks USING fts5(
  chunk_id UNINDEXED, article_id UNINDEXED, title, heading, body, url UNINDEXED,
  tokenize = 'porter unicode61'
);

-- The offline demo mailbox (MAIL_PROVIDER=demo).
CREATE TABLE IF NOT EXISTS demo_messages (
  id          TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  from_name   TEXT,
  from_email  TEXT NOT NULL,
  to_email    TEXT NOT NULL,
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL,
  received_at TEXT NOT NULL,
  message_id  TEXT NOT NULL,
  headers     TEXT NOT NULL DEFAULT '{}',
  attachments TEXT NOT NULL DEFAULT '[]',
  labels      TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS demo_drafts (
  id          TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  in_reply_to TEXT NOT NULL,
  to_email    TEXT NOT NULL,
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  deleted_at  TEXT
);
"""

APP_TABLES = ("drafts", "query_sources", "llm_calls", "notifications", "audit", "queries")
DEMO_TABLES = ("demo_drafts", "demo_messages")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A named shared-cache memory database lets tests use ":memory:" across connections.
        self._uri = self.path == ":memory:"
        if self._uri:
            import uuid

            self.path = f"file:mem-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keepalive: sqlite3.Connection | None = self._open()
        else:
            self._keepalive = None
        with self.session() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, uri=self._uri, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._uri:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success, rolls back on error, and is always closed."""
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset(self, *, demo: bool = True) -> None:
        with self.session() as conn:
            for table in APP_TABLES + (DEMO_TABLES if demo else ()):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM kb_chunks")
            conn.execute("DELETE FROM kb_articles")
