"""The ask pipeline, with Claude replaced by a script.

A scripted chat returns exactly the tool inputs a test needs, so these tests
pin the pipeline's own decisions -- what stops before generation, what gets
retried, what the model is shown -- and nothing about how well Claude writes
SQL. That is the golden set's job (FPL-17).

The pieces that need the database run inside transactions that are rolled
back: retrieval is indexed with a fake embedder inside the writer's
transaction, so the real index is untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from fplq import ask as ask_mod
from fplq.ask import (
    SUBMIT_TOOL,
    UNDERSTAND_TOOL,
    _checked_submission,
    _checked_understanding,
    ask,
    resolve_players,
)
from fplq.config import settings
from fplq.db import connect
from fplq.llm import BedrockChat, ToolResult, load_prompt
from fplq.retrieval import index
from tests.test_retrieval import FakeEmbedder


class ScriptedChat:
    """Returns the queued tool inputs in order and remembers what it was sent."""

    model = "scripted"

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.sent: list[tuple[str, str]] = []  # (tool name, user message text)

    def call(self, system: str, messages: list[dict[str, Any]], tool: dict[str, Any],
             max_tokens: int) -> ToolResult:
        self.sent.append((tool["name"], messages[0]["content"][0]["text"]))
        if not self.responses:
            raise AssertionError(f"unexpected {tool['name']} call")
        return ToolResult(self.responses.pop(0), self.model, 100, 20, 5)


def understood(kind: str = "answer", players: list[str] | None = None, message: str = ""):
    return {"kind": kind, "players": players or [], "message": message}


def submitted(sql: str, kind: str = "sql", explanation: str = "Explained."):
    return {"kind": kind, "sql": sql, "explanation": explanation}


# --- no database --------------------------------------------------------------


def test_both_prompts_exist_and_are_versioned_by_file_name() -> None:
    for version in (ask_mod.UNDERSTAND_PROMPT, ask_mod.GENERATE_PROMPT):
        prompt = load_prompt(version)
        assert prompt.version == version
        assert len(prompt.text) > 200


def test_tool_schemas_offer_exactly_the_kinds_the_pipeline_accepts() -> None:
    assert UNDERSTAND_TOOL["inputSchema"]["json"]["properties"]["kind"]["enum"] == [
        "answer", "clarify", "refuse"]
    assert SUBMIT_TOOL["inputSchema"]["json"]["properties"]["kind"]["enum"] == ["sql", "refuse"]


def test_understanding_is_checked_before_it_is_trusted() -> None:
    assert _checked_understanding(understood(players=[" Salah ", "Salah", "Saka"])) == (
        "answer", ["Salah", "Saka"], "")
    with pytest.raises(ValueError, match="unknown kind"):
        _checked_understanding(understood(kind="sql"))
    with pytest.raises(ValueError, match="not a list"):
        _checked_understanding({"kind": "answer", "players": "Salah", "message": ""})
    with pytest.raises(ValueError, match="implausible"):
        _checked_understanding(understood(players=[f"p{i}" for i in range(11)]))


def test_submission_is_checked_before_it_is_trusted() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        _checked_submission(submitted("SELECT 1", kind="answer"))


class _FakeConverse:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.response


def test_bedrock_call_forces_the_tool_at_temperature_zero_with_explicit_max_tokens() -> None:
    client = _FakeConverse({
        "stopReason": "tool_use",
        "output": {"message": {"content": [{"toolUse": {"name": "t", "toolUseId": "1",
                                                         "input": {"kind": "sql"}}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 3},
    })
    result = BedrockChat(client=client, model="m").call(
        "system", [{"role": "user", "content": [{"text": "q"}]}], {"name": "t"}, max_tokens=99)
    sent = client.requests[0]
    assert sent["toolConfig"]["toolChoice"] == {"tool": {"name": "t"}}
    assert sent["inferenceConfig"] == {"maxTokens": 99, "temperature": 0}
    assert (result.input, result.input_tokens, result.output_tokens) == ({"kind": "sql"}, 10, 3)


def test_bedrock_call_refuses_a_truncated_answer() -> None:
    client = _FakeConverse({"stopReason": "max_tokens", "output": {"message": {"content": []}}})
    with pytest.raises(RuntimeError, match="did not complete"):
        BedrockChat(client=client, model="m").call("s", [], {"name": "t"}, max_tokens=5)


# --- against the live database ------------------------------------------------


@pytest.fixture
def conns():
    from fplq.validate import reader_connection

    with connect(settings.writer_dsn) as writer, reader_connection() as reader:
        writer.execute("DELETE FROM retrieval.chunk")
        index(writer, FakeEmbedder())
        yield writer, reader
        writer.rollback()
        reader.rollback()


def run(conns, chat: ScriptedChat, question: str = "a question"):
    writer, reader = conns
    return ask(question, chat=chat, embedder=FakeEmbedder(), writer=writer, reader=reader)


@pytest.mark.database
def test_resolution_takes_a_unique_best_match_and_asks_about_a_tie(conns) -> None:
    _, reader = conns
    players, question = resolve_players(reader, ["Bruno Fernandes"])
    assert [p.player_id for p in players] == [387] and question is None

    players, question = resolve_players(reader, ["Silva"])
    assert players == [] and question.startswith('Several players match "Silva"')

    _, question = resolve_players(reader, ["Zzyzx Qwerty"])
    assert "couldn't find" in question


@pytest.mark.database
def test_a_refusal_at_understanding_makes_no_generation_call(conns) -> None:
    chat = ScriptedChat(understood("refuse", message="Captain picks are not in the data."))
    answer = run(conns, chat)
    assert (answer.kind, answer.message) == ("refuse", "Captain picks are not in the data.")
    assert [name for name, _ in chat.sent] == ["record_understanding"]


@pytest.mark.database
def test_an_ambiguous_player_stops_before_any_sql_exists(conns) -> None:
    chat = ScriptedChat(understood(players=["Silva"]))
    answer = run(conns, chat)
    assert answer.kind == "clarify" and "Which one" in answer.message
    assert answer.sql is None
    assert len(chat.sent) == 1


@pytest.mark.database
def test_the_happy_path_resolves_generates_validates_and_runs(conns) -> None:
    chat = ScriptedChat(
        understood(players=["Bruno Fernandes"]),
        submitted("SELECT season, points FROM analytics.player_season "
                  "WHERE player_id = 387 ORDER BY season"),
    )
    answer = run(conns, chat, "Bruno Fernandes points by season")
    assert answer.kind == "answer"
    assert answer.columns == ["season", "points"]
    assert answer.rows and answer.rows[0][0] == "2019-20"
    assert "LIMIT" in answer.sql  # the validator's regenerated statement, not the input
    assert [c["stage"] for c in answer.trace.calls] == ["understand", "generate"]

    generation_message = chat.sent[1][1]
    assert "Bruno Fernandes = player_id 387" in generation_message
    assert "Current season: " in generation_message
    for view in ("analytics.player_gameweek", "analytics.player_season", "analytics.team_fixture"):
        assert f"View {view}" in generation_message, "every view's docs go in every prompt"


@pytest.mark.database
def test_a_refused_query_is_retried_once_with_the_reason(conns) -> None:
    chat = ScriptedChat(
        understood(),
        submitted("SELECT display_name FROM core.player"),
        submitted("SELECT player_name FROM analytics.player LIMIT 1"),
    )
    answer = run(conns, chat)
    assert answer.kind == "answer"
    assert answer.trace.attempts[0]["refused"] == "forbidden_schema"
    assert "Refused (forbidden_schema)" in chat.sent[2][1]


@pytest.mark.database
def test_a_database_error_is_retried_and_the_transaction_recovers(conns) -> None:
    chat = ScriptedChat(
        understood(),
        submitted("SELECT no_such_column FROM analytics.player"),
        submitted("SELECT player_name FROM analytics.player LIMIT 1"),
    )
    answer = run(conns, chat)
    assert answer.kind == "answer"
    assert answer.trace.attempts[0]["refused"] == "database_error"


@pytest.mark.database
def test_two_refusals_end_in_an_error_not_a_third_try(conns) -> None:
    chat = ScriptedChat(
        understood(),
        submitted("SELECT * FROM core.player"),
        submitted("DELETE FROM analytics.player"),
    )
    answer = run(conns, chat)
    assert answer.kind == "error"
    assert answer.sql is None and answer.rows == []
    assert len(answer.trace.attempts) == 2


@pytest.mark.database
def test_generation_can_still_decline(conns) -> None:
    chat = ScriptedChat(understood(), submitted("", kind="refuse",
                                                explanation="Minute-level timings are not in the data."))
    answer = run(conns, chat)
    assert (answer.kind, answer.message) == ("refuse", "Minute-level timings are not in the data.")


@pytest.mark.database
def test_the_resolver_overrides_a_model_clarification_about_a_name(conns) -> None:
    """Live run, 4 Nov: asked "How many goals has Silva scored?", the
    understand stage answered `clarify` itself, prompted by the retrieved
    ambiguous-player example, so no code ever checked the name. The resolver
    must decide, and its question lists the real candidates."""
    chat = ScriptedChat(understood("clarify", players=["Silva"], message="Which Silva?"))
    answer = run(conns, chat)
    assert answer.kind == "clarify"
    assert answer.message.startswith('Several players match "Silva"')


@pytest.mark.database
def test_a_model_clarification_stands_when_its_names_resolve(conns) -> None:
    chat = ScriptedChat(understood("clarify", players=["Bruno Fernandes"],
                                   message="Goals, or goal involvements?"))
    answer = run(conns, chat)
    assert (answer.kind, answer.message) == ("clarify", "Goals, or goal involvements?")
    assert [p.player_id for p in answer.players] == [387]
    assert len(chat.sent) == 1, "no SQL is generated for a question still being clarified"
