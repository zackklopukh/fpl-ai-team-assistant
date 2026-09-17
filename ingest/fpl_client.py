"""The only place in the repo that talks to the FPL API.

Two rules this module exists to enforce:

* Requests are serialised with a short sleep between them. A cron job is under no
  deadline pressure, and the API has no documented rate limit — which means the
  limit is whatever Cloudflare decides today.
* Nothing here is ever called from a request a user is waiting on. Cron writes to
  Postgres; the app reads Postgres. The two per-manager endpoints are the single
  exception (see `entry` and `entry_transfers`), and they must be cached and
  rate-limited by their caller.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from config import FPL_BASE_URL, USER_AGENT

log = logging.getLogger(__name__)

# Seconds between requests. Deliberately unhurried.
REQUEST_INTERVAL = 0.6

# 429 and 5xx are worth retrying; a 404 means the team or player does not exist.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class FPLClient:
    """A serialised, retrying client for the public FPL endpoints."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=FPL_BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._last_request_at = 0.0

    def __enter__(self) -> "FPLClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < REQUEST_INTERVAL:
                time.sleep(REQUEST_INTERVAL - elapsed)

            try:
                response = self._client.get(path)
                self._last_request_at = time.monotonic()

                if response.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} from {path}",
                        request=response.request,
                        response=response,
                    )

                response.raise_for_status()
                return response.json()

            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in RETRY_STATUS:
                    raise

                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break

                # Back off geometrically. A 403 here usually means the shared
                # GitHub Actions IP range is blocked, and retrying harder is the
                # wrong fix — run ingestion from somewhere else.
                backoff = REQUEST_INTERVAL * (3**attempt)
                log.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    path,
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    backoff,
                )
                time.sleep(backoff)

        raise RuntimeError(f"GET {path} failed after {MAX_ATTEMPTS} attempts") from last_error

    # --- Public endpoints, safe for cron -----------------------------------

    def bootstrap_static(self) -> dict[str, Any]:
        """All players, teams and gameweeks. The backbone of every sync."""
        return self._get("/bootstrap-static/")

    def fixtures(self, event: int | None = None) -> list[dict[str, Any]]:
        """Every fixture, or one gameweek's. A team may appear twice, or not at all."""
        path = "/fixtures/" if event is None else f"/fixtures/?event={event}"
        return self._get(path)

    def element_summary(self, element_id: int) -> dict[str, Any]:
        """One player's per-gameweek history plus upcoming fixtures."""
        return self._get(f"/element-summary/{element_id}/")

    def event_live(self, gw: int) -> dict[str, Any]:
        """Live per-player stats for a gameweek. Bonus is provisional until settled."""
        return self._get(f"/event/{gw}/live/")

    # --- Per-manager endpoints ---------------------------------------------
    # The exception to the no-live-calls rule, because they are user-specific.
    # Cache each response for at least an hour and rate-limit per team id.

    def entry(self, team_id: int) -> dict[str, Any]:
        """A manager's summary: name, overall points, last deadline bank and value."""
        return self._get(f"/entry/{team_id}/")

    def entry_transfers(self, team_id: int) -> list[dict[str, Any]]:
        """Full transfer log, each row carrying the prices actually paid, in tenths."""
        return self._get(f"/entry/{team_id}/transfers/")

    def entry_history(self, team_id: int) -> dict[str, Any]:
        """Per-gameweek points, rank, squad value and bank."""
        return self._get(f"/entry/{team_id}/history/")

    def entry_picks(self, team_id: int, gw: int) -> dict[str, Any]:
        """A manager's squad for a gameweek. Private until that deadline passes."""
        return self._get(f"/entry/{team_id}/event/{gw}/picks/")
