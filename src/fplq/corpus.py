"""The retrieval corpus: what the generator is told about the data.

Three files under corpus/, each kept by hand and reviewed like code:

    schema.yaml    the analytics schema, view by view and column by column
    glossary.yaml  FPL vocabulary, mapped onto that schema or marked as absent
    examples.yaml  questions with the SQL, clarification or refusal they get

They live in the repository rather than in the database because they are the
prompt, and the prompt is versioned: a change to an example is a diff someone
reviews, and every change will run the golden set (FPL-18).

This module loads them, refuses a malformed file loudly, and cuts them into
chunks for retrieval. One chunk is one thing a question might need: a view, a
function, a term, or an example. Cutting at those seams rather than at a fixed
token count means a retrieved chunk is never half a view description, and the
corpus is small enough (tens of chunks) that a chunk-size trade-off does not
arise. Embedding and indexing the chunks into pgvector is a separate step.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fplq.config import REPO_ROOT

CORPUS_DIR = REPO_ROOT / "corpus"

EXAMPLE_KINDS = frozenset({"answer", "clarify", "refuse"})


class CorpusError(ValueError):
    """A corpus file is missing something the generator would rely on."""


@dataclass(frozen=True)
class ViewDoc:
    name: str
    description: str
    grain: str
    columns: dict[str, str]
    pitfalls: tuple[str, ...]


@dataclass(frozen=True)
class FunctionDoc:
    name: str
    returns: str
    description: str
    usage: str


@dataclass(frozen=True)
class Term:
    term: str
    aliases: tuple[str, ...]
    meaning: str
    compute: str
    in_data: bool | str
    instead: str | None


@dataclass(frozen=True)
class ResolvedPlayer:
    name: str
    player_id: int


@dataclass(frozen=True)
class Example:
    id: str
    kind: str
    question: str
    teaches: tuple[str, ...]
    players: tuple[ResolvedPlayer, ...]
    sql: str | None
    response: str | None
    alternative_sql: str | None
    ambiguous_name: str | None


@dataclass(frozen=True)
class Corpus:
    players_note: str
    views: tuple[ViewDoc, ...]
    functions: tuple[FunctionDoc, ...]
    terms: tuple[Term, ...]
    examples: tuple[Example, ...]


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit. `id` is stable across edits, so a re-index
    replaces a chunk rather than adding a second copy of it."""

    id: str
    kind: str
    text: str


# -- loading -------------------------------------------------------------------


def _require(entry: dict[str, Any], key: str, where: str) -> Any:
    value = entry.get(key)
    if value in (None, "", [], {}):
        raise CorpusError(f"{where}: missing '{key}'")
    return value


