-- 002_core.sql — the modelled warehouse. This is the schema the NL->SQL layer sees.
--
-- Design rules, because the generated SQL is only as good as the model underneath it:
--
--   1. Surrogate keys everywhere. Source IDs are never primary keys, because the
--      FPL API reuses element ids across seasons -- element 4 is a different human
--      in 2019-20 than in 2026-27. Every join through a source id without a season
--      is a bug waiting to be asked about.
--   2. Cross-source identity lives in one place (core.player_source_id), never
--      smeared across fact tables.
--   3. Anything that changes over time is stored as history, not as a current
--      value. Price and ownership are the ones that matter.
--   4. Column names are the words a human would use. The retrieval layer feeds
--      these names to the model; "now_cost" costs us accuracy that "price" doesn't.

CREATE SCHEMA IF NOT EXISTS core;

-- ---------------------------------------------------------------------------
-- Reference
-- ---------------------------------------------------------------------------

-- Seasons are the spine. Every season-scoped fact carries season_id.
CREATE TABLE core.season (
    season_id       SMALLSERIAL PRIMARY KEY,
    name            TEXT        NOT NULL UNIQUE,   -- '2025-26'
    start_year      SMALLINT    NOT NULL UNIQUE,   -- 2025
    is_current      BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE UNIQUE INDEX season_single_current_idx
    ON core.season ((TRUE)) WHERE is_current;

CREATE TABLE core.position (
    position_id     SMALLINT PRIMARY KEY,          -- FPL element_type, stable across seasons
    code            TEXT NOT NULL UNIQUE,          -- 'GKP','DEF','MID','FWD'
    name            TEXT NOT NULL
);

INSERT INTO core.position (position_id, code, name) VALUES
    (1, 'GKP', 'Goalkeeper'),
    (2, 'DEF', 'Defender'),
    (3, 'MID', 'Midfielder'),
    (4, 'FWD', 'Forward'),
    (5, 'AM',  'Assistant Manager');   -- introduced 2024-25, later withdrawn

-- ---------------------------------------------------------------------------
-- Clubs
-- ---------------------------------------------------------------------------

-- A club exists independently of whether it is in the Premier League this year.
-- Promotion and relegation are modelled as season membership, not as the club
-- appearing and vanishing.
CREATE TABLE core.team (
    team_id         SERIAL PRIMARY KEY,
    fpl_team_code   INTEGER UNIQUE,                -- stable across seasons, unlike team id
    name            TEXT NOT NULL,                 -- 'Nottingham Forest'
    short_name      TEXT NOT NULL                  -- 'NFO'
);

-- Which clubs were in the league in a given season, and the per-season FPL team
-- id (1..20, reassigned alphabetically every year -- never join on it alone).
CREATE TABLE core.team_season (
    team_season_id  SERIAL PRIMARY KEY,
    team_id         INTEGER  NOT NULL REFERENCES core.team (team_id),
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    fpl_team_id     SMALLINT NOT NULL,
    strength_overall_home SMALLINT,
    strength_overall_away SMALLINT,
    strength_attack_home  SMALLINT,
    strength_attack_away  SMALLINT,
    strength_defence_home SMALLINT,
    strength_defence_away SMALLINT,
    UNIQUE (team_id, season_id),
    UNIQUE (season_id, fpl_team_id)
);

-- ---------------------------------------------------------------------------
-- Players and identity
-- ---------------------------------------------------------------------------

-- One row per human being, for all time. Populated by the resolver.
CREATE TABLE core.player (
    player_id       SERIAL PRIMARY KEY,
    fpl_player_code INTEGER UNIQUE,                -- FPL's stable cross-season 'code'
    first_name      TEXT,
    last_name       TEXT,
    display_name    TEXT NOT NULL,                 -- FPL web_name: 'Salah', 'Son'
    full_name       TEXT NOT NULL,                 -- 'Mohamed Salah'
    normalised_name TEXT NOT NULL,                 -- accent/punctuation-stripped, for matching
    birth_date      DATE
);

CREATE INDEX player_display_name_idx    ON core.player (lower(display_name));
CREATE INDEX player_normalised_name_idx ON core.player (normalised_name);

-- The crosswalk. Every external identifier for a player lands here and nowhere
-- else. Adding Understat or football-data later means inserting rows here, not
-- touching any fact table.
CREATE TABLE core.player_source_id (
    player_id       INTEGER NOT NULL REFERENCES core.player (player_id) ON DELETE CASCADE,
    source          TEXT    NOT NULL,              -- 'fpl' | 'understat' | 'football_data'
    season_id       SMALLINT REFERENCES core.season (season_id),  -- NULL if source id is season-independent
    source_id       TEXT    NOT NULL,
    match_method    TEXT    NOT NULL               -- how we decided; audit trail for the resolver
                    CHECK (match_method IN ('code', 'exact', 'fuzzy', 'override', 'manual')),
    match_score     REAL,
    PRIMARY KEY (source, season_id, source_id)
);

CREATE INDEX player_source_id_player_idx ON core.player_source_id (player_id);

-- A player's registration for a season: which club, which position, and the
-- price they started at. Position and club can both change mid-season, which is
-- why the facts below carry their own team reference rather than reading it here.
CREATE TABLE core.player_season (
    player_season_id SERIAL PRIMARY KEY,
    player_id       INTEGER  NOT NULL REFERENCES core.player (player_id),
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    team_id         INTEGER  REFERENCES core.team (team_id),
    position_id     SMALLINT REFERENCES core.position (position_id),
    fpl_element_id  SMALLINT NOT NULL,             -- per-season FPL id; the join key for GW data
    start_price     NUMERIC(4,1),                  -- in millions, e.g. 12.5
    end_price       NUMERIC(4,1),
    squad_number    SMALLINT,
    UNIQUE (player_id, season_id),
    UNIQUE (season_id, fpl_element_id)
);

-- ---------------------------------------------------------------------------
-- Gameweeks and fixtures
-- ---------------------------------------------------------------------------

-- Gameweeks are not calendar weeks and do not align to months. Deadline is the
-- moment that matters for "at gameweek N" questions, so it is stored explicitly
-- rather than derived from kickoffs.
CREATE TABLE core.gameweek (
    gameweek_id     SERIAL PRIMARY KEY,
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    gameweek_number SMALLINT NOT NULL CHECK (gameweek_number BETWEEN 1 AND 47),
    deadline_at     TIMESTAMPTZ,
    is_finished     BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (season_id, gameweek_number)
);

CREATE TABLE core.fixture (
    fixture_id      SERIAL PRIMARY KEY,
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    gameweek_id     INTEGER  REFERENCES core.gameweek (gameweek_id),  -- NULL when postponed/unscheduled
    fpl_fixture_id  INTEGER  NOT NULL,
    kickoff_at      TIMESTAMPTZ,
    home_team_id    INTEGER NOT NULL REFERENCES core.team (team_id),
    away_team_id    INTEGER NOT NULL REFERENCES core.team (team_id),
    home_score      SMALLINT,
    away_score      SMALLINT,
    home_difficulty SMALLINT,                      -- FPL's FDR, 1..5
    away_difficulty SMALLINT,
    is_finished     BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (season_id, fpl_fixture_id),
    CHECK (home_team_id <> away_team_id)
);

CREATE INDEX fixture_gameweek_idx ON core.fixture (gameweek_id);
CREATE INDEX fixture_home_idx     ON core.fixture (home_team_id, kickoff_at);
CREATE INDEX fixture_away_idx     ON core.fixture (away_team_id, kickoff_at);

-- ---------------------------------------------------------------------------
-- Point-in-time: price and ownership
-- ---------------------------------------------------------------------------
--
-- This is the part most public FPL tools get wrong. Prices change daily and
-- ownership changes hourly, so "which players were underpriced at gameweek 5"
-- cannot be answered from a current-value column -- by the time you ask, the
-- price you needed is gone.
--
-- Modelled as a type-2 slowly changing dimension with a half-open interval
-- [valid_from, valid_to). valid_to IS NULL means "still true". The exclusion
-- constraint below makes overlapping intervals impossible at the database
-- level rather than by convention, so a double-run of the loader cannot
-- silently corrupt history.
--
-- Query pattern, and the one the few-shot examples teach:
--     JOIN core.player_price_history h
--       ON h.player_id = p.player_id
--      AND h.valid_from <= :as_of
--      AND (h.valid_to > :as_of OR h.valid_to IS NULL)

-- btree_gist is installed by `fplq bootstrap` (needs superuser); see cli.py.

CREATE TABLE core.player_price_history (
    price_history_id BIGSERIAL PRIMARY KEY,
    player_id       INTEGER  NOT NULL REFERENCES core.player (player_id),
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    price           NUMERIC(4,1) NOT NULL,         -- millions
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    source          TEXT NOT NULL DEFAULT 'fpl',
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    EXCLUDE USING gist (
        player_id WITH =,
        tstzrange(valid_from, valid_to) WITH &&
    )
);

CREATE INDEX player_price_history_asof_idx
    ON core.player_price_history (player_id, valid_from DESC);

-- Ownership is a snapshot rather than an interval: it moves continuously, so
-- an interval would be a lie about precision we don't have.
CREATE TABLE core.player_ownership_snapshot (
    ownership_id    BIGSERIAL PRIMARY KEY,
    player_id       INTEGER  NOT NULL REFERENCES core.player (player_id),
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    snapshot_at     TIMESTAMPTZ NOT NULL,
    gameweek_id     INTEGER REFERENCES core.gameweek (gameweek_id),  -- set when the snapshot is GW-grained
    selected_by     INTEGER,                       -- absolute number of squads
    ownership_pct   NUMERIC(5,2),                  -- percentage, when the source gives it
    transfers_in    INTEGER,
    transfers_out   INTEGER,
    UNIQUE (player_id, snapshot_at)
);

CREATE INDEX player_ownership_asof_idx
    ON core.player_ownership_snapshot (player_id, snapshot_at DESC);

-- ---------------------------------------------------------------------------
-- The central fact: one player, one fixture
-- ---------------------------------------------------------------------------
--
-- Grain is (player, fixture), not (player, gameweek) -- double gameweeks are
-- real and a per-gameweek grain quietly loses one of the two matches.
-- price_at_deadline and selected_at_deadline are denormalised from the history
-- tables above: they are what the value was when this match was played, kept
-- here because nearly every value question needs them and the as-of join is
-- the single most common way generated SQL goes wrong.

CREATE TABLE core.player_fixture_stat (
    stat_id         BIGSERIAL PRIMARY KEY,
    player_id       INTEGER  NOT NULL REFERENCES core.player (player_id),
    season_id       SMALLINT NOT NULL REFERENCES core.season (season_id),
    gameweek_id     INTEGER  REFERENCES core.gameweek (gameweek_id),
    fixture_id      INTEGER  REFERENCES core.fixture (fixture_id),
    team_id         INTEGER  REFERENCES core.team (team_id),
    opponent_team_id INTEGER REFERENCES core.team (team_id),
    position_id     SMALLINT REFERENCES core.position (position_id),
    was_home        BOOLEAN,
    kickoff_at      TIMESTAMPTZ,

    minutes         SMALLINT,
    starts          SMALLINT,
    total_points    SMALLINT,
    goals_scored    SMALLINT,
    assists         SMALLINT,
    clean_sheets    SMALLINT,
    goals_conceded  SMALLINT,
    own_goals       SMALLINT,
    penalties_saved SMALLINT,
    penalties_missed SMALLINT,
    yellow_cards    SMALLINT,
    red_cards       SMALLINT,
    saves           SMALLINT,
    bonus           SMALLINT,
    bps             SMALLINT,

    influence       NUMERIC(6,1),
    creativity      NUMERIC(6,1),
    threat          NUMERIC(6,1),
    ict_index       NUMERIC(6,1),

    -- Available from 2022-23 onward; NULL for earlier seasons rather than zero,
    -- so an average over history is not silently wrong.
    expected_goals              NUMERIC(6,2),
    expected_assists            NUMERIC(6,2),
    expected_goal_involvements  NUMERIC(6,2),
    expected_goals_conceded     NUMERIC(6,2),
    expected_points             NUMERIC(6,2),

    -- Defensive contribution scoring, introduced 2025-26.
    tackles                     SMALLINT,
    recoveries                  SMALLINT,
    clearances_blocks_interceptions SMALLINT,
    defensive_contribution      SMALLINT,

    price_at_deadline    NUMERIC(4,1),
    selected_at_deadline INTEGER,
    transfers_in         INTEGER,
    transfers_out        INTEGER,

    UNIQUE (player_id, fixture_id)
);

CREATE INDEX pfs_player_season_idx  ON core.player_fixture_stat (player_id, season_id);
CREATE INDEX pfs_gameweek_idx       ON core.player_fixture_stat (gameweek_id);
CREATE INDEX pfs_team_idx           ON core.player_fixture_stat (team_id, season_id);
CREATE INDEX pfs_kickoff_idx        ON core.player_fixture_stat (kickoff_at);

-- ---------------------------------------------------------------------------
-- Data quality
-- ---------------------------------------------------------------------------

-- Every row the pipeline could not confidently place. Reviewed, not ignored --
-- the count of open rows here is a dashboard metric.
CREATE TABLE core.resolution_issue (
    issue_id        BIGSERIAL PRIMARY KEY,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch_id        BIGINT REFERENCES raw.ingest_batch (batch_id),
    issue_type      TEXT NOT NULL,      -- 'unmatched_player', 'ambiguous_player', 'missing_fixture'
    source          TEXT NOT NULL,
    season          TEXT,
    raw_value       TEXT NOT NULL,
    candidates      JSONB,
    resolved_at     TIMESTAMPTZ,
    resolution_note TEXT
);

CREATE INDEX resolution_issue_open_idx
    ON core.resolution_issue (issue_type, detected_at DESC) WHERE resolved_at IS NULL;
