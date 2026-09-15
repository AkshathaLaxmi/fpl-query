# fpl-query

Ask a football question in plain English; get an answer, the SQL that produced
it, and the numbers behind it.

```
> Which defenders under £5.0m have the best fixtures over the next four gameweeks?
> Who has the most points per million among midfielders this season?
> Which players were underpriced at gameweek 5 relative to what they scored next?
```

A production natural-language-to-SQL service over Premier League and Fantasy
Premier League data: retrieval-augmented SQL generation on AWS Bedrock, executed
against a point-in-time Postgres warehouse behind a validator, evaluated against
a golden set on every prompt change, with a hard cost ceiling.

**Status: in progress.** The data platform is built (this repo); the query
service is next. See [Roadmap](#roadmap).

---

## Why it is built this way

Three decisions carry most of the weight, and each is a place a simpler
implementation would be quietly wrong.

### 1. Point-in-time correctness

"Which players were underpriced at gameweek 5?" cannot be answered from a
current-price column, because the price you need no longer exists by the time
anyone asks. Prices and ownership are therefore modelled as history, not as
current values:

```sql
core.player_price_history(player_id, price, valid_from, valid_to)
```

A type-2 slowly changing dimension with half-open intervals, and a Postgres
exclusion constraint over `tstzrange` so overlapping history is impossible at
the database level rather than by convention — a double-run of the loader cannot
silently corrupt the record. Every value question joins as-of:

```sql
JOIN core.player_price_history h
  ON h.player_id = p.player_id
 AND h.valid_from <= :as_of
 AND (h.valid_to > :as_of OR h.valid_to IS NULL)
```

Most public FPL tools skip this. It is the difference between a stats page and
a warehouse.

### 2. Identity across sources that disagree

The same footballer is `Son Heung-min`, `Heung-Min Son`, and `Son`.
`Gabriel dos Santos Magalhães` is `Gabriel`. `Rodrigo Hernandez` is `Rodri`.
FPL's per-season `element` ids are reused — element 4 is a different human in
2019-20 than in 2026-27 — so any join through a source id without a season is a
bug waiting to happen.

Resolution runs as a cascade, most reliable signal first, recording *how* each
row matched so the heuristics can be measured rather than argued about:

| Rung | Signal | Method recorded |
|---|---|---|
| 1 | FPL's stable cross-season `code` | `code` |
| 2 | Hand-maintained alias file | `override` |
| 3 | Order-independent normalised name key | `exact` |
| 4 | Token-set similarity, with a margin over the runner-up | `fuzzy` |
| 5 | Nothing confident enough | recorded as an issue |

Rung 5 is the one that matters. A resolver that always returns something is a
resolver that attributes Salah's goals to somebody else. Unmatched names land in
`core.resolution_issue` with their near-miss candidates, `fplq issues` prints
them, and a human adds an alias. The
[override file](data/overrides/player_aliases.yaml) is not an admission of
defeat — every real data platform has one, and the alternative is silently wrong
joins.

### 3. A real boundary, and some defaults that are not one

Generated SQL is never trusted. It executes as `fplq_reader`, which is granted
`SELECT` on the `analytics` schema and **nothing at all on `core` or `raw`**,
holds no write privilege anywhere, and cannot create temporary objects.

That grant is the boundary, and it is worth being precise about why, because
the role also carries `default_transaction_read_only`, a 10-second
`statement_timeout` and a `search_path` that cannot name `core`. Those three are
*defaults*, not limits — they are `USERSET` parameters in Postgres, so a session
that manages to send `SET statement_timeout = '1h'; SELECT ...` simply raises
them. They protect against accidents. They do not protect against a hostile
query, and this README used to claim otherwise.

What stops the `SET` from arriving is the validator
([`src/fplq/validate.py`](src/fplq/validate.py)): single statement, `SELECT`
only, allow-listed schemas, an allow-list of callable functions, a forced
`LIMIT`, and an `EXPLAIN` cost gate before execution. It parses with `sqlglot`
rather than matching patterns, and what executes is the statement *regenerated
from the parsed tree* — so a byte the checks did not see cannot reach the
server. So the honest picture is two real layers, privileges and the validator,
with a set of sane defaults underneath both.

Bedrock Guardrails handle abuse and PII; they are not SQL safety and are not
treated as such.

---

## Architecture

```
Client → API Gateway (usage plan, per-IP throttle)
       → Lambda (Python 3.13, ARM64)
       → Step Functions: clarify → retrieve → generate SQL
                        → validate → execute → summarise
       → Bedrock (Converse API) + Guardrails
       → Knowledge Base: schema docs, glossary, few-shot examples
       → Postgres (read-only role) ← ingestion pipeline
       → DynamoDB: semantic cache, token budget, query log
```

The layer built so far is the right-hand column: ingestion, the warehouse, and
the `analytics` views that the retrieval corpus describes.

### Schema

| Schema | Contents | Who reads it |
|---|---|---|
| `raw` | Immutable landing. Append-only, one row per source record, payload verbatim. | Replay only |
| `core` | The model. Surrogate keys, source-id crosswalk, point-in-time history. | The pipeline |
| `analytics` | Denormalised views in the words a human would use. | **Generated SQL, and nothing else** |

`analytics` is the seam. Renaming a column in `core` does not break the prompt,
and the allow-list is exactly one schema wide.

```
analytics.player_gameweek   one row per player per match; price is the price
                            that gameweek, not today's
analytics.player_season     season totals, aggregated from the same match rows
                            so a drill-down never contradicts the summary
analytics.team_fixture      one row per team per fixture, so upcoming-fixture
                            questions need no UNION
analytics.price_as_of()     point-in-time price lookup as a callable
```

Grain of the central fact table is *(player, fixture)*, not *(player,
gameweek)* — double gameweeks are real, and a per-gameweek grain quietly loses
one of the two matches.

---

## Data sources

| Source | Contents | Licence position |
|---|---|---|
| Official FPL API | Current season: prices, ownership, live points, fixtures | Public, undocumented, **unlicensed**. Snapshotted on a schedule into our own storage, never proxied live. Attributed, non-commercial. |
| [Community FPL archive](https://github.com/vaastav/Fantasy-Premier-League) | 2019-20 → current, per-gameweek | Credited; gives cross-season history from day one |
| football-data.co.uk | Match results and odds | Free for non-commercial use |

Deliberately excluded: FBref (lost its Opta licence in January 2026) and
scrape-dependent sources for anything user-facing.

The archive's `merged_gw.csv` carries per-gameweek `value` and `selected`, which
is genuine point-in-time price and ownership at gameweek grain — so the history
tables are populated on day one rather than sitting empty for a season while
daily snapshots accumulate. Gameweek resolution is a real limitation and is
documented as one; the live API's daily snapshot refines the current season.
Claiming finer resolution than the source has would be inventing precision.

---

## Getting started

Requires Python 3.11+ and PostgreSQL 14+ (`btree_gist` is used for the
exclusion constraint).

### Postgres, if you do not already have it

`fplq bootstrap` needs a running server and a connection that can `CREATE
ROLE`, `CREATE DATABASE` and `CREATE EXTENSION` — so a superuser, not the
roles this project creates.

**macOS, Homebrew:**

```bash
brew install postgresql@17
brew services start postgresql@17
# The versioned formula is keg-only, so its binaries are not on PATH until
# you say so -- without this, psql and pg_isready stay "command not found"
# even though the install succeeded.
echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zshrc
exec zsh
```

Homebrew creates a superuser named after your account rather than one called
`postgres`. If something later reports that your role does not exist:
`createuser -s "$(whoami)"`.

**macOS, [Postgres.app](https://postgresapp.com):** install, click Initialize,
then add its `bin` directory to `PATH` from Preferences → Command Line Tools.

**Debian / Ubuntu:**

```bash
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
```

**Docker**, if you would rather not install a server at all:

```bash
# Pick your own password here -- postgres:postgres is a known default that
# credential-scanning bots try first against any exposed port.
docker run -d --name fplq-pg -p 5432:5432 \
    -e POSTGRES_PASSWORD=change-me postgres:17
```

Check it is up with `pg_isready` before going further.

### Pointing the project at it

The default admin connection assumes a Linux socket
(`/var/run/postgresql`) and a `postgres` superuser. That is right on Debian and
wrong nearly everywhere else, which shows up as:

```
connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed:
No such file or directory
```

That message means the DSN, not the server — set `FPLQ_ADMIN_DSN` in `.env`
to match your install:

```ini
# macOS (Homebrew or Postgres.app): socket in /tmp, superuser is your account.
# Write your actual username -- .env is read as literal key=value pairs, so
# $(whoami) and other shell expansions are not expanded here.
FPLQ_ADMIN_DSN=postgresql://your-username@/postgres?host=/tmp

# Docker, or any server reached over TCP -- use the password you set above,
# not "postgres".
FPLQ_ADMIN_DSN=postgresql://postgres:change-me@localhost:5432/postgres
```

`.env` is gitignored. `bootstrap` writes the generated writer and reader
passwords into the same file, so it is worth creating before the first run
rather than after.

### The project itself

```bash
git clone https://github.com/akshathalaxmi/fpl-query
cd fpl-query
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

fplq bootstrap                # creates database, roles and schema, and
                              # generates credentials into .env on first run
fplq load --seasons 2024-25 2025-26 2026-27
fplq stats                    # row counts
fplq issues                   # names that need a human
```

Loading is idempotent — every write is an upsert on a natural key, and a season
loads inside one transaction, so a half-loaded season is not a state that
exists. Re-run it freely.

Once a season is loaded, keep the current one fresh from the live API:

```bash
fplq ingest                   # today's prices, ownership and match results
fplq replay --batch-id 12     # re-run a past ingest's transform, no re-fetch
```

`fplq ingest` is meant to run on a schedule (cron, a Lambda, whatever is
convenient) rather than by hand every day. It exits non-zero if it writes zero
rows — the quiet failure that a scheduler should alert on, since nothing else
about that run looks wrong.

```bash
pytest -m "not database and not network"   # fast: parsing, resolution, the validator
pytest -m database                         # integration, against a live Postgres
pytest -m "database and network"           # live ingestion, against the real FPL API
pytest                                     # everything, including the three above
ruff check .
```

---

## Roadmap

- [x] Immutable raw landing (local filesystem / S3, same interface)
- [x] Warehouse schema with point-in-time price and ownership history
- [x] Entity resolution with audit trail and an issues queue
- [x] Archive ingestion, 2019-20 → current
- [x] `analytics` views and the read-only execution role
- [ ] Scheduled ingestion from the live API (EventBridge → Lambda)
- [ ] Retrieval corpus: schema docs, glossary, few-shot examples
- [x] The SQL validator: parse, allow-list, forced `LIMIT`, cost gate
- [ ] SQL generation on Bedrock
- [ ] Golden set and regression runs on every prompt change
- [ ] Semantic cache, token budget, per-IP limits
- [ ] Public endpoint

---

## Security

No credentials are committed. `fplq bootstrap` generates a random password per
role on first run and writes it to `.env`, which is gitignored and written
`0600`; deployment supplies the environment from Secrets Manager instead. An
earlier revision carried a "harmless" dev default in the DSNs — a known password,
published, and used silently by anyone who deployed without setting the
environment.

Input that a stranger typed reaches `analytics.find_player()` by design, so it is
escaped with `analytics.like_literal()` and matched literally. It is never
interpolated into a `LIKE` pattern or a regex, and results are capped. See
`sql/008_search_hardening.sql` for what that fixed and why.

Known limitations, stated rather than implied away:

- The reader's session settings are overridable defaults, not limits — see above.
  The validator, not the timeout, is what makes them hard to reach.
- `analytics.price_as_of()` is `SECURITY DEFINER` with a pinned `search_path`
  and a fixed body; see `sql/006_function_security.sql`.
- The validator's guarantee is only as good as the parser's view of Postgres.
  `sqlglot` reads the Postgres dialect but is not Postgres' own grammar, so the
  design does not depend on it being exhaustive: anything it fails to parse, and
  anything it parses into a fallback `Command` node, is rejected rather than
  passed through, and every function name it does not recognise must be on an
  allow-list. The failure direction is refusal, not execution.
- The cost gate uses planner estimates, which are estimates. It stops the
  catastrophic plans, not every slow one; the statement timeout still sits
  underneath it.
- Validation covers the SQL. Prompt injection that produces a *legitimate*
  query the user did not intend is a different problem, handled by Guardrails
  and the generation prompt rather than here.

If you find something, open an issue.

## Licence

MIT. See [LICENSE](LICENSE).

Not affiliated with the Premier League or Fantasy Premier League. FPL data is
used non-commercially, with attribution, from our own snapshots.
