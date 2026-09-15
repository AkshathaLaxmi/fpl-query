"""Live API row parsing.

Field names drawn from a real bootstrap-static/fixtures response (fetched by
hand while building the loader) -- deliberately not the archive's CSV column
names, which is exactly the case that would slip through if the two parsers
were accidentally merged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fplq.transform.live import (
    parse_live_element,
    parse_live_event,
    parse_live_fixture,
    parse_live_team,
)

# --- elements ----------------------------------------------------------------


def test_price_is_converted_from_tenths() -> None:
    row = parse_live_element({"now_cost": 125, "code": 1, "id": 1})
    assert row["price"] == 12.5


def test_ownership_percent_is_a_string_and_becomes_a_float() -> None:
    row = parse_live_element({"selected_by_percent": "39.1", "code": 1, "id": 1})
    assert row["ownership_pct"] == 39.1


def test_missing_ownership_percent_is_none_not_zero() -> None:
    row = parse_live_element({"selected_by_percent": None, "code": 1, "id": 1})
    assert row["ownership_pct"] is None


def test_full_name_is_first_plus_second() -> None:
    row = parse_live_element({
        "code": 1, "id": 1, "first_name": "Mohamed", "second_name": "Salah",
        "web_name": "M.Salah",
    })
    assert row["full_name"] == "Mohamed Salah"
    assert row["display_name"] == "M.Salah"


def test_display_name_falls_back_to_full_name_when_web_name_is_blank() -> None:
    row = parse_live_element({"code": 1, "id": 1, "first_name": "A", "second_name": "B",
                              "web_name": ""})
    assert row["display_name"] == "A B"


# --- teams ---------------------------------------------------------------------


def test_team_row_carries_both_ids() -> None:
    row = parse_live_team({"id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"})
    assert row["fpl_team_id"] == 1
    assert row["fpl_team_code"] == 3


# --- events (gameweeks) ---------------------------------------------------------


def test_event_deadline_is_parsed_as_utc() -> None:
    row = parse_live_event({"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "finished": True})
    assert row["gameweek_number"] == 1
    assert row["deadline_at"] == datetime(2026, 8, 21, 17, 30, tzinfo=UTC)
    assert row["is_finished"] is True


def test_event_not_finished_defaults_false() -> None:
    row = parse_live_event({"id": 4, "deadline_time": None, "finished": False})
    assert row["is_finished"] is False
    assert row["deadline_at"] is None


# --- fixtures --------------------------------------------------------------------


def test_fixture_row_maps_team_h_and_team_a() -> None:
    row = parse_live_fixture({
        "id": 1, "event": 1, "kickoff_time": "2026-08-21T19:00:00Z",
        "team_h": 1, "team_a": 7, "team_h_score": 3, "team_a_score": 0,
        "team_h_difficulty": 2, "team_a_difficulty": 5, "finished": True,
    })
    assert row["home_fpl_team_id"] == 1
    assert row["away_fpl_team_id"] == 7
    assert row["home_score"] == 3
    assert row["away_score"] == 0
    assert row["is_finished"] is True


def test_unstarted_fixture_has_no_score() -> None:
    row = parse_live_fixture({
        "id": 2, "event": 4, "kickoff_time": "2026-09-20T14:00:00Z",
        "team_h": 2, "team_a": 3, "team_h_score": None, "team_a_score": None,
        "finished": False,
    })
    assert row["home_score"] is None
    assert row["away_score"] is None
    assert row["is_finished"] is False
