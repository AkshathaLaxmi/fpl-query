# Data sources, licensing and the rules we hold ourselves to

This project answers questions about football data it does not own. That imposes
obligations, and this document states them plainly so that "we thought it was
fine" is never the answer to a question about them.

## The official FPL API

`https://fantasy.premierleague.com/api/` is public and unauthenticated. It is
also **undocumented and unlicensed** — there is no published terms-of-use grant
permitting third-party use. It is widely used by the community, and widely used
is not the same as permitted.

The position taken here, and enforced in code:

| Rule | Where it lives |
|---|---|
| Never proxy the API in a user request path | Every user query is served from our Postgres. `FplApiClient` is only ever called by scheduled ingestion. |
| Snapshot on a schedule, into our own storage | `ingest/store.py`, immutable landing |
| Identify ourselves | `User-Agent` in `config.Settings`, naming the project and its repo |
| Rate-limit ourselves | `request_delay_s`, enforced in `FplApiClient._throttle` |
| Back off rather than hammer on failure | Exponential retry in `FplApiClient._get` |
| Non-commercial | No payments, no ads, no paid tier |
| Attribute | In the UI and in this repo |

If access is ever restricted or asked to stop, the service keeps working on the
data already collected, and we stop collecting. Owning our own snapshots is what
makes that a graceful degradation rather than an outage — which is a good reason
to do it even setting the ethics aside.

**Not affiliated with the Premier League or Fantasy Premier League.**

## Community historical archive

[vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League)
mirrors the FPL API season by season, back further than we could collect
ourselves. Credited here and in the README.

Two things make it worth ingesting rather than treating as a nice-to-have:

- **Cross-season questions work on launch day.** "Since 2023/24" is answerable
  immediately rather than after a year of collection.
- **It carries real point-in-time data.** `merged_gw.csv` has per-gameweek
  `value` and `selected` — the price and ownership that were actually true that
  week. The price history tables are populated from it rather than sitting empty
  while daily snapshots accumulate.

### Its limitations, stated rather than hidden

- **Gameweek resolution, not daily.** Prices move daily; the archive records
  them weekly. Intervals in `player_price_history` are therefore gameweek-grained
  for historical seasons. The live API's daily snapshot refines the current
  season. Claiming finer resolution would be inventing precision the source does
  not have.
- **Schema drift between seasons.** `position` and `team` appear in `merged_gw`
  only from 2020-21; expected-goals columns from 2022-23; defensive-contribution
  columns from 2025-26. The loader reads by column name and writes `NULL` for
  absent columns — never `0`, which would make an average over history quietly
  wrong.
- **String nulls.** Missing values arrive as the literal text `"None"` and
  `"nan"`. Handled centrally in `ingest/archive.py`; this cost us a failed load
  before it was.
- **Season-end snapshot for `players_raw`.** Prices there are final, not current
  mid-season. Opening prices are reconstructed from `cost_change_start`.

## football-data.co.uk

Historical results and odds, free for non-commercial use. Not yet ingested;
listed here because it is the next source and the crosswalk
(`core.player_source_id`) already has a slot for it.

## Deliberately excluded

- **FBref** — lost its Opta licence in January 2026, so current advanced metrics
  are not available there and older ones are of uncertain provenance.
- **Transfermarkt and other scrape-dependent sources** — no licence for
  redistribution, and a scraper in a user-facing path is both a legal and an
  availability risk.
- **Understat** — optional and deferred. Only if a real user question needs xG
  we cannot get elsewhere, per the brief's non-goals.

## Personal data

None is ingested. Player statistics and prices are not personal data in any
sense that matters here, and no user accounts exist beyond what rate limiting
requires. This is one reason the project is fully open source: there is nothing
to leak.
