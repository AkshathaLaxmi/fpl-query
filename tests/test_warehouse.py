"""Integration tests against a loaded database.

Run with `pytest -m database`. Skipped automatically when no database is
reachable, so `pytest` on a laptop with no Postgres still passes.

These assert the two properties that the unit tests structurally cannot: that
the point-in-time model holds over real data, and that the execution role is
actually confined. Both are invariants rather than examples -- they are written
to fail if any future change breaks them, not to check one hand-picked row.
"""

from __future__ import annotations

import psycopg
import pytest

from fplq.config import settings

pytestmark = pytest.mark.database


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _reachable(settings.reader_dsn),
    reason="no database reachable; run `fplq bootstrap && fplq load` first",
)


@pytest.fixture
def reader() -> psycopg.Connection:
    with psycopg.connect(settings.reader_dsn) as conn:
        yield conn


def _one(conn: psycopg.Connection, sql: str, *params: object) -> object:
    with conn.cursor() as cur:
        cur.execute(sql, params or None)
        row = cur.fetchone()
        return row[0] if row else None


# --- the point-in-time invariant --------------------------------------------


@requires_db
def test_no_overlapping_price_intervals(reader: psycopg.Connection) -> None:
    """At most one price is live per player at any instant.

    The whole as-of design rests on this. It is enforced by an exclusion
    constraint, and it is asserted here as well, because the constraint can only
    protect rows the loader inserts -- this catches a future migration that
    weakens it.
    """
    overlaps = _one(reader, """
        SELECT count(*) FROM analytics.player_price_history a
        JOIN analytics.player_price_history b
          ON a.player_id = b.player_id
         AND a.valid_from < b.valid_from
         AND tstzrange(a.valid_from, a.valid_to) && tstzrange(b.valid_from, b.valid_to)
    """)
    assert overlaps == 0


@requires_db
def test_only_the_current_season_has_open_intervals(reader: psycopg.Connection) -> None:
    """An unclosed interval in a finished season swallows every later season.

    This is the bug that shipped and was caught by the exclusion constraint:
    a May-2025 interval left open overlapped all of 2025-26, and those rows
    silently vanished.
    """
    open_in_finished = _one(reader, """
        SELECT count(DISTINCT h.season) FROM analytics.player_price_history h
        WHERE h.valid_to IS NULL
          AND h.season <> (SELECT max(season) FROM analytics.player_price_history)
    """)
    assert open_in_finished == 0


@requires_db
def test_price_as_of_returns_the_price_of_that_moment(reader: psycopg.Connection) -> None:
    """A price read as-of a past instant must match the interval covering it."""
    row = _one(reader, """
        SELECT h.price = analytics.price_as_of(h.player_id, h.valid_from + interval '1 hour')
        FROM analytics.player_price_history h
        WHERE h.valid_to IS NOT NULL
          AND h.valid_to > h.valid_from + interval '1 hour'
        LIMIT 1
    """)
    assert row is True


@requires_db
def test_a_price_change_is_visible_across_its_boundary(reader: psycopg.Connection) -> None:
    """The point of the whole model: the answer differs depending on when you ask."""
    changed = _one(reader, """
        SELECT count(*) FROM (
            SELECT h.player_id, h.valid_from,
                   analytics.price_as_of(h.player_id, h.valid_from - interval '1 second') AS before,
                   analytics.price_as_of(h.player_id, h.valid_from + interval '1 second') AS after
            FROM analytics.player_price_history h
            WHERE h.valid_to IS NOT NULL
            LIMIT 200
        ) t WHERE before IS DISTINCT FROM after
    """)
    assert changed > 0


# --- identity ---------------------------------------------------------------


@requires_db
def test_no_open_resolution_issues(reader: psycopg.Connection) -> None:
    """A clean load resolves every name. If this fails, read `fplq issues`."""
    with psycopg.connect(settings.writer_dsn) as conn:
        assert _one(conn, "SELECT count(*) FROM core.resolution_issue "
                          "WHERE resolved_at IS NULL") == 0


@requires_db
def test_players_are_linked_across_seasons_not_duplicated(reader: psycopg.Connection) -> None:
    """One human, one player_id, however many seasons they played."""
    multi_season = _one(reader, """
        SELECT count(*) FROM (
            SELECT player_id FROM analytics.player_season
            GROUP BY player_id HAVING count(DISTINCT season) > 1
        ) t
    """)
    assert multi_season > 500


