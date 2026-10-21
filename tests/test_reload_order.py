"""Reloading one season out of order must not roll back what later seasons wrote.

The warehouse is "most recent season wins" for a player's name. That used to
depend on `fplq load` running seasons in ascending order. These run against the loaded database, inside a
transaction that is rolled back, so they leave it as they found it.

Run with `pytest -m database`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from fplq.config import settings
from fplq.db import connect
from fplq.ingest.archive import ArchiveFile
from fplq.transform.loader import LoadReport, SeasonLoader

pytestmark = pytest.mark.database


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


@pytest.fixture
def writer() -> psycopg.Connection:
    # connect() commits on a clean exit; roll back first so nothing lands.
    with connect(settings.writer_dsn) as conn:
        yield conn
        conn.rollback()


_REGISTRATION = """
    SELECT p.player_id, p.fpl_player_code, p.first_name, p.last_name, ps.season_id,
           ps.team_id, ps.fpl_element_id, ps.position_id
    FROM core.player p
    JOIN core.player_season ps ON ps.player_id = p.player_id
    JOIN core.season s ON s.season_id = ps.season_id
    WHERE s.name = %s
"""


def _registration(conn: psycopg.Connection, season: str, where: str = "",
                  *params: object) -> dict[str, object]:
    with conn.cursor() as cur:
        cur.execute(_REGISTRATION + where + " ORDER BY p.player_id LIMIT 1",
                    (season, *params))
        row = cur.fetchone()
    if row is None:
        pytest.skip(f"no matching player registered in {season}")
    return row


def _load_player(conn: psycopg.Connection, player: dict[str, object], season: str,
                 web_name: str) -> None:
    raw = {"code": str(player["fpl_player_code"]), "id": str(player["fpl_element_id"]),
           "first_name": str(player["first_name"] or ""),
           "second_name": str(player["last_name"] or ""), "web_name": web_name,
           "element_type": str(player["position_id"]), "team": "99", "now_cost": "125"}
    file = ArchiveFile(season=season, filename="players_raw.csv", rows=[raw],
                       raw_uri="", content_sha256="", fetched_at=datetime.now(UTC),
                       source_uri="")
    SeasonLoader(conn).load_players(int(player["season_id"]), season, file,
                                    {99: int(player["team_id"])}, LoadReport(season=season))


def _display_name(conn: psycopg.Connection, player_id: object) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT display_name FROM core.player WHERE player_id=%s", (player_id,))
        return cur.fetchone()["display_name"]


@requires_db
def test_reloading_an_old_season_keeps_the_current_name(writer: psycopg.Connection) -> None:
    """FPL called him "Salah" in 2019-20 and "M.Salah" now."""
    salah = _registration(writer, "2019-20", " AND p.full_name = 'Mohamed Salah'")
    before = _display_name(writer, salah["player_id"])
    _load_player(writer, salah, "2019-20", "Salah")
    assert _display_name(writer, salah["player_id"]) == before


@requires_db
def test_the_latest_season_still_renames(writer: psycopg.Connection) -> None:
    """The guard must not freeze names: the newest season still wins."""
    with writer.cursor() as cur:
        cur.execute("SELECT name FROM core.season ORDER BY start_year DESC LIMIT 1")
        latest = cur.fetchone()["name"]
    player = _registration(writer, latest)
    _load_player(writer, player, latest, "Not.Their.Name")
    assert _display_name(writer, player["player_id"]) == "Not.Their.Name"
