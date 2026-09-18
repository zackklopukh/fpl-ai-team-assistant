"""Check that the database matches `db/schema.sql`, and say so in English.

This is the first thing to run when credentials land, and the thing to run again
whenever something downstream is behaving as though a column is missing —
because usually it is. It connects, reports the server version, checks every
table and column the schema file declares, checks `pg_trgm` is installed, counts
the rows in each table and prints the last few `ingest_runs`.

The expected schema is parsed out of `db/schema.sql` rather than transcribed into
a list here. A hand-copied list rots the first time somebody adds a column, and
schema drift is the exact failure this script exists to catch — a checker with
its own stale copy of the truth is worse than no checker.

Drift is reported in both directions and they are not the same severity:

* A table or column in the schema file but not in the database is a **failure**.
  Something did not get applied, and an ingestion job is going to fail on it.
* A column in the database but not in the schema file is a **warning**. Usually
  it means someone added it by hand in the Supabase console and did not write it
  down, which is worth knowing about and is not a reason to exit non-zero.

Exit codes: 0 all good (warnings allowed), 1 a real problem, 2 could not connect.

    python ingest/verify_db.py
    python ingest/run.py verify
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

SCHEMA_PATH = config.REPO_ROOT / "db" / "schema.sql"

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_NO_CONNECTION = 2

# How many recent ingest_runs to show. Enough to see the overnight sequence.
RECENT_RUNS = 8

# Words that begin a table constraint rather than a column definition.
_CONSTRAINT_KEYWORDS = {
    "primary",
    "foreign",
    "unique",
    "check",
    "constraint",
    "exclude",
    "like",
}

_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)


# --- Parsing db/schema.sql -------------------------------------------------


def _strip_comments(sql: str) -> str:
    """Drop `-- ...` comments. The schema file is more comment than SQL."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _balanced_body(sql: str, open_paren: int) -> str:
    """The text between `sql[open_paren]` and its matching close paren.

    Written as a paren counter rather than a regex because `full_name` is a
    generated column whose definition contains its own parentheses and spans
    several lines; a non-greedy regex stops in the middle of it.
    """
    depth = 0
    for index in range(open_paren, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return sql[open_paren + 1 : index]
    raise ValueError("unbalanced parentheses in schema file")


def _split_top_level(body: str) -> list[str]:
    """Split a table body on commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []

    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1

        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)

    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parse_schema(sql: str) -> dict[str, list[str]]:
    """table name -> column names, in declaration order, from the schema file."""
    clean = _strip_comments(sql)
    tables: dict[str, list[str]] = {}

    for match in _CREATE_TABLE.finditer(clean):
        table = match.group(1)
        body = _balanced_body(clean, match.end() - 1)

        columns = []
        for part in _split_top_level(body):
            first = part.split()[0].lower().strip('"')
            if first in _CONSTRAINT_KEYWORDS:
                continue
            columns.append(first)

        tables[table] = columns

    return tables


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, list[str]]:
    return parse_schema(path.read_text())


# --- Comparison ------------------------------------------------------------


@dataclass
class Report:
    """Accumulated findings, printed as we go and summarised at the end."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def problem(self, message: str) -> None:
        self.problems.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def exit_code(self) -> int:
        return EXIT_PROBLEM if self.problems else EXIT_OK


def compare_columns(
    expected: Sequence[str], actual: Sequence[str]
) -> tuple[list[str], list[str]]:
    """(missing from the database, extra in the database) for one table."""
    expected_set, actual_set = set(expected), set(actual)
    return (
        sorted(expected_set - actual_set),
        sorted(actual_set - expected_set),
    )


# --- Output ----------------------------------------------------------------


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def line(status: str, text: str) -> None:
    print(f"  {status:<5} {text}")


OK = "ok"
FAIL = "FAIL"
WARN = "warn"


# --- Checks ----------------------------------------------------------------


def check_server(conn, report: Report) -> None:
    heading("Server")
    with conn.cursor() as cur:
        cur.execute("select version(), current_database(), current_schema()")
        version, database, schema = cur.fetchone()
    line(OK, version.split(" on ")[0])
    line(OK, f"database {database}, schema {schema}")


def check_extension(conn, report: Report, name: str = "pg_trgm") -> None:
    heading("Extensions")
    with conn.cursor() as cur:
        cur.execute("select extversion from pg_extension where extname = %s", (name,))
        row = cur.fetchone()

    if row:
        line(OK, f"{name} {row[0]}")
    else:
        line(FAIL, f"{name} is not installed — fuzzy name matching will not work")
        report.problem(
            f"{name} missing; run `create extension if not exists {name};`"
        )


def actual_tables(conn) -> dict[str, list[str]]:
    """table -> columns, for the current schema, straight from the catalogue."""
    with conn.cursor() as cur:
        cur.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = current_schema() "
            "order by table_name, ordinal_position"
        )
        rows = cur.fetchall()

    tables: dict[str, list[str]] = {}
    for table, column in rows:
        tables.setdefault(table, []).append(column)
    return tables


