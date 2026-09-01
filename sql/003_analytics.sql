-- 003_analytics.sql — the surface the language model actually sees.
--
-- The generated SQL does not query core directly. It queries this schema, for
-- three reasons:
--
--   1. Fewer joins per question. Every join the model has to invent is a chance
--      to invent the wrong one. `analytics.player_gameweek` collapses the five
--      joins that nearly every question needs into a name a human would use.
--   2. The table allow-list in the validator is exactly this schema. If the
--      model cannot name a table outside analytics, it cannot reach one.
--   3. Renaming a column in core does not break the prompt. This is the
--      seam between the data model and the retrieval corpus.
--
-- Kept as views, not materialised, until a golden-set latency number says
-- otherwise. Premature materialisation is a cache-invalidation problem bought
-- with no evidence.

CREATE SCHEMA IF NOT EXISTS analytics;

-- Every player-match row with the things you'd have had to join for, already
-- joined. This is the workhorse -- most questions are a WHERE and a GROUP BY
-- away from an answer once they start here.
CREATE OR REPLACE VIEW analytics.player_gameweek AS
SELECT
    s.stat_id,
    se.name                 AS season,
    gw.gameweek_number      AS gameweek,
    gw.deadline_at          AS gameweek_deadline,
    p.player_id,
    p.display_name          AS player_name,
    p.full_name             AS player_full_name,
    pos.code                AS position,
    t.name                  AS team,
    t.short_name            AS team_short,
    opp.name                AS opponent,
    opp.short_name          AS opponent_short,
    s.was_home,
    s.kickoff_at,
    CASE WHEN s.was_home THEN f.home_difficulty ELSE f.away_difficulty END
                            AS fixture_difficulty,
    s.price_at_deadline     AS price,
    s.selected_at_deadline  AS selected_by,
    s.total_points          AS points,
    s.minutes,
    s.starts,
    s.goals_scored,
    s.assists,
    s.clean_sheets,
    s.goals_conceded,
    s.own_goals,
    s.penalties_saved,
    s.penalties_missed,
    s.yellow_cards,
    s.red_cards,
    s.saves,
    s.bonus,
    s.bps,
    s.influence,
    s.creativity,
    s.threat,
    s.ict_index,
    s.expected_goals,
    s.expected_assists,
    s.expected_goal_involvements,
    s.expected_goals_conceded,
    s.expected_points,
    s.tackles,
    s.recoveries,
    s.clearances_blocks_interceptions,
    s.defensive_contribution,
    s.transfers_in,
    s.transfers_out,
    -- Points per million at the price actually paid that week, not today's price.
    CASE WHEN s.price_at_deadline > 0
         THEN ROUND(s.total_points / s.price_at_deadline, 3) END
                            AS points_per_million
FROM core.player_fixture_stat s
JOIN core.player   p   ON p.player_id  = s.player_id
JOIN core.season   se  ON se.season_id = s.season_id
LEFT JOIN core.gameweek gw  ON gw.gameweek_id = s.gameweek_id
LEFT JOIN core.fixture  f   ON f.fixture_id   = s.fixture_id
LEFT JOIN core.position pos ON pos.position_id = s.position_id
LEFT JOIN core.team     t   ON t.team_id      = s.team_id
LEFT JOIN core.team     opp ON opp.team_id    = s.opponent_team_id;

COMMENT ON VIEW analytics.player_gameweek IS
'One row per player per match played. Grain is player x fixture, so a double '
'gameweek gives a player two rows for the same gameweek number. `price` is the '
'price at that gameweek''s deadline, not the current price.';

-- Season totals. Derived from the same fact table rather than from a source
-- "season summary", so the totals always agree with the per-match rows -- a
-- question that drills down never contradicts the one that summarised.
CREATE OR REPLACE VIEW analytics.player_season AS
SELECT
    se.name                 AS season,
    p.player_id,
    p.display_name          AS player_name,
    p.full_name             AS player_full_name,
    pos.code                AS position,
    t.name                  AS team,
    ps.start_price,
    ps.end_price            AS current_price,
    COUNT(*) FILTER (WHERE s.minutes > 0)  AS matches_played,
    COALESCE(SUM(s.starts), 0)             AS starts,
    COALESCE(SUM(s.minutes), 0)            AS minutes,
    COALESCE(SUM(s.total_points), 0)       AS points,
    COALESCE(SUM(s.goals_scored), 0)       AS goals,
    COALESCE(SUM(s.assists), 0)            AS assists,
    COALESCE(SUM(s.clean_sheets), 0)       AS clean_sheets,
    COALESCE(SUM(s.goals_conceded), 0)     AS goals_conceded,
    COALESCE(SUM(s.saves), 0)              AS saves,
    COALESCE(SUM(s.bonus), 0)              AS bonus,
    COALESCE(SUM(s.yellow_cards), 0)       AS yellow_cards,
    COALESCE(SUM(s.red_cards), 0)          AS red_cards,
    SUM(s.expected_goals)                  AS expected_goals,
    SUM(s.expected_assists)                AS expected_assists,
    CASE WHEN SUM(s.minutes) > 0
         THEN ROUND(SUM(s.total_points)::numeric * 90 / SUM(s.minutes), 2) END
                                           AS points_per_90,
    CASE WHEN ps.end_price > 0
         THEN ROUND(SUM(s.total_points)::numeric / ps.end_price, 2) END
                                           AS points_per_million
