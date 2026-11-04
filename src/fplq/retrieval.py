"""Embedding the corpus into pgvector, and searching it.

`index()` keeps retrieval.chunk in step with corpus/*.yaml. It is incremental
and idempotent: a chunk is re-embedded only when its text or the embedding
model has changed, and a chunk that has left the corpus is deleted. Rerunning
it with nothing changed makes no Bedrock call at all, which matters both for
cost and because an index that drifts on every run cannot be trusted to
reproduce the prompt a past answer was built from.

`search()` embeds a question and returns the nearest chunks by cosine
distance. It runs as the writer, never as fplq_reader: the reader is the role
generated SQL executes as, and it has no grant on the retrieval schema by
design (sql/011_retrieval.sql).

The embedder is a small protocol so the tests can use a deterministic fake
instead of calling Bedrock.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from fplq import corpus as corpus_mod
from fplq.config import Settings
from fplq.config import settings as default_settings

log = logging.getLogger(__name__)


class Embedder(Protocol):
    model: str

    def embed(self, text: str) -> list[float]: ...


class TitanEmbedder:
    """Amazon Titan Text Embeddings v2 on Bedrock.

    Vectors are requested normalised, so cosine distance and inner product
    rank identically, and at the configured dimensions so they fit the
    column. Retries are adaptive: the account's Titan quota is 60 requests a
    minute, and a full re-index is under that, but a burst should slow down
    rather than fail.
    """

    def __init__(self, settings: Settings = default_settings, client: Any | None = None) -> None:
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        if client is None:
            import boto3  # optional dependency: pip install -e '.[aws]'
            from botocore.config import Config

            client = boto3.client(
                "bedrock-runtime",
                region_name=settings.bedrock_region,
                config=Config(retries={"total_max_attempts": 6, "mode": "adaptive"}),
            )
        self._client = client

    def embed(self, text: str) -> list[float]:
        response = self._client.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {"inputText": text, "dimensions": self.dimensions, "normalize": True}
            ),
        )
        vector = json.loads(response["body"].read())["embedding"]
        if len(vector) != self.dimensions:
            raise ValueError(
                f"{self.model} returned {len(vector)} dimensions, expected {self.dimensions}"
            )
        return vector


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _vector_literal(vector: list[float]) -> str:
    # pgvector's text input format. Passing it as text and casting keeps the
    # project free of a client-side adapter dependency.
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


@dataclass
class IndexReport:
    embedded: int = 0
    unchanged: int = 0
    deleted: int = 0

    def summary(self) -> str:
        return (f"{self.embedded} chunk(s) embedded, {self.unchanged} unchanged, "
                f"{self.deleted} deleted")


def index(conn: psycopg.Connection, embedder: Embedder,
          corpus: corpus_mod.Corpus | None = None) -> IndexReport:
    """Bring retrieval.chunk in line with the corpus. Runs in the caller's
    transaction, so a failure part-way leaves the previous index intact."""
    chunks = corpus_mod.chunks(corpus or corpus_mod.load())
    report = IndexReport()

    existing = {
        row["chunk_id"]: (row["content_sha256"], row["model"])
        for row in conn.execute("SELECT chunk_id, content_sha256, model FROM retrieval.chunk")
    }

    for chunk in chunks:
        digest = _sha256(chunk.text)
        if existing.get(chunk.id) == (digest, embedder.model):
            report.unchanged += 1
            continue
        vector = embedder.embed(chunk.text)
        conn.execute(
            """
            INSERT INTO retrieval.chunk (chunk_id, kind, content, content_sha256, embedding, model)
            VALUES (%s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                kind = EXCLUDED.kind,
                content = EXCLUDED.content,
                content_sha256 = EXCLUDED.content_sha256,
                embedding = EXCLUDED.embedding,
                model = EXCLUDED.model,
                embedded_at = now()
            """,
            (chunk.id, chunk.kind, chunk.text, digest, _vector_literal(vector), embedder.model),
        )
        report.embedded += 1

    stale = sorted(set(existing) - {c.id for c in chunks})
    if stale:
        conn.execute("DELETE FROM retrieval.chunk WHERE chunk_id = ANY(%s)", (stale,))
        report.deleted = len(stale)

    log.info("retrieval index: %s", report.summary())
    return report


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    kind: str
    content: str
    distance: float


def search(conn: psycopg.Connection, embedder: Embedder, question: str, k: int = 6,
           kinds: tuple[str, ...] | None = None) -> list[Hit]:
    """The k chunks nearest to the question, nearest first, optionally only
    of the given kinds ('view', 'function', 'term', 'example').

    Refuses to compare across models: a vector from one embedding model is
    meaningless against another's, and nothing would announce the mismatch
    except poor results.
    """
    mixed = conn.execute(
        "SELECT count(*) AS n FROM retrieval.chunk WHERE model <> %s", (embedder.model,)
    ).fetchone()["n"]
    if mixed:
        raise RuntimeError(
            f"{mixed} chunk(s) were embedded with a different model than {embedder.model}; "
            "run `fplq index` first"
        )
    vector = _vector_literal(embedder.embed(question))
    rows = conn.execute(
        """
        SELECT chunk_id, kind, content, embedding <=> %s::vector AS distance
        FROM retrieval.chunk
        WHERE %s::text[] IS NULL OR kind = ANY(%s::text[])
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (vector, list(kinds) if kinds else None, list(kinds) if kinds else None, vector, k),
    ).fetchall()
    return [Hit(r["chunk_id"], r["kind"], r["content"], float(r["distance"])) for r in rows]
