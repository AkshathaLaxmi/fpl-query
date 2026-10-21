"""The archive loader's row counts.

The build log's lesson is that matching counts are not the same as a correct
load -- but a count that does not match the table is worse, because it is the
first thing anyone checks. The archive repeats some (player, fixture) pairs, and
every repeat is an upsert onto a row the load already wrote. These pin that the
report counts the rows that exist, and reports the repeats on their own.

No database: a fake connection stands in, because what is under test is the
loader's bookkeeping, not Postgres's ON CONFLICT.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fplq.ingest.archive import ArchiveFile
from fplq.transform.loader import LoadReport, SeasonLoader


class _FakeCursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.log.append(sql)

    def fetchone(self) -> dict[str, Any]:
        return {"home_team_id": 1, "away_team_id": 2}

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed)


def _gw_row(element: int, fixture: int, gw: int) -> dict[str, str]:
    return {
        "element": str(element), "name": f"Player {element}", "position": "MID",
        "GW": str(gw), "fixture": str(fixture), "opponent_team": "2",
        "was_home": "True", "kickoff_time": "2020-06-17T19:15:00Z",
        "value": "60", "selected": "1000", "total_points": "2", "minutes": "90",
    }


def _stats_report(rows: list[dict[str, str]]) -> tuple[LoadReport, _FakeConn]:
    conn = _FakeConn()
    loader = SeasonLoader(conn)  # type: ignore[arg-type]
    file = ArchiveFile(season="2019-20", filename="gws/merged_gw.csv", rows=rows,
                       raw_uri="", content_sha256="", fetched_at=datetime.now(UTC),
                       source_uri="")
    report = LoadReport(season="2019-20")
    loader.load_gameweek_stats(
        season_id=1, season="2019-20", file=file,
        element_map={10: 100, 11: 101}, team_map={1: 1, 2: 2},
        fixture_map={275: 9275, 276: 9276}, gameweek_map={29: 129, 39: 139},
        deadlines={}, season_end=None, report=report,
    )
    return report, conn


def _stat_upserts(conn: _FakeConn) -> int:
    return sum("INSERT INTO core.player_fixture_stat" in sql for sql in conn.executed)


def test_repeated_player_fixture_counts_once() -> None:
    # 2019-20's shape: fixture 275 listed under its original gameweek (29) and
    # its rescheduled one (39), for the same player.
    report, _ = _stats_report([
        _gw_row(10, 275, 29),
        _gw_row(10, 275, 39),
        _gw_row(11, 275, 29),
        _gw_row(10, 276, 30),
    ])
    assert report.stats == 3
    assert report.stat_duplicates == 1
    assert "3 player-match rows (1 duplicate source rows)" in report.summary()


def test_duplicates_are_still_written() -> None:
    """Only the count changes: every source row still reaches the upsert."""
    rows = [_gw_row(10, 275, 29), _gw_row(10, 275, 39), _gw_row(10, 275, 39)]
    report, conn = _stats_report(rows)
    assert _stat_upserts(conn) == len(rows)
    assert (report.stats, report.stat_duplicates) == (1, 2)


def test_no_duplicates_reports_zero() -> None:
    report, _ = _stats_report([_gw_row(10, 275, 29), _gw_row(11, 276, 30)])
    assert (report.stats, report.stat_duplicates) == (2, 0)