FROM core.player_fixture_stat s
JOIN core.player   p   ON p.player_id  = s.player_id
JOIN core.season   se  ON se.season_id = s.season_id
LEFT JOIN core.player_season ps ON ps.player_id = s.player_id AND ps.season_id = s.season_id
LEFT JOIN core.position pos ON pos.position_id = ps.position_id
LEFT JOIN core.team     t   ON t.team_id       = ps.team_id
GROUP BY se.name, p.player_id, p.display_name, p.full_name,
         pos.code, t.name, ps.start_price, ps.end_price;

COMMENT ON VIEW analytics.player_season IS
'Season totals per player, aggregated from match rows. `points_per_million` '
'uses end-of-season price; for value at a point in time use player_gameweek.';

-- Fixtures with names attached and both perspectives available, so "next four
-- fixtures for Arsenal" does not require a UNION the model has to think of.
CREATE OR REPLACE VIEW analytics.fixture AS
SELECT
    f.fixture_id,
    se.name             AS season,
    gw.gameweek_number  AS gameweek,
    f.kickoff_at,
    ht.name             AS home_team,
    ht.short_name       AS home_team_short,
    at.name             AS away_team,
    at.short_name       AS away_team_short,
    f.home_score,
    f.away_score,
    f.home_difficulty,
    f.away_difficulty,
    f.is_finished
FROM core.fixture f
JOIN core.season se ON se.season_id = f.season_id
LEFT JOIN core.gameweek gw ON gw.gameweek_id = f.gameweek_id
JOIN core.team ht ON ht.team_id = f.home_team_id
JOIN core.team at ON at.team_id = f.away_team_id;

-- One row per team per fixture. This is the shape fixture-difficulty questions
-- actually want: "next four fixtures for every defender under 5.0" is a filter
-- and a LIMIT here, rather than a UNION over home and away.
CREATE OR REPLACE VIEW analytics.team_fixture AS
SELECT
    f.fixture_id,
    se.name             AS season,
    gw.gameweek_number  AS gameweek,
    f.kickoff_at,
    t.team_id,
    t.name              AS team,
    t.short_name        AS team_short,
    o.team_id           AS opponent_team_id,
    o.name              AS opponent,
    o.short_name        AS opponent_short,
    v.is_home,
    v.difficulty,
    v.goals_for,
    v.goals_against,
    f.is_finished
FROM core.fixture f
JOIN core.season se ON se.season_id = f.season_id
LEFT JOIN core.gameweek gw ON gw.gameweek_id = f.gameweek_id
CROSS JOIN LATERAL (
    VALUES
        (f.home_team_id, f.away_team_id, TRUE,  f.home_difficulty, f.home_score, f.away_score),
        (f.away_team_id, f.home_team_id, FALSE, f.away_difficulty, f.away_score, f.home_score)
) AS v(team_id, opponent_id, is_home, difficulty, goals_for, goals_against)
JOIN core.team t ON t.team_id = v.team_id
JOIN core.team o ON o.team_id = v.opponent_id;

COMMENT ON VIEW analytics.team_fixture IS
'One row per team per fixture (two rows per match). Use this for fixture '
'difficulty and upcoming-fixture questions; no UNION needed.';

-- Point-in-time price lookup, expressed as a function so the as-of join is
-- something the model calls rather than something it has to derive. The
-- few-shot examples use this for "at gameweek N" questions.
CREATE OR REPLACE FUNCTION analytics.price_as_of(
    p_player_id INTEGER,
    p_as_of     TIMESTAMPTZ
) RETURNS NUMERIC
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT h.price
    FROM core.player_price_history h
    WHERE h.player_id = p_player_id
      AND h.valid_from <= p_as_of
      AND (h.valid_to > p_as_of OR h.valid_to IS NULL)
    LIMIT 1;
$$;

COMMENT ON FUNCTION analytics.price_as_of IS
'Price in millions that a player carried at a given instant. Use for "at '
'gameweek N" and "when you bought them" questions; do not use current price.';

CREATE OR REPLACE VIEW analytics.player_price_history AS
SELECT
    p.player_id,
    p.display_name AS player_name,
    se.name        AS season,
    h.price,
    h.valid_from,
    h.valid_to
FROM core.player_price_history h
JOIN core.player p  ON p.player_id  = h.player_id
JOIN core.season se ON se.season_id = h.season_id;
