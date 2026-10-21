"""The retrieval corpus must stay true to the database it describes.

Every claim in corpus/ is something the generator will act on, so each kind of
claim has a test that fails when it stops being true:

  * a documented column the view no longer has, or a column the docs never
    mention -- the model guesses at an undescribed column, and nothing
    records that it guessed;
  * an example whose SQL the validator would refuse, or which no longer runs --
    a few-shot example that cannot execute teaches the model to write SQL that
    cannot execute;
  * a resolved player id that find_player() no longer returns for that name,
    or an "ambiguous" name that has stopped being ambiguous.

The static half runs anywhere. The half that needs the database is marked
`database`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fplq import corpus
from fplq.corpus import CorpusError
from fplq.validate import Validator

C = corpus.load()
V = Validator()

ANSWERS = [e for e in C.examples if e.kind == "answer"]
ALL_SQL = [(e.id, e.sql) for e in ANSWERS] + [
    (f"{e.id} (alternative)", e.alternative_sql) for e in C.examples if e.alternative_sql
]


# --- shape --------------------------------------------------------------------


def test_every_kind_of_response_is_represented() -> None:
    assert {e.kind for e in C.examples} == corpus.EXAMPLE_KINDS


def test_chunk_ids_are_unique_and_cover_everything() -> None:
    chunks = corpus.chunks(C)
    assert len({c.id for c in chunks}) == len(chunks)
    assert len(chunks) == len(C.views) + len(C.functions) + len(C.terms) + len(C.examples)


@pytest.mark.parametrize(("eid", "sql"), ALL_SQL, ids=[eid for eid, _ in ALL_SQL])
def test_example_sql_passes_the_static_checks(eid: str, sql: str) -> None:
    assert V.check(sql).sql


@pytest.mark.parametrize("example", ANSWERS, ids=[e.id for e in ANSWERS])
def test_examples_never_match_player_names_themselves(example: corpus.Example) -> None:
    """The examples are what the model imitates. If one matched a name with
    LIKE, the model would learn to, and an ambiguous name would match many
    players without anyone being asked which they meant."""
    sql = example.sql.lower()
    assert "player_search" not in sql
    assert "find_player" not in sql
    assert "player_name =" not in sql
    for player in example.players:
        assert str(player.player_id) in sql, f"{player.name} is resolved but unused"


# --- the loader refuses what the generator could not use ----------------------


def _write_corpus(tmp: Path, *, view: str = "", term: str = "", example: str = "") -> Path:
    (tmp / "schema.yaml").write_text(
        "players: Players arrive resolved.\n"
        "views:\n"
        + (view or "  - {name: analytics.v, description: d, grain: g, columns: {a: A}}\n")
    )
    (tmp / "glossary.yaml").write_text(
        "terms:\n" + (term or "  - {term: t, meaning: m, compute: c, in_data: true}\n")
    )
    (tmp / "examples.yaml").write_text(
        "examples:\n"
        + (example or "  - {id: e, kind: answer, question: q, teaches: [x], sql: SELECT 1}\n")
    )
    return tmp


def test_a_minimal_corpus_loads(tmp_path: Path) -> None:
    assert corpus.load(_write_corpus(tmp_path)).examples[0].sql == "SELECT 1"


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ({"view": "  - {name: analytics.v, description: d, grain: g, columns: {a: ''}}\n"},
         "without a description"),
        ({"term": "  - {term: t, meaning: m, compute: c, in_data: false}\n"},
         "no 'instead'"),
        ({"term": "  - {term: t, meaning: m, compute: c, in_data: maybe}\n"},
         "in_data must be"),
        ({"example": "  - {id: e, kind: answer, question: q, teaches: [x]}\n"},
         "an answer needs sql"),
        ({"example": "  - {id: e, kind: refuse, question: q, teaches: [x], response: r, sql: SELECT 1}\n"},
         "only an answer has sql"),
        ({"example": "  - {id: e, kind: clarify, question: q, teaches: [x], response: r}\n"},
         "ambiguous_name"),
        ({"example": "  - {id: e, kind: guess, question: q, teaches: [x]}\n"},
         "kind must be"),
        ({"example": "  - {id: e, kind: answer, question: q, teaches: [x], sql: SELECT 1}\n"
                     "  - {id: e, kind: answer, question: q, teaches: [x], sql: SELECT 2}\n"},
         "duplicate example id"),
    ],
)
def test_the_loader_refuses_a_corpus_the_generator_could_not_use(
    tmp_path: Path, parts: dict[str, str], message: str
) -> None:
    with pytest.raises(CorpusError, match=message):
        corpus.load(_write_corpus(tmp_path, **parts))


# --- against the live database ------------------------------------------------


@pytest.mark.database
def test_schema_docs_match_the_live_views_exactly() -> None:
    from fplq.validate import reader_connection

    with reader_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.table_name, c.column_name
            FROM information_schema.columns c
            JOIN information_schema.views v
              ON v.table_schema = c.table_schema AND v.table_name = c.table_name
            WHERE c.table_schema = 'analytics'
            ORDER BY c.table_name, c.ordinal_position
            """
        ).fetchall()

    live: dict[str, list[str]] = {}
    for row in rows:
        live.setdefault(f"analytics.{row['table_name']}", []).append(row["column_name"])
    documented = {v.name: list(v.columns) for v in C.views}

    assert sorted(documented) == sorted(live), "a view is documented but missing, or the reverse"
    for name, columns in live.items():
        assert set(documented[name]) == set(columns), name


