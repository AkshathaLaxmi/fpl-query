"""The archive loader's bookkeeping: row counts, the batch ledger, and the
price-history handoff to live ingestion.

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

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fplq.ingest.archive import ArchiveFile
from fplq.transform.loader import LoadReport, SeasonLoader

# fixture_id -> gameweek_id, as core.fixture holds them: fixture 275 was
# postponed out of gameweek 29 and played in gameweek 39.
_FIXTURE_GAMEWEEKS = {9275: 139, 9276: 130}


class _FakeCursor:
    def __init__(self, log: list[tuple[str, object]],
                 live_from: dict[int, datetime]) -> None:
        self.log = log
        self.live_from = live_from
        self._last = ""

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.log.append((sql, params))
        self._last = sql

    def fetchone(self) -> dict[str, Any]:
        return {"home_team_id": 1, "away_team_id": 2, "season_id": 1, "batch_id": 7}

    def fetchall(self) -> list[dict[str, Any]]:
        if "FROM core.fixture" in self._last:
            return [{"fixture_id": f, "gameweek_id": g} for f, g in _FIXTURE_GAMEWEEKS.items()]
        if "source <> 'fpl_archive'" in self._last:
            return [{"player_id": p, "live_from": t} for p, t in self.live_from.items()]
        return []


class _FakeConn:
    def __init__(self, live_from: dict[int, datetime] | None = None) -> None:
        self.executed: list[tuple[str, object]] = []
        self.live_from = live_from or {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed, self.live_from)

    def commit(self) -> None:
        self.executed.append(("COMMIT", None))

    def rollback(self) -> None:
        self.executed.append(("ROLLBACK", None))


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


# --- the batch ledger --------------------------------------------------------


def test_a_failed_load_keeps_its_batch_row() -> None:
    """The batch row is committed before the work, so the rollback of a failed
    season cannot take it along -- and marking it failed has a row to update."""
    conn = _FakeConn()
    with pytest.raises(KeyError):
        SeasonLoader(conn).load_season("2019-20", {})  # type: ignore[arg-type]
    steps = [sql.split()[0] if sql not in ("COMMIT", "ROLLBACK") else sql
             for sql, _ in conn.executed]
    opened = next(i for i, (sql, _) in enumerate(conn.executed)
                  if "INSERT INTO raw.ingest_batch" in sql)
    assert steps[opened + 1] == "COMMIT"
    assert steps.index("ROLLBACK") > opened + 1
    assert any("status=%s" in sql and params[0] == "failed"  # type: ignore[index]
               for sql, params in conn.executed if params)


# --- the price handoff -------------------------------------------------------

_GW1 = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
_GW2 = _GW1 + timedelta(days=7)
_GW3 = _GW2 + timedelta(days=7)


def _rebuilt_intervals(live_from: dict[int, datetime]) -> list[tuple[Any, ...]]:
    conn = _FakeConn(live_from)
    SeasonLoader(conn)._build_price_history(  # type: ignore[arg-type]
        season_id=1, prices={100: [(1, 7.0), (2, 7.0), (3, 7.1)]},
        deadlines={1: _GW1, 2: _GW2, 3: _GW3}, season_end=None,
        report=LoadReport(season="2026-27"),
    )
    return [params for sql, params in conn.executed  # type: ignore[misc]
            if "INSERT INTO core.player_price_history" in sql]


def test_without_live_data_the_current_season_stays_open() -> None:
    intervals = _rebuilt_intervals({})
    assert [(p[2], p[3], p[4]) for p in intervals] == [(7.0, _GW1, _GW3), (7.1, _GW3, None)]


def test_a_reload_after_live_ingest_hands_off_at_the_live_start() -> None:
    """Live took over between GW1 and GW3: the archive closes there, and the
    GW3 interval -- which live already owns -- is not rebuilt at all."""
    live = _GW2 + timedelta(hours=5)
    intervals = _rebuilt_intervals({100: live})
    assert [(p[2], p[3], p[4]) for p in intervals] == [(7.0, _GW1, live)]


def test_live_data_for_another_player_does_not_clip_this_one() -> None:
    intervals = _rebuilt_intervals({200: _GW1})
    assert [(p[2], p[3], p[4]) for p in intervals] == [(7.0, _GW1, _GW3), (7.1, _GW3, None)]


def test_archive_ownership_is_rebuilt_not_only_upserted() -> None:
    """Live ingestion corrects a derived deadline; an upsert-only reload then
    left the old snapshot at the stale instant beside the new one."""
    conn = _FakeConn()
    SeasonLoader(conn)._build_ownership(  # type: ignore[arg-type]
        season_id=1, ownership={100: [(1, 5000, None, None)]},
        deadlines={1: _GW1}, gameweek_map={1: 101}, report=LoadReport(season="2026-27"),
    )
    sqls = [sql for sql, _ in conn.executed]
    delete = next(i for i, sql in enumerate(sqls)
                  if "DELETE FROM core.player_ownership_snapshot" in sql)
    insert = next(i for i, sql in enumerate(sqls)
                  if "INSERT INTO core.player_ownership_snapshot" in sql)
    assert delete < insert
    # Only the archive's gameweek-grained rows: live snapshots have no gameweek.
    assert "gameweek_id IS NOT NULL" in sqls[delete]
