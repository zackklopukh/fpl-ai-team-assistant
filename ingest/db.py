"""Postgres access for the ingestion scripts.

Plain SQL on purpose — there is no ORM in this project and the schema is small
enough that one is pure overhead.

Every write goes through `upsert`, which writes on the natural key and updates on
conflict. That makes a re-run after a half-failed job safe: you never have to
reason about partial state, you just run it again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql

from config import require_database_url

log = logging.getLogger(__name__)


@contextmanager
def connect() -> Iterable[psycopg.Connection]:
    """A connection that commits on success and rolls back on any exception."""
    with psycopg.connect(require_database_url()) as conn:
        yield conn


# Which tables carry an `updated_at` column, looked up once per process.
_updated_at_cache: dict[str, bool] = {}


def _has_updated_at(conn: psycopg.Connection, table: str) -> bool:
    if table not in _updated_at_cache:
        with conn.cursor() as cur:
            cur.execute(
                "select 1 from information_schema.columns "
                "where table_schema = current_schema() "
                "and table_name = %s and column_name = 'updated_at'",
                (table,),
            )
            _updated_at_cache[table] = cur.fetchone() is not None
    return _updated_at_cache[table]


def upsert(
    conn: psycopg.Connection,
    table: str,
    rows: Sequence[dict[str, Any]],
    conflict_keys: Sequence[str],
    update_columns: Sequence[str] | None = None,
) -> int:
    """Insert rows, updating the ones that already exist.

    `update_columns` defaults to every column that is not part of the key. Pass it
    explicitly when a column should be written once and then left alone.

    An `updated_at` column is refreshed automatically on update. Its `default now()`
    only fires on insert, so without this every re-run would leave the timestamp
    reporting when the row first appeared rather than when it was last confirmed —
    and that timestamp is what recommendations are stamped with.

    Returns the number of rows written.
    """
    if not rows:
        return 0

    columns = list(rows[0].keys())

    for row in rows:
        if list(row.keys()) != columns:
            raise ValueError(
                f"every row must have the same columns in the same order; "
                f"expected {columns}, got {list(row.keys())}"
            )

    if update_columns is None:
        update_columns = [c for c in columns if c not in conflict_keys]

    assignments = [
        sql.SQL("{col} = excluded.{col}").format(col=sql.Identifier(c))
        for c in update_columns
    ]

    if "updated_at" not in columns and _has_updated_at(conn, table):
        assignments.append(sql.SQL("updated_at = now()"))

    statement = sql.SQL(
        "insert into {table} ({columns}) values ({placeholders}) "
        "on conflict ({conflict}) do update set {assignments}"
    ).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        placeholders=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
        conflict=sql.SQL(", ").join(sql.Identifier(c) for c in conflict_keys),
        assignments=sql.SQL(", ").join(assignments),
    )

    if not update_columns:
        statement = sql.SQL(
            "insert into {table} ({columns}) values ({placeholders}) "
            "on conflict ({conflict}) do nothing"
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            placeholders=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
            conflict=sql.SQL(", ").join(sql.Identifier(c) for c in conflict_keys),
        )

    with conn.cursor() as cur:
        cur.executemany(statement, rows)

    return len(rows)


@contextmanager
def run_logged(conn: psycopg.Connection, job: str, season: str):
    """Record a job in `ingest_runs` so a failed overnight sync is visible.

    Yields a mutable dict; set `result["rows"]` to the row count you wrote.
    """
    result: dict[str, Any] = {"rows": 0}

    with conn.cursor() as cur:
        cur.execute(
            "insert into ingest_runs (job, season) values (%s, %s) returning id",
            (job, season),
        )
        run_id = cur.fetchone()[0]
    conn.commit()

    try:
        yield result
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "update ingest_runs set finished_at = now(), ok = false, error = %s "
                "where id = %s",
                (str(exc)[:2000], run_id),
            )
        conn.commit()
        raise
    else:
        with conn.cursor() as cur:
            cur.execute(
                "update ingest_runs set finished_at = now(), ok = true, rows_written = %s "
                "where id = %s",
                (result["rows"], run_id),
            )
        conn.commit()
        log.info("%s: wrote %d rows", job, result["rows"])
