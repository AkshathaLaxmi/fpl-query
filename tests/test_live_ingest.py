"""Live ingestion, end to end.

Needs both a live database (a season to refresh must already exist) and a real
call to the FPL API, so it is marked with both and runs in neither the fast
suite nor a plain `pytest -m database`. It is the one place that proves the
claim in transform/live.py's docstring: that the price-history handoff between
an archive-loaded open interval and a live one does not trip the exclusion
constraint, and that running twice does not duplicate anything.

Run with `pytest -m "database and network"`.
"""

from __future__ import annotations

import psycopg
import pytest

from fplq.config import CURRENT_SEASON, settings
from fplq.db import connect
from fplq.transform.live import ingest_live, replay_batch

pytestmark = [pytest.mark.database, pytest.mark.network]


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _reachable(settings.writer_dsn),
    reason="no database reachable; run `fplq bootstrap && fplq load` first",
)


def _season_exists(season: str) -> bool:
    with connect(settings.writer_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.season WHERE name=%s", (season,))
        return cur.fetchone() is not None


requires_season = pytest.mark.skipif(
    not _reachable(settings.writer_dsn) or not _season_exists(CURRENT_SEASON),
    reason=f"{CURRENT_SEASON} is not loaded; run `fplq load --seasons {CURRENT_SEASON}` first",
)


def _no_overlaps(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) AS n FROM core.player_price_history a
            JOIN core.player_price_history b
              ON a.player_id = b.player_id
             AND a.price_history_id < b.price_history_id
             AND tstzrange(a.valid_from, a.valid_to) && tstzrange(b.valid_from, b.valid_to)
        """)
        return cur.fetchone()["n"]


@requires_db
@requires_season
def test_ingest_writes_rows_and_keeps_price_history_non_overlapping() -> None:
    with connect(settings.writer_dsn) as conn:
        report = ingest_live(conn, CURRENT_SEASON)
        assert report.rows_written > 0
        assert report.batch_id is not None
        assert _no_overlaps(conn) == 0


@requires_db
@requires_season
def test_ingesting_twice_does_not_duplicate_ownership_or_price(
) -> None:
    with connect(settings.writer_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM core.player_ownership_snapshot")
        before = cur.fetchone()["n"]

    with connect(settings.writer_dsn) as conn:
        first = ingest_live(conn, CURRENT_SEASON)
        second = ingest_live(conn, CURRENT_SEASON)

    # Same day, same prices almost always: the second run should see no price
    # changes even though the first one (which may have just taken over from
    # an archive-loaded open interval) could have seen several.
    assert second.price_changes == 0

    with connect(settings.writer_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM core.player_ownership_snapshot")
        after = cur.fetchone()["n"]
    # Each run is its own point-in-time snapshot, so two runs add two rounds
    # of rows, not zero -- ownership is a snapshot, not an interval.
    assert after == before + first.ownership_snapshots + second.ownership_snapshots


@requires_db
@requires_season
def test_replay_reproduces_the_same_batch_without_a_second_fetch() -> None:
    with connect(settings.writer_dsn) as conn:
        report = ingest_live(conn, CURRENT_SEASON)
        assert report.batch_id is not None

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM core.player_ownership_snapshot")
            before = cur.fetchone()["n"]

        replay_batch(conn, report.batch_id)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM core.player_ownership_snapshot")
            after = cur.fetchone()["n"]

    # Replaying the batch that just ran hits the same snapshot_at it already
    # wrote, so it updates those rows rather than adding new ones.
    assert after == before
