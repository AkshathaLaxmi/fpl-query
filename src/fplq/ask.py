"""Question in, answer out: the NL-to-SQL pipeline (FPL-10).

    retrieve    glossary terms and worked examples nearest the question (pgvector)
    understand  Claude: answer, clarify or refuse? which players are named?
    resolve     code: find_player() per name -> one id, or stop and ask the user
    generate    Claude: SQL from the question, the schema, the reference material
                and the resolved player_ids
    validate    the validator, cost gate included; one retry with the error fed back
    execute     as fplq_reader

Player names are resolved by code between the two model calls, never by the
model writing SQL. The model cannot call find_player (the validator refuses
it), and it is told to filter on the ids it is given. So whether "Silva" is
ambiguous is decided here, deterministically, before any SQL exists.

The schema documentation goes into every generation prompt whole, rather than
being retrieved: it is small, and a retrieval miss on a view would produce SQL
that names columns the model had to guess. Retrieval picks the glossary terms
and examples, which is where the corpus is large and the relevance varies.

Every model call and the retrieval are recorded on the Answer's trace -- prompt
version, model, tokens, latency, chunks retrieved -- and logged as one JSON
line, so a golden-set run can compare prompt versions call by call.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import psycopg

from fplq import corpus as corpus_mod
from fplq.llm import Chat, load_prompt
from fplq.retrieval import Embedder, Hit, search
from fplq.validate import ValidationError, Validator

log = logging.getLogger(__name__)

UNDERSTAND_PROMPT = "understand.v1"
GENERATE_PROMPT = "generate.v1"

REFERENCE_KINDS = ("term", "example")
REFERENCE_K = 8
MAX_PLAYERS = 10
MAX_ATTEMPTS = 2  # the first try and one correction

UNDERSTAND_TOOL = {
    "name": "record_understanding",
    "description": "Record what kind of response the question needs and the players it names.",
    "inputSchema": {"json": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["answer", "clarify", "refuse"]},
            "players": {"type": "array", "items": {"type": "string"},
                        "description": "Players named in the question, fullest form given."},
            "message": {"type": "string",
                        "description": "For clarify or refuse: the reply the user sees."},
        },
        "required": ["kind", "players", "message"],
    }},
}

SUBMIT_TOOL = {
    "name": "submit_sql",
    "description": "Submit the SQL that answers the question, or decline.",
    "inputSchema": {"json": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["sql", "refuse"]},
            "sql": {"type": "string", "description": "The query, when kind is sql."},
            "explanation": {"type": "string",
                            "description": "For the user: what it computes and the assumptions."},
        },
        "required": ["kind", "sql", "explanation"],
    }},
}


@dataclass(frozen=True)
class ResolvedPlayer:
    mention: str
    player_id: int
    player_name: str
    full_name: str
    team: str | None


@dataclass
class Trace:
    retrieval: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def record_call(self, stage: str, prompt: str, result: Any) -> None:
        self.calls.append({
            "stage": stage, "prompt": prompt, "model": result.model,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "latency_ms": result.latency_ms,
        })

    @property
    def tokens(self) -> tuple[int, int]:
        return (sum(c["input_tokens"] for c in self.calls),
                sum(c["output_tokens"] for c in self.calls))


@dataclass
class Answer:
    question: str
    kind: str  # 'answer' | 'clarify' | 'refuse' | 'error'
    message: str = ""
    explanation: str = ""
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    players: list[ResolvedPlayer] = field(default_factory=list)
    trace: Trace = field(default_factory=Trace)
    elapsed_ms: int = 0


# -- model output is untrusted: check its shape before acting on it ----------


def _checked_understanding(raw: dict[str, Any]) -> tuple[str, list[str], str]:
    kind = raw.get("kind")
    if kind not in ("answer", "clarify", "refuse"):
        raise ValueError(f"understand returned an unknown kind: {kind!r}")
    players = raw.get("players") or []
    if not isinstance(players, list) or not all(isinstance(p, str) for p in players):
        raise ValueError("understand returned players that are not a list of names")
    names = list(dict.fromkeys(p.strip() for p in players if p.strip()))
    if len(names) > MAX_PLAYERS or any(len(p) > 80 for p in names):
        raise ValueError("understand returned an implausible list of players")
    return kind, names, str(raw.get("message") or "").strip()[:600]


def _checked_submission(raw: dict[str, Any]) -> tuple[str, str, str]:
    kind = raw.get("kind")
    if kind not in ("sql", "refuse"):
        raise ValueError(f"generate returned an unknown kind: {kind!r}")
    return kind, str(raw.get("sql") or "").strip(), str(raw.get("explanation") or "").strip()[:1000]


# -- stages ------------------------------------------------------------------


def resolve_players(reader: psycopg.Connection, mentions: list[str]
                    ) -> tuple[list[ResolvedPlayer], str | None]:
    """Each name to exactly one player, or a question for the user.

    Unique best match -> resolved. Several players tied for the best match ->
    ask which one. No match -> say so. Never pick the first of a tie: that is
    the silent wrong answer the whole design exists to prevent.
    """
    resolved: list[ResolvedPlayer] = []
    for mention in mentions:
        rows = reader.execute(
            "SELECT player_id, player_name, full_name, current_team, last_season, match_rank "
            "FROM analytics.find_player(%s, 25)", (mention,),
        ).fetchall()
        if not rows:
            return resolved, (f"I couldn't find a player called \"{mention}\" in the data "
                              f"(2019-20 onwards). Could you check the spelling?")
        best = min(r["match_rank"] for r in rows)
        top = [r for r in rows if r["match_rank"] == best]
        if len(top) > 1:
            options = "; ".join(f"{r['full_name']} ({r['current_team']}, {r['last_season']})"
                                for r in top[:5])
            more = f", and {len(top) - 5} more" if len(top) > 5 else ""
            return resolved, (f"Several players match \"{mention}\": {options}{more}. "
                              "Which one do you mean?")
        r = top[0]
        resolved.append(ResolvedPlayer(mention, r["player_id"], r["player_name"],
                                       r["full_name"], r["current_team"]))
    return resolved, None


def _reference_text(hits: list[Hit]) -> str:
    return "\n\n---\n\n".join(h.content for h in hits)


def _schema_text(corpus: corpus_mod.Corpus) -> str:
    parts = [c.text for c in corpus_mod.chunks(corpus) if c.kind in ("view", "function")]
    return corpus.players_note + "\n\n" + "\n\n---\n\n".join(parts)


def _players_text(players: list[ResolvedPlayer]) -> str:
    if not players:
        return "None named."
    return "\n".join(f"- {p.mention} = player_id {p.player_id} ({p.full_name}, {p.team})"
                     for p in players)


def _generation_message(question: str, current_season: str, schema: str, reference: str,
                        players: list[ResolvedPlayer], feedback: str | None) -> str:
    sections = [
        f"Today's date: {date.today().isoformat()}\nCurrent season: {current_season}",
        f"## Schema\n\n{schema}",
        f"## Reference material\n\n{reference}",
        f"## Players named in the question\n\n{_players_text(players)}",
        f"## Question\n\n{question}",
    ]
    if feedback:
        sections.append(f"## Your previous attempt was refused\n\n{feedback}")
    return "\n\n".join(sections)


def ask(question: str, *, chat: Chat, embedder: Embedder, writer: psycopg.Connection,
        reader: psycopg.Connection, corpus: corpus_mod.Corpus | None = None,
        validator: Validator | None = None) -> Answer:
    """Run one question through the pipeline. Never raises for a question the
    pipeline cannot answer -- that is a 'clarify', 'refuse' or 'error' Answer --
    only for infrastructure failures (Bedrock or the database unreachable)."""
    started = time.monotonic()
    corpus = corpus or corpus_mod.load()
    validator = validator or Validator()
    answer = Answer(question=question, kind="error")

    try:
        _run(answer, chat, embedder, writer, reader, corpus, validator)
    finally:
        answer.elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("ask %s", json.dumps({
            "question": question, "kind": answer.kind, "sql": answer.sql,
            "rows": len(answer.rows), "elapsed_ms": answer.elapsed_ms,
            "players": [asdict(p) for p in answer.players], **asdict(answer.trace),
        }, default=str))
    return answer


def _run(answer: Answer, chat: Chat, embedder: Embedder, writer: psycopg.Connection,
         reader: psycopg.Connection, corpus: corpus_mod.Corpus, validator: Validator) -> None:
    question = answer.question

    # retrieve
    hits = search(writer, embedder, question, k=REFERENCE_K, kinds=REFERENCE_KINDS)
    answer.trace.retrieval = [{"chunk": h.chunk_id, "distance": round(h.distance, 4)}
                              for h in hits]
    reference = _reference_text(hits)

    # understand
    prompt = load_prompt(UNDERSTAND_PROMPT)
    result = chat.call(prompt.text, [{"role": "user", "content": [{"text":
                       f"## Reference material\n\n{reference}\n\n## Question\n\n{question}"}]}],
                       UNDERSTAND_TOOL, max_tokens=400)
    answer.trace.record_call("understand", prompt.version, result)
    kind, mentions, message = _checked_understanding(result.input)
    if kind == "refuse":
        answer.kind, answer.message = kind, message
        return

    # resolve -- even when the model wants to clarify. Whether a name is
    # ambiguous is code's decision, and the resolver's question lists the real
    # candidates; the model, prompted by a retrieved example, may ask about a
    # name it has never checked.
    answer.players, clarification = resolve_players(reader, mentions)
    if clarification:
        answer.kind, answer.message = "clarify", clarification
        return
    if kind == "clarify":
        answer.kind, answer.message = kind, message
        return

    # generate -> validate -> execute, with one correction
    current_season = reader.execute(
        "SELECT MAX(season) AS s FROM analytics.fixture").fetchone()["s"]
    schema = _schema_text(corpus)
    prompt = load_prompt(GENERATE_PROMPT)
    feedback: str | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        message_text = _generation_message(question, current_season, schema, reference,
                                           answer.players, feedback)
        result = chat.call(prompt.text, [{"role": "user", "content": [{"text": message_text}]}],
                           SUBMIT_TOOL, max_tokens=1500)
        answer.trace.record_call("generate", prompt.version, result)
        kind, sql, explanation = _checked_submission(result.input)

        if kind == "refuse":
            answer.kind, answer.message = "refuse", explanation
            return

        try:
            validated = validator.validate(sql, conn=reader)
            cursor = reader.execute(validated.sql)
            answer.columns = [d.name for d in cursor.description]
            answer.rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r)
                           for r in cursor.fetchall()]
        except (ValidationError, psycopg.Error) as err:
            # An aborted transaction refuses everything after it until rolled back.
            reader.rollback()
            code = err.code if isinstance(err, ValidationError) else "database_error"
            detail = str(err).strip().splitlines()[0]
            answer.trace.attempts.append({"attempt": attempt, "sql": sql,
                                          "refused": code, "detail": detail})
            feedback = f"SQL:\n{sql}\n\nRefused ({code}): {detail}"
            continue

        answer.trace.attempts.append({"attempt": attempt, "sql": validated.sql,
                                      "cost": validated.estimated_cost})
        answer.kind, answer.sql, answer.explanation = "answer", validated.sql, explanation
        return

    answer.kind = "error"
    answer.message = ("I couldn't write a query for that which passed the safety checks. "
                      "Try rephrasing, or narrowing it to a season or a team.")
