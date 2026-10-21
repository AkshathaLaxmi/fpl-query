"""The archive loader's row counts.

The build log's lesson is that matching counts are not the same as a correct
load -- but a count that does not match the table is worse, because it is the
first thing anyone checks. The archive repeats some (player, fixture) pairs, and
in 2019-20 a postponed fixture appears under both its original and its
rescheduled gameweek. These pin that one row per key is written -- the right
one -- and that the report counts the rows that exist, with repeats on their own.

No database: a fake connection stands in, because what is under test is the
loader's bookkeeping, not Postgres's ON CONFLICT.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fplq.ingest.archive import ArchiveFile
from fplq.transform.loader import LoadReport, SeasonLoader

# fixture_id -> gameweek_id, as core.fixture holds them: fixture 275 was
# postponed out of gameweek 29 and played in gameweek 39.
_FIXTURE_GAMEWEEKS = {9275: 139, 9276: 130}


class _FakeCursor:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self.log = log
        self._last = ""

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.log.append((sql, params))
        self._last = sql

    def fetchone(self) -> dict[str, Any]:
        return {"home_team_id": 1, "away_team_id": 2}

    def fetchall(self) -> list[dict[str, Any]]:
        if "FROM core.fixture" in self._last:
            return [{"fixture_id": f, "gameweek_id": g} for f, g in _FIXTURE_GAMEWEEKS.items()]
        return []


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed)


def _gw_row(element: int, fixture: int, gw: int, *,
            points: int = 2, goals: int = 0) -> dict[str, str]:
    return {
        "element": str(element), "name": f"Player {element}", "position": "MID",
        "GW": str(gw), "fixture": str(fixture), "opponent_team": "2",
        "was_home": "True", "kickoff_time": "2020-06-17T19:15:00Z",
        "value": "60", "selected": "1000", "total_points": str(points),
        "minutes": "90", "goals_scored": str(goals),
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
        fixture_map={275: 9275, 276: 9276}, gameweek_map={29: 129, 30: 130, 39: 139},
        deadlines={}, season_end=None, report=report,
    )
    return report, conn


def _stat_upserts(conn: _FakeConn) -> list[tuple[Any, ...]]:
    return [params for sql, params in conn.executed  # type: ignore[misc]
            if "INSERT INTO core.player_fixture_stat" in sql]


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


def test_each_key_is_written_once() -> None:
    rows = [_gw_row(10, 276, 30), _gw_row(10, 276, 30), _gw_row(10, 276, 30)]
    report, conn = _stats_report(rows)
    assert len(_stat_upserts(conn)) == 1
    assert (report.stats, report.stat_duplicates) == (1, 2)


def test_postponed_fixture_keeps_the_rescheduled_row() -> None:
    """The 2019-20 fixture 275 case: the blank GW29 placeholder comes first.

    Writing it and letting the real GW39 row patch a few columns over it gave
    rows with real points, no goals and the wrong gameweek. The row in the
    fixture's own gameweek must be the one written, whichever comes first.
    """
    placeholder = _gw_row(10, 275, 29, points=0)
    real = _gw_row(10, 275, 39, points=8, goals=1)
    for rows in ([placeholder, real], [real, placeholder]):
        _, conn = _stats_report(rows)
        (params,) = _stat_upserts(conn)
        gameweek_id, total_points, goals_scored = params[2], params[11], params[12]
        assert (gameweek_id, total_points, goals_scored) == (139, 8, 1)


def test_reload_replaces_every_column() -> None:
    """A partial DO UPDATE lets a hybrid row survive every later reload."""
    _, conn = _stats_report([_gw_row(10, 276, 30)])
    (sql,) = [sql for sql, _ in conn.executed if "INSERT INTO core.player_fixture_stat" in sql]
    inserted = sql.split("INSERT INTO core.player_fixture_stat (")[1].split(")")[0]
    columns = [c.strip() for c in inserted.split(",")]
    updated = sql.split("DO UPDATE SET")[1]
    missing = [c for c in columns if c not in ("player_id", "fixture_id")
               and f"EXCLUDED.{c}" not in updated]
    assert missing == []


def test_no_duplicates_reports_zero() -> None:
    report, _ = _stats_report([_gw_row(10, 275, 29), _gw_row(11, 276, 30)])
    assert (report.stats, report.stat_duplicates) == (2, 0)
