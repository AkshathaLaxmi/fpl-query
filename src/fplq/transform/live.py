"""Live ingestion from the official FPL API.

`fpl_api.FplApiClient.bootstrap_static()` is, in its own docstring, "the daily
snapshot that drives price and ownership history." This module is where that
snapshot becomes rows: today's price, today's ownership, and match results as
they are confirmed, for whichever season is currently being played.

Historical seasons stay owned by `transform/loader.py` -- this module only
ever touches the current season, and never invents one. If the season is not
already in `core.season`, that is `fplq load` in the future, not here.

Two things follow from having no "as of" parameter on the live API:

  * There is no fetch-vs-execute gap to worry about for freshness (unlike the
    archive, which can drift from what the live API now says), but there is
    also no way to ask the API "what did you say yesterday". Replay therefore
    reads back the JSON this module already landed in `raw.document`, rather
    than re-fetching -- a second fetch would return *today's* state, not the
    state the batch being replayed actually saw.
  * Landing goes through `raw.document` in Postgres, not just the byte
    snapshot in the raw store: `raw.document` is what `replay()` reads.

One subtlety in the price history handoff. `core.player_price_history` has
one exclusion constraint per player across ALL sources (see 002_core.sql) --
by design, "at most one price is live per player at any instant" does not
care who reported it. The current season's most recent archive load leaves
its last interval open (because the season is not over), so the first live
run for a player takes over that same open interval -- closing it and
opening a new one -- rather than inserting a second, competing one. See
`_refresh_price` below.

That handoff is one-directional. Re-running `fplq load` for a season *after*
live ingestion has moved a player's price is not yet reconciled here: the
archive loader rebuilds a season's `source='fpl_archive'` rows from scratch
and leaves any later `source='fpl'` row untouched, so if the archive's
rebuilt last interval and a live interval both end up open, the exclusion
constraint will (correctly) refuse the second insert. That is a real,
undecided interaction, not a hidden one: the fix is to make the archive
reload season-current-aware, and it is not built yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from fplq.config import CURRENT_SEASON, ConfigError, Settings
from fplq.config import settings as default_settings
from fplq.ingest.fpl_api import FplApiClient
from fplq.resolve.names import name_key
from fplq.resolve.players import PlayerResolver, load_overrides

log = logging.getLogger(__name__)

NAMESPACE = "fpl_api"


def _to_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Row shaping -- same vocabulary as transform/loader.py's archive parsers, over
# the live API's JSON field names instead of the archive's CSV column names.
# ---------------------------------------------------------------------------


def parse_live_element(elem: dict[str, Any]) -> dict[str, object]:
    first = (elem.get("first_name") or "").strip()
    second = (elem.get("second_name") or "").strip()
    return {
        "fpl_player_code": _to_int(elem.get("code")),
        "fpl_element_id": _to_int(elem.get("id")),
        "first_name": first or None,
        "last_name": second or None,
        "display_name": (elem.get("web_name") or "").strip() or f"{first} {second}".strip(),
        "full_name": f"{first} {second}".strip(),
        "birth_date": elem.get("birth_date") or None,
        "position_id": _to_int(elem.get("element_type")),
        "fpl_team_id": _to_int(elem.get("team")),
        "fpl_team_code": _to_int(elem.get("team_code")),
        "price": _to_int(elem.get("now_cost")) / 10.0 if elem.get("now_cost") is not None else None,
        "ownership_pct": _to_float(elem.get("selected_by_percent")),
        "transfers_in": _to_int(elem.get("transfers_in")),
        "transfers_out": _to_int(elem.get("transfers_out")),
    }


def parse_live_team(team: dict[str, Any]) -> dict[str, object]:
    return {
        "fpl_team_id": _to_int(team.get("id")),
        "fpl_team_code": _to_int(team.get("code")),
        "name": (team.get("name") or "").strip(),
        "short_name": (team.get("short_name") or "").strip(),
        "strength_overall_home": _to_int(team.get("strength_overall_home")),
        "strength_overall_away": _to_int(team.get("strength_overall_away")),
        "strength_attack_home": _to_int(team.get("strength_attack_home")),
        "strength_attack_away": _to_int(team.get("strength_attack_away")),
        "strength_defence_home": _to_int(team.get("strength_defence_home")),
        "strength_defence_away": _to_int(team.get("strength_defence_away")),
    }


def parse_live_event(event: dict[str, Any]) -> dict[str, object]:
    return {
        "gameweek_number": _to_int(event.get("id")),
        "deadline_at": _to_ts(event.get("deadline_time")),
        "is_finished": bool(event.get("finished")),
    }


def parse_live_fixture(fx: dict[str, Any]) -> dict[str, object]:
    return {
        "fpl_fixture_id": _to_int(fx.get("id")),
        "gameweek_number": _to_int(fx.get("event")),
        "kickoff_at": _to_ts(fx.get("kickoff_time")),
        "home_fpl_team_id": _to_int(fx.get("team_h")),
        "away_fpl_team_id": _to_int(fx.get("team_a")),
        "home_score": _to_int(fx.get("team_h_score")),
        "away_score": _to_int(fx.get("team_a_score")),
        "home_difficulty": _to_int(fx.get("team_h_difficulty")),
        "away_difficulty": _to_int(fx.get("team_a_difficulty")),
        "is_finished": bool(fx.get("finished")),
    }


@dataclass
class LiveIngestReport:
    season: str
    batch_id: int | None = None
    teams: int = 0
    players_created: int = 0
    players_refreshed: int = 0
    price_changes: int = 0
    ownership_snapshots: int = 0
    fixtures_refreshed: int = 0
    gameweeks_refreshed: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def rows_written(self) -> int:
        # players_created is a subset of players_refreshed (a newly created
        # player is refreshed too, in the same pass) -- not added again here.
        return (
            self.teams + self.players_refreshed
            + self.price_changes + self.ownership_snapshots
            + self.fixtures_refreshed + self.gameweeks_refreshed
        )

    def summary(self) -> str:
        return (
            f"{self.season}: {self.teams} teams, {self.players_refreshed} players refreshed "
            f"({self.players_created} new), {self.price_changes} price changes, "
            f"{self.ownership_snapshots} ownership snapshots, "
            f"{self.fixtures_refreshed} fixtures, {self.gameweeks_refreshed} gameweeks, "
            f"{len(self.unmatched)} unmatched"
        )


class LiveLoader:
    """Fetches (or replays) a live snapshot and refreshes the current season."""

    def __init__(self, conn: psycopg.Connection, settings: Settings = default_settings) -> None:
        self.conn = conn
        self.settings = settings
        self.overrides = load_overrides(settings.overrides_path)

    # -- batch + document ledger --------------------------------------------

    def _open_batch(self, endpoint: str, season: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.ingest_batch (source, endpoint, season) "
                "VALUES ('fpl_api', %s, %s) RETURNING batch_id",
                (endpoint, season),
            )
            return cur.fetchone()["batch_id"]

    def _close_batch(self, batch_id: int, *, row_count: int,
                     status: str = "succeeded", error: str | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE raw.ingest_batch SET status=%s, completed_at=now(), "
                "row_count=%s, error=%s WHERE batch_id=%s",
                (status, row_count, error, batch_id),
            )

    def _land(self, batch_id: int, record_type: str, items: list[dict[str, Any]],
              key_field: str, observed_at: datetime) -> None:
        """Write each item as its own row in raw.document, JSONB untouched.

        This -- not the byte snapshot in the raw store -- is what `replay()`
        reads. One row per record rather than one row per fetch, so a replay
        or a future per-player audit does not have to re-parse a whole
        bootstrap-static payload to find one player.
        """
        import json

        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO raw.document (batch_id, record_type, record_key, observed_at, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (batch_id, record_type, str(item.get(key_field)), observed_at, json.dumps(item))
                    for item in items
                ],
            )

    def _documents(self, batch_id: int, record_type: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM raw.document WHERE batch_id=%s AND record_type=%s",
                (batch_id, record_type),
            )
            return [row["payload"] for row in cur.fetchall()]

    def _observed_at(self, batch_id: int) -> datetime | None:
        """The moment the batch's documents record as true, not when the row
        naming the batch was inserted.

        `raw.ingest_batch.requested_at` is written by `_open_batch` just
        before the fetch; `raw.document.observed_at` is written just after,
        from the response itself. Replaying with the batch's `requested_at`
        would timestamp the replay a few hundred milliseconds earlier than
        the original run actually saw -- close enough to look right and wrong
        enough that two ownership snapshots meant to be identical would not
        collide on `UNIQUE (player_id, snapshot_at)` and instead silently
        duplicate.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT observed_at FROM raw.document WHERE batch_id=%s LIMIT 1",
                (batch_id,),
            )
            row = cur.fetchone()
            return row["observed_at"] if row else None

    # -- reference -----------------------------------------------------------

    def _season_id(self, season: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT season_id FROM core.season WHERE name=%s", (season,))
            row = cur.fetchone()
        if row is None:
            raise ConfigError(
                f"season {season!r} does not exist yet. Live ingestion refreshes a "
                f"season, it does not create one -- run `fplq load --seasons {season}` first."
            )
        return row["season_id"]

    def _mark_current(self, season_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE core.season SET is_current = (season_id = %s)", (season_id,))

    # -- teams -----------------------------------------------------------------

    def _refresh_teams(self, season_id: int, teams: list[dict[str, Any]],
                       report: LiveIngestReport) -> dict[int, int]:
        """Returns {fpl_team_id -> team_id}. Same upsert shape as the archive loader."""
        mapping: dict[int, int] = {}
        with self.conn.cursor() as cur:
            for raw in teams:
                row = parse_live_team(raw)
                if not row["name"]:
                    continue
                cur.execute(
                    """
                    INSERT INTO core.team (fpl_team_code, name, short_name)
                    VALUES (%(fpl_team_code)s, %(name)s, %(short_name)s)
                    ON CONFLICT (fpl_team_code) DO UPDATE
                        SET name = EXCLUDED.name, short_name = EXCLUDED.short_name
                    RETURNING team_id
                    """,
                    row,
                )
                team_id = cur.fetchone()["team_id"]
                cur.execute(
                    """
                    INSERT INTO core.team_season (
                        team_id, season_id, fpl_team_id,
                        strength_overall_home, strength_overall_away,
                        strength_attack_home, strength_attack_away,
                        strength_defence_home, strength_defence_away)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (team_id, season_id) DO UPDATE SET
                        fpl_team_id = EXCLUDED.fpl_team_id,
                        strength_overall_home = EXCLUDED.strength_overall_home,
                        strength_overall_away = EXCLUDED.strength_overall_away,
                        strength_attack_home  = EXCLUDED.strength_attack_home,
                        strength_attack_away  = EXCLUDED.strength_attack_away,
                        strength_defence_home = EXCLUDED.strength_defence_home,
                        strength_defence_away = EXCLUDED.strength_defence_away
                    """,
                    (team_id, season_id, row["fpl_team_id"],
                     row["strength_overall_home"], row["strength_overall_away"],
                     row["strength_attack_home"], row["strength_attack_away"],
                     row["strength_defence_home"], row["strength_defence_away"]),
                )
                mapping[int(row["fpl_team_id"])] = team_id
                report.teams += 1
        return mapping

    # -- players, price, ownership -------------------------------------------

    def _existing_code_index(self) -> dict[int, int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT fpl_player_code, player_id FROM core.player "
                "WHERE fpl_player_code IS NOT NULL"
            )
            return {r["fpl_player_code"]: r["player_id"] for r in cur.fetchall()}

    def _existing_players(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT player_id, display_name, full_name FROM core.player")
            return cur.fetchall()

    def _record_issue(self, cur: psycopg.Cursor, issue_type: str, season: str,
                      raw_value: str, candidates: list[dict[str, object]] | None = None) -> None:
        import json

        cur.execute(
            """
            INSERT INTO core.resolution_issue (issue_type, source, season, raw_value, candidates)
            VALUES (%s, 'fpl_api', %s, %s, %s)
            """,
            (issue_type, season, raw_value, json.dumps(candidates) if candidates else None),
        )

    def _refresh_players(self, season_id: int, season: str, elements: list[dict[str, Any]],
                         team_map: dict[int, int], fetched_at: datetime,
                         report: LiveIngestReport) -> dict[int, int]:
        """Returns {fpl_element_id -> player_id}.

        Every element in bootstrap-static already belongs to a player almost
        always known from the archive load -- this is a refresh path, not a
        registration path, and the identity cascade only has to earn its keep
        for the mid-season signings the archive has not mirrored yet.
        """
        code_index = self._existing_code_index()
        resolver = PlayerResolver.from_rows(self._existing_players(), self.overrides)
        element_map: dict[int, int] = {}

        with self.conn.cursor() as cur:
            for raw in elements:
                row = parse_live_element(raw)
                element_id = row["fpl_element_id"]
                if element_id is None:
                    continue

                code = row["fpl_player_code"]
                full_name = str(row["full_name"]) or str(row["display_name"])

                player_id = code_index.get(code) if code is not None else None
                if player_id is None and code is not None:
                    # A code the archive has never seen: genuinely new to the
                    # league, not just new to this loader's cache.
                    match = resolver.resolve(full_name)
                    player_id = match.player_id
                    if player_id is None:
                        cur.execute(
                            """
                            INSERT INTO core.player (
                                fpl_player_code, first_name, last_name,
                                display_name, full_name, normalised_name, birth_date)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING player_id
                            """,
                            (code, row["first_name"], row["last_name"], row["display_name"],
                             full_name, name_key(full_name), row["birth_date"]),
                        )
                        player_id = cur.fetchone()["player_id"]
                        report.players_created += 1
                if player_id is None:
                    self._record_issue(cur, "unmatched_player", season, full_name)
                    report.unmatched.append(full_name)
                    continue

                if code is not None:
                    code_index[code] = player_id
                cur.execute(
                    """
                    INSERT INTO core.player_source_id
                        (player_id, source, season_id, source_id, match_method, match_score)
                    VALUES (%s, 'fpl', %s, %s, 'code', 100.0)
                    ON CONFLICT (source, season_id, source_id) DO NOTHING
                    """,
                    (player_id, season_id, str(element_id)),
                )
                cur.execute(
                    """
                    INSERT INTO core.player_season
                        (player_id, season_id, team_id, position_id, fpl_element_id,
                         start_price, end_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (player_id, season_id) DO UPDATE SET
                        team_id     = EXCLUDED.team_id,
                        position_id = EXCLUDED.position_id,
                        fpl_element_id = EXCLUDED.fpl_element_id,
                        end_price   = EXCLUDED.end_price
                    """,
                    (player_id, season_id,
                     team_map.get(row["fpl_team_id"]) if row["fpl_team_id"] else None,
                     row["position_id"], element_id, row["price"], row["price"]),
                )
                element_map[int(element_id)] = player_id
                report.players_refreshed += 1

                if row["price"] is not None and self._refresh_price(
                    player_id, season_id, row["price"], fetched_at
                ):
                    report.price_changes += 1
                if row["ownership_pct"] is not None:
                    self._append_ownership(player_id, season_id, fetched_at,
                                           row["ownership_pct"],
                                           row["transfers_in"], row["transfers_out"])
                    report.ownership_snapshots += 1

        return element_map

    def _refresh_price(self, player_id: int, season_id: int, price: float,
                       observed_at: datetime) -> bool:
        """Type-2 handoff: take over the open interval, don't compete with it.

        Returns True if the price actually changed (a new interval was
        opened), False if today's price matches what was already on record --
        the common case, and one that should not grow the table.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT price_history_id, price FROM core.player_price_history
                WHERE player_id=%s AND valid_to IS NULL
                ORDER BY valid_from DESC LIMIT 1
                """,
                (player_id,),
            )
            current = cur.fetchone()

            if current is not None and abs(float(current["price"]) - price) < 1e-9:
                return False

            if current is not None:
                cur.execute(
                    "UPDATE core.player_price_history SET valid_to=%s WHERE price_history_id=%s",
                    (observed_at, current["price_history_id"]),
                )

            cur.execute(
                """
                INSERT INTO core.player_price_history
                    (player_id, season_id, price, valid_from, valid_to, source)
                VALUES (%s, %s, %s, %s, NULL, 'fpl')
                """,
                (player_id, season_id, price, observed_at),
            )
        return True

    def _append_ownership(self, player_id: int, season_id: int, observed_at: datetime,
                          ownership_pct: float, transfers_in: int | None,
                          transfers_out: int | None) -> None:
        # selected_by (the absolute count) is left NULL: bootstrap-static gives
        # only a percentage. Inventing a count from total_players would be
        # precision the source does not have.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.player_ownership_snapshot
                    (player_id, season_id, snapshot_at, ownership_pct,
                     transfers_in, transfers_out)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (player_id, snapshot_at) DO UPDATE SET
                    ownership_pct = EXCLUDED.ownership_pct,
                    transfers_in  = EXCLUDED.transfers_in,
                    transfers_out = EXCLUDED.transfers_out
                """,
                (player_id, season_id, observed_at, ownership_pct,
                 transfers_in, transfers_out),
            )

    # -- gameweeks and fixtures -----------------------------------------------

    def _refresh_gameweeks(self, season_id: int, events: list[dict[str, Any]],
                           report: LiveIngestReport) -> dict[int, int]:
        """Returns {gameweek_number -> gameweek_id}, with the true FPL deadline.

        The archive derives a deadline (one hour before first kickoff) because
        it has no better signal. The live API knows the real one, so this
        overwrites rather than merely fills in what the archive guessed.
        """
        mapping: dict[int, int] = {}
        with self.conn.cursor() as cur:
            for raw in events:
                row = parse_live_event(raw)
                if row["gameweek_number"] is None:
                    continue
                cur.execute(
                    """
                    INSERT INTO core.gameweek (season_id, gameweek_number, deadline_at, is_finished)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (season_id, gameweek_number) DO UPDATE SET
                        deadline_at = COALESCE(EXCLUDED.deadline_at, core.gameweek.deadline_at),
                        is_finished = EXCLUDED.is_finished
                    RETURNING gameweek_id
                    """,
                    (season_id, row["gameweek_number"], row["deadline_at"], row["is_finished"]),
                )
                mapping[row["gameweek_number"]] = cur.fetchone()["gameweek_id"]
                report.gameweeks_refreshed += 1
        return mapping

    def _refresh_fixtures(self, season_id: int, team_map: dict[int, int],
                          gameweek_map: dict[int, int], fixtures: list[dict[str, Any]],
                          report: LiveIngestReport) -> None:
        with self.conn.cursor() as cur:
            for raw in fixtures:
                row = parse_live_fixture(raw)
                home = team_map.get(row["home_fpl_team_id"])
                away = team_map.get(row["away_fpl_team_id"])
                if home is None or away is None or row["fpl_fixture_id"] is None:
                    continue
                cur.execute(
                    """
                    INSERT INTO core.fixture (
                        season_id, gameweek_id, fpl_fixture_id, kickoff_at,
                        home_team_id, away_team_id, home_score, away_score,
                        home_difficulty, away_difficulty, is_finished)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (season_id, fpl_fixture_id) DO UPDATE SET
                        gameweek_id = EXCLUDED.gameweek_id,
                        kickoff_at  = EXCLUDED.kickoff_at,
                        home_score  = EXCLUDED.home_score,
                        away_score  = EXCLUDED.away_score,
                        home_difficulty = EXCLUDED.home_difficulty,
                        away_difficulty = EXCLUDED.away_difficulty,
                        is_finished = EXCLUDED.is_finished
                    """,
                    (season_id, gameweek_map.get(row["gameweek_number"]),
                     row["fpl_fixture_id"], row["kickoff_at"], home, away,
                     row["home_score"], row["away_score"],
                     row["home_difficulty"], row["away_difficulty"], row["is_finished"]),
                )
                report.fixtures_refreshed += 1

    # -- orchestration ---------------------------------------------------------

    def _apply(self, season_id: int, season: str, elements: list[dict[str, Any]],
              teams: list[dict[str, Any]], events: list[dict[str, Any]],
              fixtures: list[dict[str, Any]], observed_at: datetime,
              report: LiveIngestReport) -> None:
        self._mark_current(season_id)
        team_map = self._refresh_teams(season_id, teams, report)
        self._refresh_players(season_id, season, elements, team_map, observed_at, report)
        gameweek_map = self._refresh_gameweeks(season_id, events, report)
        self._refresh_fixtures(season_id, team_map, gameweek_map, fixtures, report)

    def ingest(self, season: str = CURRENT_SEASON) -> LiveIngestReport:
        """Fetch bootstrap-static and fixtures, land them, and refresh `season`."""
        season_id = self._season_id(season)
        report = LiveIngestReport(season=season)
        batch_id = self._open_batch("live_snapshot", season)
        report.batch_id = batch_id
        try:
            with FplApiClient(self.settings) as client:
                bootstrap = client.bootstrap_static()
                fixtures_result = client.fixtures()

            observed_at = bootstrap.fetched_at
            elements = bootstrap.payload["elements"]
            teams = bootstrap.payload["teams"]
            events = bootstrap.payload["events"]
            fixtures = fixtures_result.payload

            self._land(batch_id, "element", elements, "id", observed_at)
            self._land(batch_id, "team", teams, "id", observed_at)
            self._land(batch_id, "event", events, "id", observed_at)
            self._land(batch_id, "fixture", fixtures, "id", observed_at)

            self._apply(season_id, season, elements, teams, events, fixtures,
                       observed_at, report)

            self._close_batch(batch_id, row_count=report.rows_written)
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self._close_batch(batch_id, row_count=0, status="failed", error=str(exc)[:2000])
            self.conn.commit()
            raise

        if report.rows_written == 0:
            log.warning("live ingest batch %s wrote zero rows for %s -- "
                       "a quiet failure, not a quiet success", batch_id, season)
        log.info(report.summary())
        return report

    def replay(self, batch_id: int) -> LiveIngestReport:
        """Re-run the transform for a past batch from raw.document, no fetch.

        For fixing a transform bug against state we already have, not for
        catching up on time that passed -- see the module docstring.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT season FROM raw.ingest_batch WHERE batch_id=%s",
                (batch_id,),
            )
            batch = cur.fetchone()
        if batch is None:
            raise ValueError(f"no ingest batch {batch_id}")

        season = batch["season"]
        season_id = self._season_id(season)
        elements = self._documents(batch_id, "element")
        teams = self._documents(batch_id, "team")
        events = self._documents(batch_id, "event")
        fixtures = self._documents(batch_id, "fixture")
        if not elements:
            raise ValueError(f"batch {batch_id} has no landed documents to replay")

        report = LiveIngestReport(season=season, batch_id=batch_id)
        observed_at = self._observed_at(batch_id)
        try:
            self._apply(season_id, season, elements, teams, events, fixtures,
                       observed_at, report)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        log.info("replayed batch %s: %s", batch_id, report.summary())
        return report


def ingest_live(conn: psycopg.Connection, season: str = CURRENT_SEASON,
                settings: Settings = default_settings) -> LiveIngestReport:
    return LiveLoader(conn, settings).ingest(season)


def replay_batch(conn: psycopg.Connection, batch_id: int,
                 settings: Settings = default_settings) -> LiveIngestReport:
    return LiveLoader(conn, settings).replay(batch_id)
