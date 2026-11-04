"""The retrieval index: incremental, model-aware, and out of the reader's reach.

The embedder is faked with a deterministic hash, so these tests say nothing
about how good Titan's vectors are -- that is judged by running `fplq retrieve`
on real questions, and later by the golden set. What they pin is the
bookkeeping: what gets re-embedded, what gets deleted, and who can read it.

The database tests run inside a transaction that is rolled back, after
clearing the table inside it, so the real index is untouched.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import replace

import psycopg
import pytest

from fplq import corpus as corpus_mod
from fplq.config import settings
from fplq.db import connect
from fplq.retrieval import TitanEmbedder, _vector_literal, index, search
from fplq.validate import ValidationError, Validator

DIMS = settings.embedding_dimensions


class FakeEmbedder:
    """Same text, same unit vector; different text, unrelated vector."""

    def __init__(self, model: str = "fake-embed-v1") -> None:
        self.model = model
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        raw = []
        seed = text.encode()
        while len(raw) < DIMS:
            seed = hashlib.sha256(seed).digest()
            raw.extend(b - 127.5 for b in seed)
        raw = raw[:DIMS]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]


# --- no database --------------------------------------------------------------


class _FakeBedrock:
    def __init__(self, dims: int) -> None:
        self.dims = dims
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(kwargs)
        body = json.dumps({"embedding": [0.0] * self.dims, "inputTextTokenCount": 3})
        return {"body": io.BytesIO(body.encode())}


def test_titan_request_asks_for_normalised_vectors_at_the_column_width() -> None:
    client = _FakeBedrock(DIMS)
    TitanEmbedder(client=client).embed("points per million")
    sent = client.requests[0]
    assert sent["modelId"] == settings.embedding_model
    assert json.loads(sent["body"]) == {
        "inputText": "points per million", "dimensions": DIMS, "normalize": True,
    }


def test_titan_refuses_a_vector_of_the_wrong_width() -> None:
    """A wrong width would otherwise surface as an insert error far from its
    cause, or not at all if the column were ever widened to match."""
    with pytest.raises(ValueError, match="dimensions"):
        TitanEmbedder(client=_FakeBedrock(DIMS - 1)).embed("x")


def test_vector_literal_is_pgvector_text_format() -> None:
    assert _vector_literal([1, 0.5, -2]) == "[1.0,0.5,-2.0]"


def test_validator_refuses_the_retrieval_schema() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Validator().check("SELECT content FROM retrieval.chunk")
    assert excinfo.value.code == "forbidden_schema"


# --- against the live database ------------------------------------------------


@pytest.fixture
def writer() -> psycopg.Connection:
    # connect() commits on a clean exit; roll back first so nothing lands.
    with connect(settings.writer_dsn) as conn:
        conn.execute("DELETE FROM retrieval.chunk")
        yield conn
        conn.rollback()


@pytest.mark.database
def test_index_embeds_everything_once_then_nothing(writer: psycopg.Connection) -> None:
    corpus = corpus_mod.load()
    total = len(corpus_mod.chunks(corpus))
    embedder = FakeEmbedder()

    first = index(writer, embedder, corpus)
    assert (first.embedded, first.unchanged, first.deleted) == (total, 0, 0)

    second = index(writer, embedder, corpus)
    assert (second.embedded, second.unchanged, second.deleted) == (0, total, 0)
    assert embedder.calls == total, "an unchanged corpus must make no embedding calls"


@pytest.mark.database
def test_index_re_embeds_only_what_changed_and_drops_what_left(writer: psycopg.Connection) -> None:
    corpus = corpus_mod.load()
    index(writer, FakeEmbedder(), corpus)

    edited = replace(corpus.examples[0], question=corpus.examples[0].question + " (reworded)")
    smaller = replace(corpus, examples=(edited, *corpus.examples[2:]))
    report = index(writer, FakeEmbedder(), smaller)

    assert report.embedded == 1
    assert report.deleted == 1
    gone = writer.execute(
        "SELECT count(*) AS n FROM retrieval.chunk WHERE chunk_id = %s",
        (f"example:{corpus.examples[1].id}",),
    ).fetchone()["n"]
    assert gone == 0


@pytest.mark.database
def test_a_new_embedding_model_re_embeds_every_chunk(writer: psycopg.Connection) -> None:
    corpus = corpus_mod.load()
    index(writer, FakeEmbedder("fake-embed-v1"), corpus)
    report = index(writer, FakeEmbedder("fake-embed-v2"), corpus)
    assert report.embedded == len(corpus_mod.chunks(corpus))


@pytest.mark.database
def test_search_returns_the_nearest_chunk_first(writer: psycopg.Connection) -> None:
    corpus = corpus_mod.load()
    embedder = FakeEmbedder()
    index(writer, embedder, corpus)
    target = corpus_mod.chunks(corpus)[5]

    hits = search(writer, embedder, target.text, k=3)
    assert hits[0].chunk_id == target.id
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert [h.distance for h in hits] == sorted(h.distance for h in hits)


@pytest.mark.database
def test_search_refuses_to_compare_vectors_from_different_models(writer: psycopg.Connection) -> None:
    index(writer, FakeEmbedder("fake-embed-v1"))
    with pytest.raises(RuntimeError, match="different model"):
        search(writer, FakeEmbedder("fake-embed-v2"), "anything")


@pytest.mark.database
def test_the_reader_cannot_see_the_retrieval_schema() -> None:
    """The role generated SQL runs as must not reach the prompt's raw
    material -- the grant is the boundary, the validator only the first
    check."""
    from fplq.validate import reader_connection

    with reader_connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT count(*) FROM retrieval.chunk")
