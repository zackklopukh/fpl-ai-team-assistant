"""Work out the right Supabase connection string, once.

Two things routinely go wrong when wiring this up, and neither announces itself
clearly:

1. **The direct host may not resolve.** New Supabase projects get an IPv6-only
   direct connection (`db.<ref>.supabase.co`) plus an IPv4 pooler. On a network
   with no IPv6 route the direct host fails in a way that reads like a firewall
   problem, and Vercel's build environment hits the same wall.
2. **The password is not URL-safe.** A password containing `#`, `@`, `:`, `/` or
   `?` silently truncates the connection string — `#` in particular starts a URL
   fragment, so everything after it is discarded and the error names the wrong
   thing entirely.

This script reads the password from .env, percent-encodes it, and tries each
pooler region until one authenticates. It prints the working string with the
password masked; pass --write to put it in .env.

    python ingest/find_pooler.py
    python ingest/find_pooler.py --write

The password is never printed or logged.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# Supabase's pooler fronts each AWS region behind a stable hostname. Every one of
# them resolves, so DNS cannot tell you which holds your project — only an
# authentication attempt can.
REGIONS = [
    "us-east-1",
    "us-west-1",
    "us-east-2",
    "us-west-2",
    "ca-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-central-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-south-1",
    "sa-east-1",
]

# 5432 is the session pooler: a real Postgres session, which is what migrations
# and long-lived scripts need. 6543 is the transaction pooler, which is faster
# for serverless but does not support prepared statements or session state.
SESSION_POOLER_PORT = 5432


def read_env_value(key: str) -> str | None:
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        if line.strip().startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            return value.strip("\"'")
    return None


def parse_credentials(url: str) -> tuple[str, str]:
    """Pull (project_ref, password) out of whatever form the URL is in.

    Deliberately hand-rolled rather than urlparse: the whole point is that this
    URL may be malformed in exactly the way urlparse cannot survive.
    """
    body = url.split("://", 1)[-1]
    creds, _, host_part = body.rpartition("@")
    _, _, password = creds.partition(":")

    match = re.search(r"db\.([a-z0-9]+)\.supabase\.co", url) or re.search(
        r"postgres\.([a-z0-9]+)", url
    )
    if not match:
        raise SystemExit(
            "Could not find the project ref in DATABASE_URL. It should contain "
            "either db.<ref>.supabase.co or postgres.<ref>"
        )
    return match.group(1), password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="rewrite DATABASE_URL in .env"
    )
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args()

    raw = read_env_value("DATABASE_URL")
    if not raw:
        raise SystemExit("DATABASE_URL is not set in .env")
    if "REPLACE_WITH" in raw:
        raise SystemExit("DATABASE_URL still holds the placeholder password")

    ref, password = parse_credentials(raw)

    # Decode before encoding, so this is idempotent. Someone who already escaped
    # their own `#` as `%23` would otherwise get `%2523` — a different password,
    # and an authentication failure that blames the wrong thing entirely.
    safe = quote(unquote(password), safe="")

    if safe != password:
        raw_chars = sorted({c for c in unquote(password) if quote(c, safe="") != c})
        if raw_chars:
            print(f"Password contains {raw_chars} — percent-encoding it.\n")
        else:
            print("Password was already percent-encoded — leaving it as is.\n")

    print(f"Project ref: {ref}")
    print(f"Trying {len(REGIONS)} pooler regions (session pooler, port {SESSION_POOLER_PORT})\n")

    for region in REGIONS:
        host = f"aws-0-{region}.pooler.supabase.com"
        url = (
            f"postgresql://postgres.{ref}:{safe}@{host}"
            f":{SESSION_POOLER_PORT}/postgres?sslmode=require"
        )
        try:
            with psycopg.connect(url, connect_timeout=args.timeout) as conn:
                with conn.cursor() as cur:
                    cur.execute("select version()")
                    version = cur.fetchone()[0]
        except psycopg.OperationalError as exc:
            reason = str(exc).strip().splitlines()[0][:70]
            print(f"  {region:<16} no   ({reason})")
            continue

        print(f"\n  {region:<16} CONNECTED")
        print(f"  {version.split(',')[0]}\n")

        masked = url.replace(safe, "<password>")
        print("DATABASE_URL=" + masked)

        if args.write:
            text = ENV_PATH.read_text()
            text = re.sub(
                r"^DATABASE_URL=.*$", f"DATABASE_URL={url}", text, count=1, flags=re.M
            )
            ENV_PATH.write_text(text)
            print("\nWritten to .env. Next: python ingest/verify_db.py")
        else:
            print("\nRe-run with --write to save it to .env.")
        return 0

    print(
        "\nNo region accepted the credentials. Either the password is wrong, or the "
        "project is still provisioning. Check Project Settings -> Database in the "
        "Supabase dashboard."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
