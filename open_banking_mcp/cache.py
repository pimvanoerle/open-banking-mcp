"""Local SQLite mirror of the bank data.

Exists because TrueLayer throttles unattended callers hard (roughly 4 calls a
day without an X-PSU-IP header) and caches responses for an hour anyway. A
daily sync writes here; reads are served from here unless the caller asks for
fresh data explicitly.

Every read returns a `synced_at` alongside the rows so callers can say how old
the answer is rather than implying it is live.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .storage import DEFAULT_DIR

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    provider_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (provider_id, kind, key)
);

CREATE TABLE IF NOT EXISTS transactions (
    provider_id    TEXT NOT NULL,
    holder_kind    TEXT NOT NULL,          -- 'account' or 'card'
    holder_id      TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    timestamp      TEXT,
    amount         REAL,
    currency       TEXT,
    description    TEXT,
    category       TEXT,
    merchant       TEXT,
    is_pending     INTEGER NOT NULL DEFAULT 0,
    payload        TEXT NOT NULL,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (provider_id, holder_kind, holder_id, transaction_id, is_pending)
);

CREATE INDEX IF NOT EXISTS idx_tx_holder_time
    ON transactions (provider_id, holder_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tx_time
    ON transactions (provider_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,             -- 'running' | 'ok' | 'error'
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Cache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_DIR / "cache.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- snapshots (small collections stored whole) ------------------------

    def put_snapshot(
        self, provider_id: str, kind: str, payload: Any, key: str = ""
    ) -> str:
        synced_at = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(provider_id, kind, key, payload, synced_at) VALUES (?, ?, ?, ?, ?)",
                (provider_id, kind, key, json.dumps(payload), synced_at),
            )
        return synced_at

    def get_snapshot(
        self, provider_id: str, kind: str, key: str = ""
    ) -> tuple[Any, datetime] | None:
        """Return (payload, synced_at) or None if never synced."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, synced_at FROM snapshots "
                "WHERE provider_id = ? AND kind = ? AND key = ?",
                (provider_id, kind, key),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload"]), parse_ts(row["synced_at"])

    # -- transactions ------------------------------------------------------

    def put_transactions(
        self,
        provider_id: str,
        holder_kind: str,
        holder_id: str,
        rows: list[dict],
        *,
        is_pending: bool = False,
        replace_pending: bool = True,
    ) -> str:
        """Upsert transactions.

        Pending rows are replaced wholesale: they carry unstable ids and vanish
        once they settle, so merging them would leave ghosts behind forever.
        """
        synced_at = _now()
        with self._conn() as conn:
            if is_pending and replace_pending:
                conn.execute(
                    "DELETE FROM transactions WHERE provider_id = ? AND holder_kind = ?"
                    " AND holder_id = ? AND is_pending = 1",
                    (provider_id, holder_kind, holder_id),
                )
            conn.executemany(
                "INSERT OR REPLACE INTO transactions (provider_id, holder_kind,"
                " holder_id, transaction_id, timestamp, amount, currency, description,"
                " category, merchant, is_pending, payload, synced_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        provider_id,
                        holder_kind,
                        holder_id,
                        tx.get("transaction_id")
                        or tx.get("normalised_provider_transaction_id")
                        or f"{tx.get('timestamp')}|{tx.get('amount')}|{tx.get('description')}",
                        tx.get("timestamp"),
                        tx.get("amount"),
                        tx.get("currency"),
                        tx.get("description"),
                        tx.get("transaction_category"),
                        (tx.get("merchant_name") or (tx.get("meta") or {}).get("merchant_name")),
                        1 if is_pending else 0,
                        json.dumps(tx),
                        synced_at,
                    )
                    for tx in rows
                ],
            )
        return synced_at

    def transactions(
        self,
        provider_id: str | None = None,
        holder_id: str | None = None,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        search: str | None = None,
        include_pending: bool = True,
        limit: int = 200,
    ) -> tuple[list[dict], datetime | None]:
        clauses, params = [], []
        if provider_id:
            clauses.append("provider_id = ?"); params.append(provider_id)
        if holder_id:
            clauses.append("holder_id = ?"); params.append(holder_id)
        if from_date:
            clauses.append("timestamp >= ?"); params.append(from_date)
        if to_date:
            # Inclusive of the whole end day when a bare date is given.
            clauses.append("timestamp <= ?")
            params.append(to_date if len(to_date) > 10 else to_date + "T23:59:59.999Z")
        if search:
            clauses.append("(description LIKE ? OR merchant LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if not include_pending:
            clauses.append("is_pending = 0")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT payload, is_pending, synced_at FROM transactions {where}"
                f" ORDER BY timestamp DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

        out, oldest = [], None
        for row in rows:
            tx = json.loads(row["payload"])
            tx["is_pending"] = bool(row["is_pending"])
            out.append(tx)
            ts = parse_ts(row["synced_at"])
            if ts and (oldest is None or ts < oldest):
                oldest = ts
        return out, oldest

    # -- sync bookkeeping --------------------------------------------------

    def start_run(self, provider_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sync_runs (provider_id, started_at, status)"
                " VALUES (?, ?, 'running')",
                (provider_id, _now()),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, detail: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = ?, status = ?, detail = ?"
                " WHERE id = ?",
                (_now(), status, detail, run_id),
            )

    def last_run(self, provider_id: str | None = None) -> dict | None:
        where, params = ("WHERE provider_id = ?", (provider_id,)) if provider_id else ("", ())
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT * FROM sync_runs {where} ORDER BY id DESC LIMIT 1", params
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        with self._conn() as conn:
            tx = conn.execute(
                "SELECT COUNT(*) n, MIN(timestamp) lo, MAX(timestamp) hi,"
                " SUM(is_pending) pending FROM transactions"
            ).fetchone()
            snaps = conn.execute(
                "SELECT kind, COUNT(*) n FROM snapshots GROUP BY kind"
            ).fetchall()
        return {
            "transactions": tx["n"],
            "pending": tx["pending"] or 0,
            "earliest": tx["lo"],
            "latest": tx["hi"],
            "snapshots": {r["kind"]: r["n"] for r in snaps},
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