@pytest.mark.database
def test_documented_functions_exist() -> None:
    from fplq.validate import reader_connection

    with reader_connection() as conn:
        live = {
            row["proname"]
            for row in conn.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'analytics'"
            ).fetchall()
        }
    for f in C.functions:
        assert f.name.removeprefix("analytics.").split("(")[0] in live, f.name


@pytest.mark.database
@pytest.mark.parametrize(("eid", "sql"), ALL_SQL, ids=[eid for eid, _ in ALL_SQL])
def test_example_sql_runs_as_the_reader_and_returns_rows(eid: str, sql: str) -> None:
    """Through the full gate, cost check included, then executed as the role
    generated SQL runs as. An empty result is a failure: an example that
    returns nothing has usually stopped meaning what its question says."""
    from fplq.validate import reader_connection

    with reader_connection() as conn:
        validated = V.validate(sql, conn=conn)
        rows = conn.execute(validated.sql).fetchall()
    assert rows, f"{eid} returned no rows"


RESOLVED = [(e.id, p) for e in C.examples for p in e.players]


def _best_matches(conn, name: str) -> list[dict]:
    rows = conn.execute(
        "SELECT player_id, match_rank FROM analytics.find_player(%s)", (name,)
    ).fetchall()
    best = min((r["match_rank"] for r in rows), default=None)
    return [r for r in rows if r["match_rank"] == best]


@pytest.mark.database
@pytest.mark.parametrize(("eid", "player"), RESOLVED, ids=[f"{e}:{p.name}" for e, p in RESOLVED])
def test_resolved_players_still_resolve_to_the_same_id(eid: str, player: corpus.ResolvedPlayer) -> None:
    from fplq.validate import reader_connection

    with reader_connection() as conn:
        best = _best_matches(conn, player.name)
    assert [r["player_id"] for r in best] == [player.player_id], (
        f"{eid}: '{player.name}' no longer resolves uniquely to {player.player_id}"
    )


CLARIFY = [e for e in C.examples if e.kind == "clarify"]


@pytest.mark.database
@pytest.mark.parametrize("example", CLARIFY, ids=[e.id for e in CLARIFY])
def test_ambiguous_names_are_still_ambiguous(example: corpus.Example) -> None:
    from fplq.validate import reader_connection

    with reader_connection() as conn:
        best = _best_matches(conn, example.ambiguous_name)
    assert len(best) > 1, f"'{example.ambiguous_name}' now resolves to one player"
