"""Small repository layer over the SQLite schema in db.py."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from .models import OPEN_STATUSES, ContextSource
from .timeutil import iso

Row = dict[str, Any]

JSON_COLUMNS = {"escalation_flags", "questions", "labels", "citations", "missing_info", "dropped_citations"}


def _row(row: sqlite3.Row | None) -> Row | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        if isinstance(out[key], str):
            out[key] = json.loads(out[key])
    return out


def _rows(rows: Iterable[sqlite3.Row]) -> list[Row]:
    return [r for r in (_row(row) for row in rows) if r is not None]


def _encode(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in JSON_COLUMNS and not isinstance(value, str):
            out[key] = json.dumps(value)
        elif isinstance(value, datetime):
            out[key] = iso(value)
        elif isinstance(value, bool):
            out[key] = int(value)
        else:
            out[key] = value
    return out


# ------------------------------------------------------------------ queries


def get_query(conn: sqlite3.Connection, query_id: int) -> Row | None:
    return _row(conn.execute("SELECT * FROM queries WHERE id = ?", (query_id,)).fetchone())


def get_query_by_message(conn: sqlite3.Connection, provider: str, message_id: str) -> Row | None:
    return _row(
        conn.execute(
            "SELECT * FROM queries WHERE provider = ? AND provider_message_id = ?", (provider, message_id)
        ).fetchone()
    )


def save_query(conn: sqlite3.Connection, provider: str, message_id: str, fields: dict[str, Any]) -> int:
    """Insert the query for (provider, message_id), or update it if it exists (e.g. a retry)."""
    data = _encode(fields)
    existing = get_query_by_message(conn, provider, message_id)
    if existing:
        if data:
            assignments = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE queries SET {assignments} WHERE id = ?", (*data.values(), existing["id"]))
        return int(existing["id"])
    data = {"provider": provider, "provider_message_id": message_id, **data}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO queries ({cols}) VALUES ({marks})", tuple(data.values()))
    return int(cur.lastrowid or 0)


def update_query(conn: sqlite3.Connection, query_id: int, **fields: Any) -> None:
    data = _encode(fields)
    assignments = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE queries SET {assignments} WHERE id = ?", (*data.values(), query_id))


def open_queries_in_thread(
    conn: sqlite3.Connection, provider: str, thread_id: str, exclude_id: int | None = None
) -> list[Row]:
    marks = ", ".join("?" for _ in OPEN_STATUSES)
    return _rows(
        conn.execute(
            f"SELECT * FROM queries WHERE provider = ? AND thread_id = ? AND status IN ({marks}) AND id != ?",
            (provider, thread_id, *OPEN_STATUSES, exclude_id or -1),
        ).fetchall()
    )


def open_queries(conn: sqlite3.Connection, provider: str | None = None, limit: int = 500) -> list[Row]:
    marks = ", ".join("?" for _ in OPEN_STATUSES)
    sql = f"SELECT * FROM queries WHERE status IN ({marks})"
    params: list[Any] = list(OPEN_STATUSES)
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    sql += " ORDER BY received_at LIMIT ?"
    params.append(limit)
    return _rows(conn.execute(sql, params).fetchall())


def recent_for_client(
    conn: sqlite3.Connection, client_id: str, before: datetime, limit: int = 3
) -> list[Row]:
    return _rows(
        conn.execute(
            """SELECT * FROM queries WHERE client_id = ? AND received_at < ? AND summary IS NOT NULL
               ORDER BY received_at DESC LIMIT ?""",
            (client_id, iso(before), limit),
        ).fetchall()
    )


URGENCY_ORDER = (
    "CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
)


def list_queries(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str] | None = None,
    category: str | None = None,
    urgency: str | None = None,
    assignee: str | None = None,
    limit: int = 200,
) -> list[Row]:
    where: list[str] = []
    params: list[Any] = []
    if statuses:
        where.append(f"status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    for column, value in (("category", category), ("urgency", urgency), ("assignee_key", assignee)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    sql = "SELECT * FROM queries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {URGENCY_ORDER}, COALESCE(due_at, received_at), received_at LIMIT ?"
    params.append(limit)
    return _rows(conn.execute(sql, params).fetchall())


# ------------------------------------------------------------------ drafts and sources


def insert_draft(conn: sqlite3.Connection, query_id: int, fields: dict[str, Any]) -> int:
    data = _encode({"query_id": query_id, **fields})
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO drafts ({cols}) VALUES ({marks})", tuple(data.values()))
    return int(cur.lastrowid or 0)


def current_draft(conn: sqlite3.Connection, query_id: int) -> Row | None:
    return _row(
        conn.execute(
            "SELECT * FROM drafts WHERE query_id = ? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
            (query_id,),
        ).fetchone()
    )


def draft_history(conn: sqlite3.Connection, query_id: int) -> list[Row]:
    return _rows(
        conn.execute("SELECT * FROM drafts WHERE query_id = ? ORDER BY id DESC", (query_id,)).fetchall()
    )


def supersede_drafts(conn: sqlite3.Connection, query_id: int, at: datetime) -> None:
    conn.execute(
        "UPDATE drafts SET superseded_at = ? WHERE query_id = ? AND superseded_at IS NULL",
        (iso(at), query_id),
    )


def replace_sources(conn: sqlite3.Connection, query_id: int, sources: Sequence[ContextSource]) -> None:
    conn.execute("DELETE FROM query_sources WHERE query_id = ?", (query_id,))
    conn.executemany(
        "INSERT INTO query_sources (query_id, source_id, kind, title, text, url) VALUES (?, ?, ?, ?, ?, ?)",
        [(query_id, s.source_id, s.kind, s.title, s.text, s.url) for s in sources],
    )


def sources_for(conn: sqlite3.Connection, query_id: int) -> list[ContextSource]:
    rows = conn.execute(
        "SELECT source_id, kind, title, text, url FROM query_sources WHERE query_id = ? ORDER BY rowid",
        (query_id,),
    ).fetchall()
    return [ContextSource(**dict(r)) for r in rows]


# ------------------------------------------------------------------ usage, notifications, audit


def record_llm_call(conn: sqlite3.Connection, query_id: int | None, fields: dict[str, Any]) -> None:
    data = _encode({"query_id": query_id, **fields})
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    conn.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({marks})", tuple(data.values()))


def llm_calls_for(conn: sqlite3.Connection, query_id: int) -> list[Row]:
    return _rows(
        conn.execute("SELECT * FROM llm_calls WHERE query_id = ? ORDER BY id", (query_id,)).fetchall()
    )


def insert_notification(conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
    data = _encode(fields)
    for key in ("message", "payload"):
        if not isinstance(data.get(key), str):
            data[key] = json.dumps(data.get(key))
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO notifications ({cols}) VALUES ({marks})", tuple(data.values()))
    return int(cur.lastrowid or 0)


def list_notifications(conn: sqlite3.Connection, limit: int = 50) -> list[Row]:
    rows = _rows(conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
    for row in rows:
        row["message"] = json.loads(row["message"])
        row["payload"] = json.loads(row["payload"])
    return rows


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    query_id: int | None = None,
    at: datetime | None = None,
    **detail: Any,
) -> None:
    from .timeutil import utcnow

    conn.execute(
        "INSERT INTO audit (at, actor, action, query_id, detail) VALUES (?, ?, ?, ?, ?)",
        (iso(at or utcnow()), actor, action, query_id, json.dumps(detail, default=str)),
    )


def audit_for(conn: sqlite3.Connection, query_id: int) -> list[Row]:
    rows = _rows(conn.execute("SELECT * FROM audit WHERE query_id = ? ORDER BY id", (query_id,)).fetchall())
    for row in rows:
        row["detail"] = json.loads(row["detail"])
    return rows