def check_tables(conn, expected: dict[str, list[str]], report: Report) -> list[str]:
    """Compare the schema file with the database. Returns the tables that exist."""
    heading("Tables")
    actual = actual_tables(conn)
    present: list[str] = []

    for table, columns in expected.items():
        if table not in actual:
            line(FAIL, f"{table} — missing; apply db/schema.sql")
            report.problem(f"table {table} does not exist")
            continue

        present.append(table)
        missing, extra = compare_columns(columns, actual[table])

        if not missing and not extra:
            line(OK, f"{table} ({len(columns)} columns)")
            continue

        if missing:
            line(FAIL, f"{table} — missing column(s): {', '.join(missing)}")
            report.problem(
                f"{table} is missing {len(missing)} column(s): {', '.join(missing)}"
            )
        if extra:
            line(WARN, f"{table} — column(s) not in db/schema.sql: {', '.join(extra)}")
            report.warn(
                f"{table} has {len(extra)} column(s) the schema file does not "
                f"declare: {', '.join(extra)}"
            )

    unexpected = sorted(set(actual) - set(expected))
    for table in unexpected:
        line(WARN, f"{table} — table not in db/schema.sql")
        report.warn(f"table {table} exists but is not declared in db/schema.sql")

    return present


def check_row_counts(conn, tables: Sequence[str], report: Report) -> None:
    heading("Row counts")
    if not tables:
        line(WARN, "no tables to count")
        return

    from psycopg import sql

    for table in tables:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("select count(*) from {}").format(sql.Identifier(table))
            )
            count = cur.fetchone()[0]
        # Empty is normal on a fresh database, so it is reported rather than
        # judged — the bring-up sequence in ingest/README.md is what fills them.
        line(OK if count else WARN, f"{table:<20} {count:>9,}")


def check_recent_runs(conn, tables: Sequence[str], report: Report) -> None:
    heading(f"Last {RECENT_RUNS} ingest runs")
    if "ingest_runs" not in tables:
        line(WARN, "ingest_runs does not exist yet")
        return

    with conn.cursor() as cur:
        cur.execute(
            "select job, season, started_at, finished_at, ok, rows_written, error "
            "from ingest_runs order by started_at desc limit %s",
            (RECENT_RUNS,),
        )
        rows = cur.fetchall()

    if not rows:
        line(WARN, "no ingestion has run yet")
        return

    for job, season, started, finished, ok, written, error in rows:
        when = started.strftime("%Y-%m-%d %H:%M")
        if ok is None:
            status, detail = WARN, "still running, or interrupted"
        elif ok:
            status, detail = OK, f"{written or 0} rows"
        else:
            status, detail = FAIL, (error or "failed, no error recorded")[:80]
        line(status, f"{when}  {job:<16} {season}  {detail}")
        if ok is False:
            report.warn(f"last {job} run at {when} failed: {detail}")


# --- Entry point -----------------------------------------------------------

# Values .env.example and the current .env ship with. None of them is a real
# credential; all of them produce an unhelpful driver error if handed to psycopg.
_PLACEHOLDERS = ("REPLACE_WITH_PASSWORD", "PASSWORD@", "PROJECT_REF")


def _placeholder_in(url: str) -> str | None:
    for placeholder in _PLACEHOLDERS:
        if placeholder in url:
            return placeholder.rstrip("@")
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the database against db/schema.sql.",
        epilog="Exit 0 all good, 1 a real problem, 2 could not connect.",
    )
    parser.add_argument("--season", default=config.SEASON)
    args = parser.parse_args(argv)

    if not config.DATABASE_URL:
        print("DATABASE_URL is not set.")
        print("Copy .env.example to .env and fill in the Supabase connection string:")
        print("  Supabase -> Project Settings -> Database -> Connection string -> URI")
        return EXIT_NO_CONNECTION

    placeholder = _placeholder_in(config.DATABASE_URL)
    if placeholder:
        # The state the repo ships in. Saying so beats a DNS error from a host
        # that was never meant to resolve.
        print(f"DATABASE_URL still contains the placeholder {placeholder!r}.")
        print("Edit .env and put the real Supabase password in before running this.")
        return EXIT_NO_CONNECTION

    try:
        expected = load_schema()
    except OSError as exc:
        print(f"Could not read {SCHEMA_PATH}: {exc}")
        return EXIT_PROBLEM

    print(f"Schema file     {SCHEMA_PATH}")
    print(f"Declares        {len(expected)} tables")
    print(f"Season          {args.season}")

    import psycopg

    report = Report()

    try:
        # A short timeout: a wrong host should say so in seconds rather than
        # hanging on a script somebody ran to find out why things are broken.
        with psycopg.connect(config.DATABASE_URL, connect_timeout=10) as conn:
            check_server(conn, report)
            check_extension(conn, report)
            tables = check_tables(conn, expected, report)
            check_row_counts(conn, tables, report)
            check_recent_runs(conn, tables, report)
    except psycopg.OperationalError as exc:
        # The common first-run failure. A stack trace here tells the reader
        # nothing they can act on, so print the driver's message and what to
        # check, and stop.
        print()
        print("Could not connect to the database.")
        print(f"  {str(exc).strip()}")
        print()
        print("Check that:")
        print("  * DATABASE_URL in .env has the real password, not a placeholder")
        print("  * the Supabase project has finished provisioning and is not paused")
        print("  * the host and port match Project Settings -> Database")
        return EXIT_NO_CONNECTION

    heading("Summary")
    for message in report.problems:
        line(FAIL, message)
    for message in report.warnings:
        line(WARN, message)

    if report.problems:
        print()
        print(
            f"  {len(report.problems)} problem(s). Apply db/schema.sql and run "
            "this again."
        )
    elif report.warnings:
        print(f"  Schema is fine. {len(report.warnings)} thing(s) worth a look.")
    else:
        print("  Everything checks out.")

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
