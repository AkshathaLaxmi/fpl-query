# Vector store: pgvector, in the Postgres we already run

Decided October 2026 (FPL-8). The retrieval corpus (schema docs, glossary,
few-shot examples) is embedded and searched with
[pgvector](https://github.com/pgvector/pgvector) inside the existing `fplq`
database, not in a separate vector service.

## What was on the table

| Option | Why it was considered | Why it lost |
|---|---|---|
| **pgvector** | Already running Postgres; one extension, no new service | — |
| S3 Vectors | Serverless and pay-per-use, so idle cost is close to nothing | A second service, a second set of IAM, and nothing to run locally, so dev and deployment would retrieve differently |
| Bedrock Knowledge Base on OpenSearch Serverless | The managed, "default" AWS path | A monthly floor in the hundreds of dollars before a single query. Ruled out for the live service. |

## Why pgvector

**The corpus is small.** It is a description of one schema, a glossary and a
set of worked examples: hundreds of chunks, not millions. Every argument for a
dedicated vector database is about scale this project will not reach, so the
deciding factors are cost and moving parts, and pgvector adds neither.

**Dev and deployment retrieve the same way.** The same extension runs on the
laptop and in the deployed database, so a retrieval test that passes locally
is testing the real thing. With S3 Vectors, local runs would need a stand-in.

**One place for the data and what describes it.** The corpus documents the
`analytics` schema. Keeping it in the same database means a migration that
changes a view can change its description in the same transaction, and one
backup covers both.

## What we gave up

- **Retrieval shares the database with user queries.** A slow similarity
  search competes with SQL execution. At this corpus size an HNSW index
  answers in well under a millisecond, so this is a note rather than a
  worry, but it is the first thing to revisit if the corpus grows by orders
  of magnitude.
- **The database is now a dependency of the prompt, not just the answer.** If
  Postgres is down, nothing works anyway, so this does not add a new failure
  mode. It does mean retrieval cannot be scaled separately.

## The boundary this must not break

The model is only ever told about `analytics`, and `fplq_reader` (the role
that executes generated SQL) can reach nothing else. The corpus does not
change that: it will live in its own schema, read by the application when it
builds the prompt, and **not granted to `fplq_reader`**. Generated SQL that
tries to read the embeddings is refused by the same grants that refuse `core`
and `raw`.

## The exam path

AIP-C01 covers Bedrock Knowledge Bases, so that path is worth building once.
It can be done as a throwaway build and torn down after, without making it
the live retrieval path or paying the OpenSearch Serverless floor month to
month.

## Setup

Requires pgvector 0.8 or later (tested with 0.8.7 on PostgreSQL 17).

```bash
# macOS
brew install pgvector

# Debian / Ubuntu, matching your Postgres major version
sudo apt install postgresql-17-pgvector
```

The Docker image `pgvector/pgvector:pg17` ships with it preinstalled.
`CREATE EXTENSION vector` arrives with the retrieval corpus migration (FPL-9).