def _read(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise CorpusError(f"{path.name}: expected a mapping at the top level")
    return data


def _view(entry: dict[str, Any]) -> ViewDoc:
    name = _require(entry, "name", "view")
    where = f"schema.yaml {name}"
    columns = _require(entry, "columns", where)
    blank = [col for col, text in columns.items() if not str(text or "").strip()]
    if blank:
        # An undescribed column is worse than an absent one: the model sees the
        # name, guesses the meaning, and nothing records that it guessed.
        raise CorpusError(f"{where}: columns without a description: {', '.join(blank)}")
    return ViewDoc(
        name=name,
        description=_require(entry, "description", where),
        grain=_require(entry, "grain", where),
        columns={str(k): str(v) for k, v in columns.items()},
        pitfalls=tuple(entry.get("pitfalls") or ()),
    )


def _function(entry: dict[str, Any]) -> FunctionDoc:
    name = _require(entry, "name", "function")
    where = f"schema.yaml {name}"
    return FunctionDoc(
        name=name,
        returns=_require(entry, "returns", where),
        description=_require(entry, "description", where),
        usage=_require(entry, "usage", where),
    )


def _term(entry: dict[str, Any]) -> Term:
    term = _require(entry, "term", "glossary term")
    where = f"glossary.yaml {term}"
    in_data = entry.get("in_data")
    if in_data not in (True, False, "partial"):
        raise CorpusError(f"{where}: in_data must be true, false or partial")
    instead = entry.get("instead")
    if in_data is False and not instead:
        # A term the data cannot support must say what to do instead, or the
        # generator is left to improvise -- which is how a neighbouring question
        # gets answered as if it were the one asked.
        raise CorpusError(f"{where}: in_data is false but no 'instead' is given")
    return Term(
        term=term,
        aliases=tuple(entry.get("aliases") or ()),
        meaning=_require(entry, "meaning", where),
        compute=_require(entry, "compute", where),
        in_data=in_data,
        instead=instead,
    )


def _example(entry: dict[str, Any]) -> Example:
    eid = _require(entry, "id", "example")
    where = f"examples.yaml {eid}"
    kind = _require(entry, "kind", where)
    if kind not in EXAMPLE_KINDS:
        raise CorpusError(f"{where}: kind must be one of {sorted(EXAMPLE_KINDS)}")

    sql = entry.get("sql")
    response = entry.get("response")
    if kind == "answer" and not sql:
        raise CorpusError(f"{where}: an answer needs sql")
    if kind != "answer" and sql:
        raise CorpusError(f"{where}: only an answer has sql; use alternative_sql for a refusal")
    if kind != "answer" and not response:
        raise CorpusError(f"{where}: a {kind} needs a response")
    if kind == "clarify" and not entry.get("ambiguous_name"):
        raise CorpusError(f"{where}: a clarify example needs the ambiguous_name it is about")

    players = tuple(
        ResolvedPlayer(name=_require(p, "name", where), player_id=int(_require(p, "player_id", where)))
        for p in entry.get("players") or ()
    )
    return Example(
        id=eid,
        kind=kind,
        question=_require(entry, "question", where),
        teaches=tuple(_require(entry, "teaches", where)),
        players=players,
        sql=sql.strip() if sql else None,
        response=response,
        alternative_sql=(entry.get("alternative_sql") or "").strip() or None,
        ambiguous_name=entry.get("ambiguous_name"),
    )


def _unique(items: list[str], what: str) -> None:
    dupes = sorted(item for item, n in Counter(items).items() if n > 1)
    if dupes:
        raise CorpusError(f"duplicate {what}: {', '.join(dupes)}")


def load(directory: Path = CORPUS_DIR) -> Corpus:
    """Read and check all three files."""
    schema = _read(directory / "schema.yaml")
    glossary = _read(directory / "glossary.yaml")
    examples = _read(directory / "examples.yaml")

    corpus = Corpus(
        players_note=_require(schema, "players", "schema.yaml"),
        views=tuple(_view(v) for v in _require(schema, "views", "schema.yaml")),
        functions=tuple(_function(f) for f in schema.get("functions") or ()),
        terms=tuple(_term(t) for t in _require(glossary, "terms", "glossary.yaml")),
        examples=tuple(_example(e) for e in _require(examples, "examples", "examples.yaml")),
    )
    _unique([v.name for v in corpus.views], "view")
    _unique([t.term for t in corpus.terms], "glossary term")
    _unique([e.id for e in corpus.examples], "example id")
    return corpus


# -- chunking ------------------------------------------------------------------


def _bullets(lines: tuple[str, ...] | list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def _view_text(v: ViewDoc) -> str:
    parts = [f"View {v.name} (one row per {v.grain}).", v.description, "", "Columns:"]
    parts += [f"- {col}: {text}" for col, text in v.columns.items()]
    if v.pitfalls:
        parts += ["", "Pitfalls:", _bullets(v.pitfalls)]
    return "\n".join(parts)


def _function_text(f: FunctionDoc) -> str:
    return f"Function {f.name} returns {f.returns}.\n{f.description}\nUsage: {f.usage}"


def _term_text(t: Term) -> str:
    aliases = f" (also: {', '.join(t.aliases)})" if t.aliases else ""
    parts = [f"Term: {t.term}{aliases}.", f"Meaning: {t.meaning}"]
    if t.in_data is False:
        parts += [t.compute, f"Instead: {t.instead}"]
    else:
        parts.append(f"How to compute: {t.compute}")
        if t.in_data == "partial":
            parts.append("Only partly supported by the data; say what was approximated.")
    return "\n".join(parts)


def _example_text(e: Example) -> str:
    parts = [f"Question: {e.question}"]
    if e.players:
        resolved = ", ".join(f"{p.name} = player_id {p.player_id}" for p in e.players)
        parts.append(f"Resolved players: {resolved}")
    if e.kind == "answer":
        parts.append(f"SQL:\n{e.sql}")
    elif e.kind == "clarify":
        parts.append(f"Ask the user: {e.response}")
    else:
        parts.append(f"Decline: {e.response}")
        if e.alternative_sql:
            parts.append(f"Labelled alternative SQL:\n{e.alternative_sql}")
    parts.append("Why: " + " ".join(e.teaches))
    return "\n".join(parts)


def chunks(corpus: Corpus) -> list[Chunk]:
    """Cut the corpus into retrievable chunks, one per view, function, term
    and example. The players note is not a chunk: it applies to every
    question, so it belongs in the fixed part of the prompt, not in retrieval."""
    out = [Chunk(f"view:{v.name}", "view", _view_text(v)) for v in corpus.views]
    out += [Chunk(f"function:{f.name.split('(')[0]}", "function", _function_text(f))
            for f in corpus.functions]
    out += [Chunk(f"term:{t.term}", "term", _term_text(t)) for t in corpus.terms]
    out += [Chunk(f"example:{e.id}", "example", _example_text(e)) for e in corpus.examples]
    return out
