"""Client for the official FPL API.

The API is public, unauthenticated, undocumented and unlicensed. That imposes
rules this module enforces rather than merely documents:

  * We never proxy it live. Every user-facing query is served from our own
    Postgres. The API is touched on a schedule, by us, and by nobody else.
  * We identify ourselves in the User-Agent and we rate-limit ourselves.
  * Responses land in the raw store verbatim before anything parses them.

See docs/data-sources.md for the attribution and non-commercial position.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from fplq.config import Settings
from fplq.config import settings as default_settings
from fplq.ingest.store import RawStore, open_store, sha256

log = logging.getLogger(__name__)

NAMESPACE = "fpl_api"


@dataclass
class FetchResult:
    endpoint: str
    payload: Any
    raw_uri: str
    content_sha256: str
    fetched_at: datetime


class FplApiClient:
    """Polite, retrying, snapshotting client. One instance per ingestion run."""

    def __init__(self, settings: Settings = default_settings,
                 store: RawStore | None = None,
                 client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.store = store or open_store(settings.raw_store_uri)
        self._client = client or httpx.Client(
            base_url=settings.fpl_api_base,
            timeout=settings.request_timeout_s,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._last_request_at = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FplApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.settings.request_delay_s:
            time.sleep(self.settings.request_delay_s - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, *, attempts: int = 4) -> bytes:
        last: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                response = self._client.get(path)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} from {path}",
                        request=response.request, response=response,
                    )
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last = exc
                backoff = 2.0 ** attempt
                log.warning("GET %s failed (%s); retrying in %.0fs", path, exc, backoff)
                time.sleep(backoff)
        raise RuntimeError(f"GET {path} failed after {attempts} attempts") from last

    def fetch(self, endpoint: str, *, name: str | None = None) -> FetchResult:
        """Fetch an endpoint, land it in the raw store, return the parsed payload."""
        body = self._get(endpoint)
        fetched_at = datetime.now(UTC)
        uri = self.store.put_snapshot(NAMESPACE, name or endpoint, body,
                                      suffix="json", when=fetched_at)
        return FetchResult(
            endpoint=endpoint,
            payload=json.loads(body),
            raw_uri=uri,
            content_sha256=sha256(body),
            fetched_at=fetched_at,
        )

    # -- endpoints ---------------------------------------------------------

    def bootstrap_static(self) -> FetchResult:
        """Players, teams, positions, gameweeks. The daily snapshot that drives
        price and ownership history."""
        return self.fetch("bootstrap-static/", name="bootstrap-static")

    def fixtures(self, event: int | None = None) -> FetchResult:
        path = f"fixtures/?event={event}" if event else "fixtures/"
        return self.fetch(path, name=f"fixtures-gw{event}" if event else "fixtures")

    def event_live(self, event: int) -> FetchResult:
        """Per-player stats for a gameweek. Ingested after each match day."""
        return self.fetch(f"event/{event}/live/", name=f"event-live-gw{event}")

    def element_summary(self, element_id: int) -> FetchResult:
        """A single player's full history, including past seasons."""
        return self.fetch(f"element-summary/{element_id}/",
                          name=f"element-summary-{element_id}")
