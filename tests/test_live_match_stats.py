"""Current-season player-match stats from the live API (FPL-29).

Two properties matter more than the rest:

  * Same parser, same row. An element-summary history row goes through the
    archive's own parse_gameweek_row, so a match loaded live and later from
    the archive produces the same values.
  * The handoff (sql/012_stat_source.sql). Live fills rows the archive does
    not have and refreshes its own; it never overwrites an archive row, and
    the archive overwrites live rows when it catches up.

The database tests run inside a rolled-back transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from fplq.config import settings
from fplq.db import connect
from fplq.ingest.archive import parse_gameweek_row
from fplq.transform.live import LiveLoader, MatchStatsReport, _as_archive_row
from fplq.transform.loader import _STAT_UPSERT, upsert_player_fixture_stat

SEASON = "2026-27"


def history_row(element: int, fixture: int, opponent: int, *, was_home: bool = True,
                bonus: int = 0, round_: int = 1) -> dict[str, Any]:
    """An element-summary history row, as the API returns it (JSON types)."""
    return {
        "element": element, "fixture": fixture, "opponent_team": opponent,
        "total_points": 6, "was_home": was_home, "kickoff_time": "2026-08-15T14:00:00Z",
        "team_h_score": 2, "team_a_score": 0, "round": round_, "modified": False,
        "minutes": 90, "goals_scored": 1, "assists": 0, "clean_sheets": 1,
        "goals_conceded": 0, "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
        "yellow_cards": 0, "red_cards": 0, "saves": 0, "bonus": bonus, "bps": 30,
        "influence": "40.2", "creativity": "12.0", "threat": "33.0", "ict_index": "8.5",
        "clearances_blocks_interceptions": 3, "recoveries": 4, "tackles": 2,
        "defensive_contribution": 9, "starts": 1, "expected_goals": "0.61",
        "expected_assists": "0.05", "expected_goal_involvements": "0.66",
        "expected_goals_conceded": "0.40", "value": 55, "transfers_balance": 10,
        "selected": 123456, "transfers_in": 2000, "transfers_out": 1990,
    }


# --- no database --------------------------------------------------------------


def test_an_api_row_parses_through_the_archive_parser() -> None:
    row = parse_gameweek_row(_as_archive_row(history_row(381, 62, 7, was_home=False, round_=7)))
    assert row["fpl_element_id"] == 381
    assert row["fpl_fixture_id"] == 62
    assert row["gameweek_number"] == 7, "the API calls it round; the archive calls it GW"
    assert row["was_home"] is False, "JSON false must not become the truthy string 'False'"
    assert row["price_at_deadline"] == 5.5, "value is in tenths of a million"
    assert row["kickoff_at"] == datetime(2026, 8, 15, 14, tzinfo=UTC)
    assert row["expected_goals"] == 0.61
    assert row["expected_points"] is None, "xP is archive-only"


def test_nulls_from_the_api_stay_null() -> None:
    raw = history_row(1, 2, 3)
    raw["expected_goals"] = None
    assert parse_gameweek_row(_as_archive_row(raw))["expected_goals"] is None


def test_only_the_live_upsert_defers_to_archive_rows() -> None:
    assert "WHERE core.player_fixture_stat.source = 'fpl_api'" in _STAT_UPSERT["fpl_api"]
    assert "WHERE" not in _STAT_UPSERT["fpl_archive"]
    for sql in _STAT_UPSERT.values():
        assert "source = EXCLUDED.source" in sql


# --- against the live database ------------------------------------------------


@pytest.fixture
def writer() -> psycopg.Connection:
    with connect(settings.writer_dsn) as conn:
        yield conn
        conn.rollback()


def _finished_match(conn: psycopg.Connection) -> dict[str, Any]:
    """A finished current-season fixture, one player who played in it, and
    the ids the API would use for both."""
    return conn.execute(
        """
        SELECT s.player_id, s.fixture_id, s.season_id, ps.fpl_element_id,
               f.fpl_fixture_id, ts.fpl_team_id AS opponent_fpl_team_id, s.was_home
        FROM core.player_fixture_stat s
        JOIN core.season se ON se.season_id = s.season_id AND se.name = %s
        JOIN core.fixture f ON f.fixture_id = s.fixture_id AND f.is_finished
        JOIN core.player_season ps ON ps.player_id = s.player_id AND ps.season_id = s.season_id
        JOIN core.team_season ts ON ts.team_id = s.opponent_team_id AND ts.season_id = s.season_id
        WHERE ps.fpl_element_id IS NOT NULL AND s.minutes > 0
        ORDER BY s.fixture_id, s.player_id
        LIMIT 1
        """,
        (SEASON,),
    ).fetchone()


def _stat(conn: psycopg.Connection, m: dict[str, Any]) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT source, bonus, bps FROM core.player_fixture_stat "
        "WHERE player_id=%s AND fixture_id=%s", (m["player_id"], m["fixture_id"]),
    ).fetchone()


def _apply(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> MatchStatsReport:
    season_id = conn.execute("SELECT season_id FROM core.season WHERE name=%s",
                             (SEASON,)).fetchone()["season_id"]
    report = MatchStatsReport(season=SEASON)
    LiveLoader(conn)._apply_match_stats(season_id, SEASON, rows, report)
    return report


def _row_for(m: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return history_row(m["fpl_element_id"], m["fpl_fixture_id"], m["opponent_fpl_team_id"],
                       was_home=m["was_home"], **kw)


@pytest.mark.database
def test_live_fills_a_missing_row_and_refreshes_its_own(writer: psycopg.Connection) -> None:
    m = _finished_match(writer)
    writer.execute("DELETE FROM core.player_fixture_stat WHERE player_id=%s AND fixture_id=%s",
                   (m["player_id"], m["fixture_id"]))

    report = _apply(writer, [_row_for(m, bonus=0)])
    assert report.rows_written == 1
    assert _stat(writer, m)["source"] == "fpl_api"

    # FPL confirms bonus points after the match; the next run must pick that up.
    _apply(writer, [_row_for(m, bonus=3)])
    assert _stat(writer, m)["bonus"] == 3


@pytest.mark.database
def test_live_never_overwrites_an_archive_row(writer: psycopg.Connection) -> None:
    m = _finished_match(writer)
    writer.execute("UPDATE core.player_fixture_stat SET source='fpl_archive', bps=-1 "
                   "WHERE player_id=%s AND fixture_id=%s", (m["player_id"], m["fixture_id"]))

    report = _apply(writer, [_row_for(m, bonus=3)])
    assert (report.rows_written, report.rows_archive_owned) == (0, 1)
    after = _stat(writer, m)
    assert (after["source"], after["bps"]) == ("fpl_archive", -1), "the archive row is untouched"


@pytest.mark.database
def test_the_archive_overwrites_a_live_row_when_it_catches_up(writer: psycopg.Connection) -> None:
    m = _finished_match(writer)
    writer.execute("UPDATE core.player_fixture_stat SET source='fpl_api', bps=-1 "
                   "WHERE player_id=%s AND fixture_id=%s", (m["player_id"], m["fixture_id"]))

    row = parse_gameweek_row(_as_archive_row(_row_for(m)))
    with writer.cursor() as cur:
        assert upsert_player_fixture_stat(
            cur, row, player_id=m["player_id"], season_id=m["season_id"], gameweek_id=None,
            fixture_id=m["fixture_id"], team_id=None, opponent_team_id=None,
            position_id=None, source="fpl_archive",
        )
    assert _stat(writer, m)["source"] == "fpl_archive"
    assert _stat(writer, m)["bps"] == 30


@pytest.mark.database
def test_an_unknown_element_is_recorded_not_guessed(writer: psycopg.Connection) -> None:
    m = _finished_match(writer)
    report = _apply(writer, [history_row(999_999, m["fpl_fixture_id"], 1)])
    assert report.rows_written == 0
    assert report.unmatched == [f"element=999999 fixture={m['fpl_fixture_id']}"]
