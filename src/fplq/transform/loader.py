"""Transform raw archive files into the warehouse.

Shape of the pipeline, per season:

    teams.csv       -> core.team, core.team_season
    players_raw.csv -> core.player, core.player_source_id, core.player_season
    fixtures.csv    -> core.gameweek, core.fixture
    merged_gw.csv   -> core.player_fixture_stat
                       -> core.player_price_history      (derived)
                       -> core.player_ownership_snapshot (derived)

Two properties are non-negotiable and are what most of the code below is for:

  * **Idempotent.** Running the loader twice produces the same database, not two
    copies of it. Every write is an upsert on a natural key. This matters more
    than it sounds: a scheduled ingestion that cannot safely be re-run is a
    pipeline you are afraid of, and being afraid of your pipeline is how data
    rots.
  * **Ordered.** Identity is established before facts reference it. Players
    before player-seasons, fixtures before stats. The season is loaded inside a
    single transaction, so a half-loaded season is not a state that exists.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from fplq.config import Settings
from fplq.config import settings as default_settings
from fplq.ingest.archive import (
    ArchiveClient,
    ArchiveFile,
    parse_fixture_row,
    parse_gameweek_row,
    parse_player_row,
    parse_team_row,
    start_price_from,
)
from fplq.resolve.names import archive_gw_name, name_key
from fplq.resolve.players import PlayerResolver, load_overrides

log = logging.getLogger(__name__)


@dataclass
class LoadReport:
    season: str
    teams: int = 0
    players_created: int = 0
    players_linked: int = 0
    player_seasons: int = 0
    gameweeks: int = 0
    fixtures: int = 0
    stats: int = 0
    price_intervals: int = 0
    ownership_snapshots: int = 0
    unmatched: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.season}: {self.teams} teams, "
            f"{self.players_created} new players ({self.players_linked} linked), "
            f"{self.fixtures} fixtures, {self.stats} player-match rows, "
            f"{self.price_intervals} price intervals, "
            f"{len(self.unmatched)} unmatched"
        )


class SeasonLoader:
    def __init__(self, conn: psycopg.Connection, settings: Settings = default_settings) -> None:
        self.conn = conn
        self.settings = settings
        self.overrides = load_overrides(settings.overrides_path)

    # -- batch bookkeeping -------------------------------------------------

    def _open_batch(self, source: str, endpoint: str, season: str,
                    file: ArchiveFile | None = None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raw.ingest_batch
                    (source, endpoint, season, source_uri, content_sha256, row_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING batch_id
                """,
                (source, endpoint, season,
                 file.source_uri if file else None,
                 file.content_sha256 if file else None,
                 len(file.rows) if file else None),
            )
            return cur.fetchone()["batch_id"]

    def _close_batch(self, batch_id: int, *, status: str = "succeeded",
                     error: str | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE raw.ingest_batch SET status=%s, completed_at=now(), error=%s "
                "WHERE batch_id=%s",
                (status, error, batch_id),
            )

    # -- reference ---------------------------------------------------------

    def season_id(self, season: str) -> int:
        start_year = int(season.split("-")[0])
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.season (name, start_year)
                VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET start_year = EXCLUDED.start_year
                RETURNING season_id
                """,
                (season, start_year),
            )
            return cur.fetchone()["season_id"]

    # -- teams -------------------------------------------------------------

    def load_teams(self, season_id: int, file: ArchiveFile,
                   report: LoadReport) -> dict[int, int]:
        """Returns {fpl_team_id (per-season 1..20) -> team_id}."""
        mapping: dict[int, int] = {}
        with self.conn.cursor() as cur:
            for raw in file.rows:
                row = parse_team_row(raw)
                if not row["name"]:
                    continue
                # fpl_team_code is stable across seasons; the per-season id is not.
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

    # -- players -----------------------------------------------------------

    def load_players(self, season_id: int, season: str, file: ArchiveFile,
                     team_map: dict[int, int], report: LoadReport) -> dict[int, int]:
        """Returns {fpl_element_id -> player_id} for this season.

        players_raw carries FPL's stable `code`, so this rung of the cascade is
        almost always the authoritative one. The resolver exists for the rows
        where it is missing, and for the other sources we will add later.
        """
        code_index = self._existing_code_index()
        resolver = PlayerResolver.from_rows(self._existing_players(), self.overrides)

        element_map: dict[int, int] = {}
        with self.conn.cursor() as cur:
            for raw in file.rows:
                row = parse_player_row(raw)
                element_id = row["fpl_element_id"]
                if element_id is None:
                    continue

                code = row["fpl_player_code"]
                full_name = str(row["full_name"]) or str(row["display_name"])

                player_id: int | None = None
                method = "code"
                score: float | None = 100.0

                if code is not None and code in code_index:
                    player_id = code_index[code]
                elif code is None:
                    match = resolver.resolve(full_name)
                    player_id, method, score = match.player_id, match.method, match.score
                    if player_id is None:
                        self._record_issue(cur, "unmatched_player", "fpl_archive",
                                           season, full_name, match.candidates)
                        report.unmatched.append(full_name)
                        continue

                # Always upsert the name, even for a player we already know.
                #
                # The obvious version skips this when the player already exists,
                # which makes the stored name depend on the order seasons were
                # loaded in. It really does: FPL's web_name for Mohamed Salah was
                # "Salah" in 2019-20 and is "M.Salah" now, so loading oldest-first
                # and newest-first produced different databases from identical
                # inputs. Row counts matched, so "idempotent" looked true while
                # the content was not.
                #
                # Seasons load in ascending order, so writing every time means the
                # most recent season wins, which is the name a person would use
                # today. Registration history stays in player_season regardless.
                if player_id is None:
                    report.players_created += 1
                else:
                    report.players_linked += 1

                cur.execute(
                    """
                    INSERT INTO core.player (
                        fpl_player_code, first_name, last_name,
                        display_name, full_name, normalised_name, birth_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fpl_player_code) DO UPDATE
                        SET display_name    = EXCLUDED.display_name,
                            full_name       = EXCLUDED.full_name,
                            first_name      = EXCLUDED.first_name,
                            last_name       = EXCLUDED.last_name,
                            normalised_name = EXCLUDED.normalised_name,
                            birth_date      = COALESCE(EXCLUDED.birth_date,
                                                       core.player.birth_date)
                    RETURNING player_id
                    """,
                    (code, row["first_name"], row["last_name"],
                     row["display_name"], full_name, name_key(full_name),
                     row["birth_date"]),
                )
                returned = cur.fetchone()["player_id"]
                # For a code-less player matched by name the upsert has no
                # conflict target to hit, so keep the id the resolver chose.
                player_id = returned if code is not None else player_id
                if code is not None:
                    code_index[code] = player_id

                # The crosswalk. Season-scoped, because element ids are reused.
                cur.execute(
                    """
                    INSERT INTO core.player_source_id
                        (player_id, source, season_id, source_id, match_method, match_score)
                    VALUES (%s, 'fpl', %s, %s, %s, %s)
                    ON CONFLICT (source, season_id, source_id) DO UPDATE
                        SET player_id = EXCLUDED.player_id,
                            match_method = EXCLUDED.match_method,
                            match_score  = EXCLUDED.match_score
                    """,
                    (player_id, season_id, str(element_id), method, score),
                )

                cur.execute(
                    """
                    INSERT INTO core.player_season (
                        player_id, season_id, team_id, position_id,
                        fpl_element_id, start_price, end_price, squad_number)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (player_id, season_id) DO UPDATE SET
                        team_id     = EXCLUDED.team_id,
                        position_id = EXCLUDED.position_id,
                        fpl_element_id = EXCLUDED.fpl_element_id,
                        start_price = EXCLUDED.start_price,
                        end_price   = EXCLUDED.end_price,
                        squad_number = EXCLUDED.squad_number
                    """,
                    (player_id, season_id,
                     team_map.get(int(row["fpl_team_id"])) if row["fpl_team_id"] else None,
                     row["position_id"], element_id,
                     start_price_from(row), row["end_price"], row["squad_number"]),
                )
                element_map[int(element_id)] = player_id
                report.player_seasons += 1

        return element_map

    def _existing_players(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT player_id, display_name, full_name FROM core.player")
            return cur.fetchall()

    def _existing_code_index(self) -> dict[int, int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT fpl_player_code, player_id FROM core.player "
                "WHERE fpl_player_code IS NOT NULL"
            )
            return {r["fpl_player_code"]: r["player_id"] for r in cur.fetchall()}

    @staticmethod
    def _record_issue(cur: psycopg.Cursor, issue_type: str, source: str,
                      season: str, raw_value: str,
                      candidates: list[dict[str, object]] | None = None) -> None:
        import json

        cur.execute(
            """
            INSERT INTO core.resolution_issue
                (issue_type, source, season, raw_value, candidates)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (issue_type, source, season, raw_value,
             json.dumps(candidates) if candidates else None),
        )

    # -- fixtures ----------------------------------------------------------

    def load_fixtures(self, season_id: int, file: ArchiveFile,
                      team_map: dict[int, int], report: LoadReport
                      ) -> tuple[dict[int, int], dict[int, int],
                                 dict[int, datetime], datetime | None]:
        """Returns (fpl_fixture_id -> fixture_id, gw_number -> gameweek_id,
        gw_number -> deadline, last kickoff of the season).

        The deadline is derived as one hour before the first kickoff of the
        gameweek. The archive's fixtures file does not carry deadlines, and the
        real FPL deadline is 90 minutes before the first match -- but derived
        consistently is enough for as-of joins, and the live API ingestion
        overwrites it with the true value for current seasons.
        """
        parsed = [parse_fixture_row(r) for r in file.rows]

        first_kickoff: dict[int, datetime] = {}
        for row in parsed:
            gw, kickoff = row["gameweek_number"], row["kickoff_at"]
            if gw is None or kickoff is None:
                continue
            if gw not in first_kickoff or kickoff < first_kickoff[gw]:
                first_kickoff[gw] = kickoff

        gameweek_map: dict[int, int] = {}
        deadlines: dict[int, datetime] = {}
        with self.conn.cursor() as cur:
            for gw in sorted(first_kickoff):
                deadline = first_kickoff[gw] - timedelta(hours=1)
                cur.execute(
                    """
                    INSERT INTO core.gameweek (season_id, gameweek_number, deadline_at, is_finished)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (season_id, gameweek_number) DO UPDATE
                        SET deadline_at = COALESCE(core.gameweek.deadline_at, EXCLUDED.deadline_at),
                            is_finished = EXCLUDED.is_finished
                    RETURNING gameweek_id, deadline_at
                    """,
                    (season_id, gw, deadline,
                     first_kickoff[gw] < datetime.now(UTC)),
                )
                got = cur.fetchone()
                gameweek_map[gw] = got["gameweek_id"]
                deadlines[gw] = got["deadline_at"]
                report.gameweeks += 1

            fixture_map: dict[int, int] = {}
            for row in parsed:
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
                    RETURNING fixture_id
                    """,
                    (season_id, gameweek_map.get(row["gameweek_number"]),
                     row["fpl_fixture_id"], row["kickoff_at"], home, away,
                     row["home_score"], row["away_score"],
                     row["home_difficulty"], row["away_difficulty"],
                     row["is_finished"]),
                )
                fixture_map[int(row["fpl_fixture_id"])] = cur.fetchone()["fixture_id"]
                report.fixtures += 1

        kickoffs = [r["kickoff_at"] for r in parsed if r["kickoff_at"] is not None]
        season_end = max(kickoffs) if kickoffs else None
        return fixture_map, gameweek_map, deadlines, season_end

    # -- facts -------------------------------------------------------------

    def load_gameweek_stats(self, season_id: int, season: str, file: ArchiveFile,
                            element_map: dict[int, int], team_map: dict[int, int],
                            fixture_map: dict[int, int], gameweek_map: dict[int, int],
                            deadlines: dict[int, datetime],
                            season_end: datetime | None,
                            report: LoadReport) -> None:
        resolver = PlayerResolver.from_rows(self._existing_players(), self.overrides)
        position_by_code = {"GK": 1, "GKP": 1, "DEF": 2, "MID": 3, "FWD": 4, "AM": 5}

        # Collected for the derived history tables, keyed by player.
        prices: dict[int, list[tuple[int, float]]] = defaultdict(list)
        ownership: dict[int, list[tuple[int, int, int | None, int | None]]] = defaultdict(list)

        with self.conn.cursor() as cur:
            for raw in file.rows:
                row = parse_gameweek_row(raw)
                element_id = row["fpl_element_id"]
                gw = row["gameweek_number"]
                if element_id is None or gw is None:
                    continue

                player_id = element_map.get(int(element_id))
                if player_id is None:
                    # Element not in players_raw -- happens for players who left
                    # mid-season in some archive seasons. Fall back to the name.
                    match = resolver.resolve(archive_gw_name(str(row["name"])))
                    if not match.matched:
                        self._record_issue(cur, "unmatched_player", "fpl_archive",
                                           season, str(row["name"]), match.candidates)
                        report.unmatched.append(str(row["name"]))
                        continue
                    player_id = match.player_id

                fixture_id = fixture_map.get(int(row["fpl_fixture_id"])) \
                    if row["fpl_fixture_id"] is not None else None
                if fixture_id is None:
                    self._record_issue(cur, "missing_fixture", "fpl_archive", season,
                                       f"element={element_id} gw={gw} "
                                       f"fixture={row['fpl_fixture_id']}")
                    continue

                opponent_id = team_map.get(row["opponent_fpl_team_id"]) \
                    if row["opponent_fpl_team_id"] is not None else None
                # The fact row's team is derived from the fixture, not from the
                # player's season registration -- a January transfer means those
                # two disagree, and the fixture is the one that is true.
                team_id = self._team_from_fixture(cur, fixture_id, row["was_home"])

                position_id = position_by_code.get((row["position_code"] or "").upper()) \
                    or self._season_position(cur, player_id, season_id)

                cur.execute(
                    """
                    INSERT INTO core.player_fixture_stat (
                        player_id, season_id, gameweek_id, fixture_id, team_id,
                        opponent_team_id, position_id, was_home, kickoff_at,
                        minutes, starts, total_points, goals_scored, assists,
                        clean_sheets, goals_conceded, own_goals, penalties_saved,
                        penalties_missed, yellow_cards, red_cards, saves, bonus, bps,
                        influence, creativity, threat, ict_index,
                        expected_goals, expected_assists, expected_goal_involvements,
                        expected_goals_conceded, expected_points,
                        tackles, recoveries, clearances_blocks_interceptions,
                        defensive_contribution,
                        price_at_deadline, selected_at_deadline,
                        transfers_in, transfers_out)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    ON CONFLICT (player_id, fixture_id) DO UPDATE SET
                        total_points = EXCLUDED.total_points,
                        minutes      = EXCLUDED.minutes,
                        bonus        = EXCLUDED.bonus,
                        bps          = EXCLUDED.bps,
                        price_at_deadline    = EXCLUDED.price_at_deadline,
                        selected_at_deadline = EXCLUDED.selected_at_deadline
                    """,
                    (player_id, season_id, gameweek_map.get(gw), fixture_id, team_id,
                     opponent_id, position_id, row["was_home"], row["kickoff_at"],
                     row["minutes"], row["starts"], row["total_points"],
                     row["goals_scored"], row["assists"], row["clean_sheets"],
                     row["goals_conceded"], row["own_goals"], row["penalties_saved"],
                     row["penalties_missed"], row["yellow_cards"], row["red_cards"],
                     row["saves"], row["bonus"], row["bps"],
                     row["influence"], row["creativity"], row["threat"], row["ict_index"],
                     row["expected_goals"], row["expected_assists"],
                     row["expected_goal_involvements"], row["expected_goals_conceded"],
                     row["expected_points"],
                     row["tackles"], row["recoveries"],
                     row["clearances_blocks_interceptions"], row["defensive_contribution"],
                     row["price_at_deadline"], row["selected_at_deadline"],
                     row["transfers_in"], row["transfers_out"]),
                )
                report.stats += 1

                if row["price_at_deadline"] is not None:
                    prices[player_id].append((gw, float(row["price_at_deadline"])))
                if row["selected_at_deadline"] is not None:
                    ownership[player_id].append(
                        (gw, int(row["selected_at_deadline"]),
                         row["transfers_in"], row["transfers_out"])
                    )

        self._build_price_history(season_id, prices, deadlines, season_end, report)
        self._build_ownership(season_id, ownership, deadlines, gameweek_map, report)

    @staticmethod
    def _team_from_fixture(cur: psycopg.Cursor, fixture_id: int,
                           was_home: bool | None) -> int | None:
        if was_home is None:
            return None
        cur.execute(
            "SELECT home_team_id, away_team_id FROM core.fixture WHERE fixture_id=%s",
            (fixture_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["home_team_id"] if was_home else row["away_team_id"]

    @staticmethod
    def _season_position(cur: psycopg.Cursor, player_id: int, season_id: int) -> int | None:
        cur.execute(
            "SELECT position_id FROM core.player_season WHERE player_id=%s AND season_id=%s",
            (player_id, season_id),
        )
        row = cur.fetchone()
        return row["position_id"] if row else None

    # -- derived history ---------------------------------------------------

    def _build_price_history(self, season_id: int,
                             prices: dict[int, list[tuple[int, float]]],
                             deadlines: dict[int, datetime],
                             season_end: datetime | None,
                             report: LoadReport) -> None:
        """Collapse per-gameweek prices into type-2 intervals.

        A player who is 7.0 for gameweeks 1-4 and 7.1 from gameweek 5 becomes two
        rows, not thirty-eight. Runs of the same price are merged, and the
        interval closes at the deadline of the gameweek where the price changed
        -- so an as-of lookup at any instant returns exactly one row.

        **Closing the final interval of a season matters.** The obvious
        implementation leaves the last run open (valid_to IS NULL, "still
        true"), which is right for the season in progress and catastrophically
        wrong for a finished one: an open interval from May 2025 overlaps every
        interval in every later season, and the exclusion constraint then
        rejects them. So a finished season's last interval is closed at the
        season's end, and only the current season stays open.

        That bug was real, and it was the exclusion constraint that caught it --
        which is the argument for having the database enforce the invariant
        rather than trusting the loader to maintain it.

        Gameweek grain is a real limitation and is documented as one: prices
        actually move daily. The live API's daily snapshot refines this for the
        current season; history stays at gameweek resolution, which is the
        resolution the archive genuinely has. Claiming finer would be inventing
        precision.
        """
        with self.conn.cursor() as cur:
            # Rebuild rather than merge: the exclusion constraint makes partial
            # overwrites fiddly, and the source is a full season file anyway.
            cur.execute(
                "DELETE FROM core.player_price_history WHERE season_id=%s AND source='fpl_archive'",
                (season_id,),
            )
            # A season is over once its last kickoff is in the past. Only then
            # is it safe -- and correct -- to close the final interval.
            season_over = season_end is not None and season_end < datetime.now(UTC)

            for player_id, observations in prices.items():
                runs: list[tuple[int, int, float]] = []   # (from_gw, to_gw, price)
                for gw, price in sorted(set(observations)):
                    if runs and abs(runs[-1][2] - price) < 1e-9:
                        runs[-1] = (runs[-1][0], gw, price)
                    else:
                        runs.append((gw, gw, price))

                for index, (from_gw, _to_gw, price) in enumerate(runs):
                    valid_from = deadlines.get(from_gw)
                    if valid_from is None:
                        continue

                    if index + 1 < len(runs):
                        valid_to = deadlines.get(runs[index + 1][0])
                    else:
                        # Last run of the season: close it if the season is done,
                        # leave it open if the season is still being played.
                        valid_to = season_end if season_over else None

                    if valid_to is not None and valid_to <= valid_from:
                        continue

                    # No ON CONFLICT here, deliberately. Swallowing an exclusion
                    # violation is what let the open-interval bug above stay
                    # invisible: rows vanished and the load still reported
                    # success. Overlapping history is a bug in this function and
                    # should fail the load, loudly.
                    cur.execute(
                        """
                        INSERT INTO core.player_price_history
                            (player_id, season_id, price, valid_from, valid_to, source)
                        VALUES (%s, %s, %s, %s, %s, 'fpl_archive')
                        """,
                        (player_id, season_id, price, valid_from, valid_to),
                    )
                    report.price_intervals += 1

    def _build_ownership(self, season_id: int,
                         ownership: dict[int, list[tuple[int, int, int | None, int | None]]],
                         deadlines: dict[int, datetime],
                         gameweek_map: dict[int, int],
                         report: LoadReport) -> None:
        with self.conn.cursor() as cur:
            for player_id, observations in ownership.items():
                for gw, selected, transfers_in, transfers_out in sorted(set(observations)):
                    snapshot_at = deadlines.get(gw)
                    if snapshot_at is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO core.player_ownership_snapshot
                            (player_id, season_id, snapshot_at, gameweek_id,
                             selected_by, transfers_in, transfers_out)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (player_id, snapshot_at) DO UPDATE SET
                            selected_by   = EXCLUDED.selected_by,
                            transfers_in  = EXCLUDED.transfers_in,
                            transfers_out = EXCLUDED.transfers_out
                        """,
                        (player_id, season_id, snapshot_at, gameweek_map.get(gw),
                         selected, transfers_in, transfers_out),
                    )
                    report.ownership_snapshots += 1

    # -- orchestration -----------------------------------------------------

    def load_season(self, season: str, files: dict[str, ArchiveFile]) -> LoadReport:
        report = LoadReport(season=season)
        season_id = self.season_id(season)
        batch_id = self._open_batch("fpl_archive", "season", season,
                                    files.get("players_raw.csv"))
        try:
            team_map = self.load_teams(season_id, files["teams.csv"], report)
            element_map = self.load_players(season_id, season, files["players_raw.csv"],
                                            team_map, report)
            fixture_map, gameweek_map, deadlines, season_end = self.load_fixtures(
                season_id, files["fixtures.csv"], team_map, report
            )
            self.load_gameweek_stats(season_id, season, files["gws/merged_gw.csv"],
                                     element_map, team_map, fixture_map,
                                     gameweek_map, deadlines, season_end, report)
            self._close_batch(batch_id)
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self._close_batch(batch_id, status="failed", error=str(exc)[:2000])
            self.conn.commit()
            raise
        log.info(report.summary())
        return report


def load_seasons(conn: psycopg.Connection, seasons: list[str],
                 settings: Settings = default_settings) -> list[LoadReport]:
    """Fetch and load each season in chronological order.

    Order matters: a player's identity is created the first time we see them, so
    loading oldest-first means their canonical row carries the name they had
    when they arrived, and later seasons link rather than duplicate.
    """
    loader = SeasonLoader(conn, settings)
    reports: list[LoadReport] = []
    with ArchiveClient(settings) as client:
        for season in sorted(seasons):
            files = {f.filename: f for f in client.fetch_season(season)}
            reports.append(loader.load_season(season, files))
    return reports