@requires_db
def test_short_name_equality_is_the_trap_find_player_solves(reader: psycopg.Connection) -> None:
    """The finding that motivated 005, pinned so it cannot regress."""
    assert _one(reader, "SELECT count(*) FROM analytics.player WHERE player_name = 'Salah'") == 0
    assert _one(reader, "SELECT count(*) FROM analytics.find_player('salah')") >= 1


@requires_db
def test_letter_folding_finds_stroked_letters(reader: psycopg.Connection) -> None:
    """The finding that motivated 007. Nobody types the o-slash."""
    for typed in ("odegaard", "hojlund"):
        assert _one(reader, "SELECT count(*) FROM analytics.find_player(%s)", typed) >= 1


# --- the execution boundary -------------------------------------------------


@requires_db
@pytest.mark.parametrize("statement", [
    "SELECT count(*) FROM core.player",
    "SELECT count(*) FROM raw.document",
    "SELECT count(*) FROM core.player_price_history",
])
def test_reader_cannot_reach_core_or_raw(reader: psycopg.Connection, statement: str) -> None:
    """The model is told about analytics; the role can reach only analytics."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege), reader.cursor() as cur:
        cur.execute(statement)


@requires_db
@pytest.mark.parametrize("statement", [
    "CREATE TABLE analytics.evil (x int)",
    "DROP VIEW analytics.player_gameweek",
    "INSERT INTO analytics.player (player_id) VALUES (1)",
])
def test_reader_cannot_write(reader: psycopg.Connection, statement: str) -> None:
    """Read-only at the role level, so a validator bug is not a data-loss bug."""
    with pytest.raises(psycopg.Error), reader.cursor() as cur:
        cur.execute(statement)


@requires_db
def test_reader_has_a_statement_timeout(reader: psycopg.Connection) -> None:
    assert _one(reader, "SHOW statement_timeout") == "10s"


@requires_db
def test_reader_search_path_cannot_see_core(reader: psycopg.Connection) -> None:
    """Unqualified names resolve inside analytics, never into core."""
    assert "core" not in str(_one(reader, "SHOW search_path"))


# --- the analytics contract -------------------------------------------------


@requires_db
def test_grain_is_player_by_fixture_not_player_by_gameweek(reader: psycopg.Connection) -> None:
    """Double gameweeks are real; a per-gameweek grain would lose a match."""
    doubles = _one(reader, """
        SELECT count(*) FROM (
            SELECT player_id, season, gameweek FROM analytics.player_gameweek
            GROUP BY player_id, season, gameweek HAVING count(*) > 1
        ) t
    """)
    assert doubles > 0


@requires_db
def test_season_totals_agree_with_match_rows(reader: psycopg.Connection) -> None:
    """A drill-down must never contradict the summary it came from."""
    mismatches = _one(reader, """
        SELECT count(*) FROM (
            SELECT s.player_id, s.season, s.points AS total,
                   (SELECT COALESCE(sum(g.points), 0) FROM analytics.player_gameweek g
                     WHERE g.player_id = s.player_id AND g.season = s.season) AS summed
            FROM analytics.player_season s LIMIT 500
        ) t WHERE total <> summed
    """)
    assert mismatches == 0


@requires_db
def test_every_match_appears_twice_in_team_fixture(reader: psycopg.Connection) -> None:
    """One row per team per fixture is what removes the UNION from every
    upcoming-fixture question."""
    fixtures = _one(reader, "SELECT count(*) FROM analytics.fixture")
    team_rows = _one(reader, "SELECT count(*) FROM analytics.team_fixture")
    assert team_rows == fixtures * 2


@requires_db
def test_expected_goals_are_null_before_they_existed_not_zero(
    reader: psycopg.Connection,
) -> None:
    """Absent is not zero -- an average over history depends on the difference."""
    assert _one(reader, """
        SELECT count(*) FROM analytics.player_gameweek
        WHERE season = '2019-20' AND expected_goals IS NOT NULL
    """) == 0


# --- input handling: user input is data, never a pattern --------------------
#
# find_player() takes, by design, whatever a stranger typed. These pin the
# fixes from migration 008; each one failed before it.


@requires_db
@pytest.mark.parametrize("metachar", ["%", "_", "%%", "a%", "\\"])
def test_like_metacharacters_are_not_wildcards(
    reader: psycopg.Connection, metachar: str
) -> None:
    """find_player('%') matched all 2,210 players before input was escaped."""
    assert _one(reader, "SELECT count(*) FROM analytics.find_player(%s)", metachar) == 0


@requires_db
@pytest.mark.parametrize("hostile", ["(a+)+$", "(", "[", "a|b", ".*", "\\y"])
def test_regex_metacharacters_are_inert(reader: psycopg.Connection, hostile: str) -> None:
    """Input no longer reaches a regex engine at all, so none of these can
    error, match everything, or backtrack."""
    count = _one(reader, "SELECT count(*) FROM analytics.find_player(%s)", hostile)
    assert count == 0


@requires_db
def test_results_are_bounded(reader: psycopg.Connection) -> None:
    """A one-character query must not be able to return the whole league."""
    assert _one(reader, "SELECT count(*) FROM analytics.find_player('a')") <= 25
    assert _one(reader, "SELECT count(*) FROM analytics.find_player('a', 500)") <= 100


@requires_db
def test_escaping_did_not_break_real_lookups(reader: psycopg.Connection) -> None:
    for typed in ("salah", "odegaard", "son", "bruno fernandes"):
        assert _one(reader, "SELECT count(*) FROM analytics.find_player(%s)", typed) >= 1


@requires_db
def test_only_the_escaped_find_player_overload_exists(reader: psycopg.Connection) -> None:
    """The unescaped single-argument version must be gone, not shadowed."""
    assert _one(reader, """
        SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'analytics' AND p.proname = 'find_player'
    """) == 1


# --- privileges -------------------------------------------------------------


@requires_db
def test_reader_cannot_create_temporary_objects(reader: psycopg.Connection) -> None:
    """TEMP is granted to PUBLIC by default; it is a disk-consumption lever no
    statement timeout bounds."""
    assert _one(reader, "SELECT has_database_privilege('fplq_reader', 'fplq', 'TEMP')") is False


@requires_db
def test_reader_has_no_privilege_on_core_or_raw(reader: psycopg.Connection) -> None:
    """The grant is the real boundary -- unlike the session settings, no SET
    can change it."""
    for schema in ("core", "raw"):
        assert _one(reader, "SELECT has_schema_privilege('fplq_reader', %s, 'USAGE')",
                    schema) is False


@requires_db
def test_session_settings_are_defaults_not_a_boundary(reader: psycopg.Connection) -> None:
    """Documents the limitation honestly rather than implying a guarantee.

    statement_timeout and default_transaction_read_only are USERSET GUCs: a
    session can raise them with a plain SET. This test asserts that this is
    still true, so nobody reading the README believes otherwise. The defence is
    the validator refusing multi-statement input, plus the grants above.
    """
    with reader.cursor() as cur:
        cur.execute("SET statement_timeout = '42s'")
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] == "42s"   # overridable, by design of Postgres

    # ...but overriding it buys nothing, because the grants still hold.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), reader.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.player")


@requires_db
def test_multi_token_names_match_across_middle_names(reader: psycopg.Connection) -> None:
    """"bruno fernandes" must find "Bruno Miguel Borges Fernandes".

    A first name and a surname is the most natural thing a person types, and
    contiguous-substring search never matched it for the many Premier League
    players who carry middle names.
    """
    for typed in ("bruno fernandes", "mohamed salah", "martin odegaard"):
        assert _one(reader, "SELECT count(*) FROM analytics.find_player(%s)", typed) >= 1


@requires_db
def test_exact_matches_outrank_scattered_token_matches(reader: psycopg.Connection) -> None:
    """Ranking must not regress now that scattered tokens can match at all."""
    rank = _one(reader, "SELECT match_rank FROM analytics.find_player('mohamed salah') LIMIT 1")
    assert rank == 1


@requires_db
def test_display_name_does_not_depend_on_load_order(reader: psycopg.Connection) -> None:
    """The canonical name is the most recent season's, not the first one seen.

    FPL's web_name for Mohamed Salah was "Salah" in 2019-20 and is "M.Salah"
    now, so a loader that keeps the first name it saw produces a different
    database depending on the order seasons were loaded -- from identical
    inputs, with identical row counts.
    """
    assert _one(reader, """
        SELECT player_name FROM analytics.player WHERE full_name = 'Mohamed Salah'
    """) == "M.Salah"
