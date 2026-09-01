"""Archive row parsing.

Every case here is drawn from the real files: the schema genuinely drifts
between seasons, and the archive genuinely writes the string "None" into
columns that a person would expect to be empty.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fplq.ingest.archive import (
    parse_fixture_row,
    parse_gameweek_row,
    parse_player_row,
    parse_team_row,
    start_price_from,
)

# --- the null sentinels -----------------------------------------------------


@pytest.mark.parametrize("sentinel", ["", "None", "none", "nan", "NULL", "N/A", "-"])
def test_string_nulls_become_none_not_text(sentinel: str) -> None:
    """The archive str()s its way to CSV, so missing values arrive as words.

    This cost a load: a birth_date of "None" was sent to Postgres as text and
    rejected the row.
    """
    row = parse_player_row({"code": "1", "id": "1", "birth_date": sentinel,
                            "first_name": "A", "second_name": "B"})
    assert row["birth_date"] is None


def test_string_nulls_do_not_become_zero() -> None:
    """A missing stat is unknown, not zero -- averages depend on the difference."""
    row = parse_gameweek_row({"element": "1", "GW": "1", "expected_goals": "None"})
    assert row["expected_goals"] is None


# --- price arithmetic -------------------------------------------------------


def test_price_is_converted_from_tenths() -> None:
    """now_cost is in tenths of a million: 125 -> 12.5, not 125."""
    row = parse_player_row({"code": "1", "id": "1", "now_cost": "125"})
    assert row["end_price"] == 12.5


def test_start_price_is_reconstructed_from_cumulative_change() -> None:
    """A player who ended at 13.8 having risen 1.3 started at 12.5."""
    parsed = {"end_price": 13.8, "cost_change_start": 13}
    assert start_price_from(parsed) == 12.5


def test_start_price_handles_a_fall() -> None:
    parsed = {"end_price": 4.2, "cost_change_start": -3}
    assert start_price_from(parsed) == 4.5


def test_start_price_falls_back_when_change_is_missing() -> None:
    assert start_price_from({"end_price": 7.0, "cost_change_start": None}) == 7.0


def test_gameweek_price_is_converted_from_tenths() -> None:
    row = parse_gameweek_row({"element": "1", "GW": "3", "value": "71"})
    assert row["price_at_deadline"] == 7.1


# --- schema drift across seasons --------------------------------------------


def test_2019_20_shape_parses_without_the_later_columns() -> None:
    """2019-20 merged_gw has no position, team, xP or expected-goals columns."""
    row = parse_gameweek_row({
        "name": "Mohamed_Salah_191", "element": "191", "GW": "1",
        "fixture": "5", "opponent_team": "12", "was_home": "True",
        "total_points": "8", "minutes": "90", "value": "125", "selected": "3000000",
    })
    assert row["total_points"] == 8
    assert row["price_at_deadline"] == 12.5
    assert row["position_code"] is None      # absent, not empty string
    assert row["expected_goals"] is None     # absent, not zero
    assert row["defensive_contribution"] is None


def test_2025_26_shape_parses_the_new_defensive_columns() -> None:
    row = parse_gameweek_row({
        "name": "Player", "element": "1", "GW": "1", "position": "DEF",
        "total_points": "6", "defensive_contribution": "12", "tackles": "3",
        "recoveries": "7", "clearances_blocks_interceptions": "5",
        "expected_goals": "0.12",
    })
    assert row["defensive_contribution"] == 12
    assert row["expected_goals"] == pytest.approx(0.12)
    assert row["position_code"] == "DEF"


def test_gameweek_number_falls_back_to_round() -> None:
    """Some seasons carry GW, some carry round; either must work."""
    assert parse_gameweek_row({"element": "1", "round": "7"})["gameweek_number"] == 7
    assert parse_gameweek_row({"element": "1", "GW": "9", "round": "7"})["gameweek_number"] == 9


# --- timestamps and booleans ------------------------------------------------


def test_kickoff_is_parsed_as_utc() -> None:
    row = parse_gameweek_row({"element": "1", "GW": "1",
                              "kickoff_time": "2025-08-16T14:00:00Z"})
    assert row["kickoff_at"] == datetime(2025, 8, 16, 14, 0, tzinfo=UTC)


def test_naive_timestamps_are_assumed_utc_not_local() -> None:
    row = parse_gameweek_row({"element": "1", "GW": "1",
                              "kickoff_time": "2025-08-16T14:00:00"})
    assert row["kickoff_at"].tzinfo is not None


def test_unparseable_timestamp_is_none_rather_than_an_exception() -> None:
    row = parse_gameweek_row({"element": "1", "GW": "1", "kickoff_time": "not a date"})
    assert row["kickoff_at"] is None


@pytest.mark.parametrize(("raw", "expected"),
                         [("True", True), ("true", True), ("1", True),
                          ("False", False), ("0", False)])
def test_boolean_forms(raw: str, expected: bool) -> None:
    assert parse_gameweek_row({"element": "1", "GW": "1", "was_home": raw})["was_home"] is expected


# --- dimensions -------------------------------------------------------------


def test_team_row() -> None:
    row = parse_team_row({"id": "1", "code": "3", "name": "Arsenal",
                          "short_name": "ARS", "strength_overall_home": "1300"})
    assert (row["fpl_team_id"], row["fpl_team_code"], row["name"]) == (1, 3, "Arsenal")
    assert row["strength_overall_home"] == 1300


def test_fixture_row() -> None:
    row = parse_fixture_row({
        "id": "1", "event": "1", "team_h": "1", "team_a": "2",
        "team_h_score": "2", "team_a_score": "1", "finished": "True",
        "team_h_difficulty": "3", "team_a_difficulty": "4",
        "kickoff_time": "2025-08-16T14:00:00Z",
    })
    assert (row["home_fpl_team_id"], row["away_fpl_team_id"]) == (1, 2)
    assert (row["home_score"], row["away_score"]) == (2, 1)
    assert row["is_finished"] is True


def test_unplayed_fixture_has_no_scores() -> None:
    row = parse_fixture_row({"id": "9", "event": "38", "team_h": "1", "team_a": "2",
                             "team_h_score": "", "team_a_score": "", "finished": "False"})
    assert row["home_score"] is None
    assert row["is_finished"] is False


def test_full_name_is_first_plus_second() -> None:
    row = parse_player_row({"code": "118748", "id": "328", "first_name": "Mohamed",
                            "second_name": "Salah", "web_name": "M.Salah"})
    assert row["full_name"] == "Mohamed Salah"
    assert row["display_name"] == "M.Salah"
