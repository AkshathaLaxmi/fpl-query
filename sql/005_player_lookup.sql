-- 005_player_lookup.sql — make player names findable the way people say them.
--
-- Found by running the brief's own example question against real data:
--
--     WHERE player_name IN ('Salah', 'Saka')
--
-- returns Saka and not Salah, because FPL's short name for him is "M.Salah".
-- Silently. With no error, and a perfectly plausible-looking result set that is
-- missing half the answer.
--
-- This is the exact failure mode the evaluation section of the brief is about:
-- not SQL that fails to run, but SQL that runs and is wrong. A model asked
-- about Salah will write 'Salah', because that is what a human says. Fixing it
-- in the prompt would be fixing it in the least reliable place available.
--
-- So the schema absorbs it. Three additions:
--
--   1. analytics.player       -- a dimension to look names up in, with every
--                                form of the name the sources use.
--   2. analytics.find_player  -- one call that takes what a human typed and
--                                returns matching player_ids, ranked.
--   3. player_search on the   -- a single pre-concatenated column, so the
--      gameweek view             common case is one ILIKE and no join.
--
-- The few-shot examples teach find_player() rather than equality on
-- player_name, and the glossary says plainly that player_name is a short form.

-- The search text for a player: every name form, lowercased and de-accented,
-- concatenated. 'M.Salah Mohamed Salah salah mohamed' matches 'salah',
-- 'mohamed salah' and 'm.salah' alike.
CREATE OR REPLACE FUNCTION analytics.player_search_text(
    p_display TEXT, p_full TEXT, p_first TEXT, p_last TEXT
) RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT lower(
        translate(
            concat_ws(' ', p_display, p_full, p_first, p_last),
            'àáâãäåçèéêëìíîïñòóôõöùúûüýÿšžćčđłńřśťžÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝ',
            'aaaaaaceeeeiiiinooooouuuuyyszccdlnrstzAAAAAACEEEEIIIINOOOOOUUUUY'
        )
    );
$$;

CREATE OR REPLACE VIEW analytics.player AS
SELECT
    p.player_id,
    p.display_name AS player_name,
    p.full_name,
    p.first_name,
    p.last_name,
    analytics.player_search_text(p.display_name, p.full_name,
                                 p.first_name, p.last_name) AS player_search,
    p.birth_date,
    (SELECT min(se.name) FROM core.player_season ps
       JOIN core.season se ON se.season_id = ps.season_id
      WHERE ps.player_id = p.player_id)  AS first_season,
    (SELECT max(se.name) FROM core.player_season ps
       JOIN core.season se ON se.season_id = ps.season_id
      WHERE ps.player_id = p.player_id)  AS last_season,
    (SELECT t.name FROM core.player_season ps
       JOIN core.season se ON se.season_id = ps.season_id
       JOIN core.team t    ON t.team_id    = ps.team_id
      WHERE ps.player_id = p.player_id
      ORDER BY se.start_year DESC LIMIT 1) AS current_team
FROM core.player p;

COMMENT ON VIEW analytics.player IS
'Player dimension. `player_name` is FPL''s SHORT name and is often not what a '
'person would type -- Mohamed Salah is "M.Salah". Never match a person''s '
'name with equality on player_name; use analytics.find_player() instead.';

-- Takes what a human typed, returns candidate player_ids best-first.
--
-- Deliberately returns a set rather than one id: "Silva" is several people and
-- the honest response to an ambiguous name is to notice, not to pick. The
-- clarification step upstream is what asks which one; this function is what
-- gives it something to ask about.
CREATE OR REPLACE FUNCTION analytics.find_player(q TEXT)
RETURNS TABLE (player_id INTEGER, player_name TEXT, full_name TEXT,
               current_team TEXT, last_season TEXT, match_rank INTEGER)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH needle AS (
        SELECT lower(
            translate(btrim(q),
                'àáâãäåçèéêëìíîïñòóôõöùúûüýÿšžćčđłńřśťž',
                'aaaaaaceeeeiiiinooooouuuuyyszcccdlnrstz')
        ) AS text
    )
    SELECT p.player_id, p.player_name, p.full_name, p.current_team, p.last_season,
           CASE
               -- Exact on either full or short name wins outright.
               WHEN lower(p.full_name)   = n.text THEN 1
               WHEN lower(p.player_name) = n.text THEN 2
               -- Then a whole-word hit, so 'son' finds Son and not Robertson.
               WHEN p.player_search ~ ('\y' || n.text || '\y') THEN 3
               -- Then a substring, which is how 'salah' finds 'M.Salah'.
               ELSE 4
           END AS match_rank
    FROM analytics.player p, needle n
    WHERE p.player_search LIKE '%' || n.text || '%'
    ORDER BY match_rank, p.last_season DESC NULLS LAST, p.full_name;
$$;

COMMENT ON FUNCTION analytics.find_player IS
'Resolve a name a human typed to player_ids, best match first. Returns MANY '
'rows when the name is ambiguous ("Silva") -- that is a signal to ask the user '
'which one, not to take the first row.';

-- The one-ILIKE path, for the common case where a question names a player and
-- a join to the dimension would just be ceremony.
--
-- Dropped and recreated rather than replaced: CREATE OR REPLACE VIEW can only
-- append columns to the end of the list, and player_search belongs next to the
-- other name columns where a reader of the schema docs will find it. The whole
-- migration runs in one transaction, so the views are never missing.
DROP VIEW IF EXISTS analytics.player_gameweek;
CREATE VIEW analytics.player_gameweek AS
SELECT
    s.stat_id,
    se.name                 AS season,
    gw.gameweek_number      AS gameweek,
    gw.deadline_at          AS gameweek_deadline,
    p.player_id,
    p.display_name          AS player_name,
    p.full_name             AS player_full_name,
    analytics.player_search_text(p.display_name, p.full_name,
                                 p.first_name, p.last_name) AS player_search,
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
    s.minutes, s.starts, s.goals_scored, s.assists, s.clean_sheets,
    s.goals_conceded, s.own_goals, s.penalties_saved, s.penalties_missed,
    s.yellow_cards, s.red_cards, s.saves, s.bonus, s.bps,
    s.influence, s.creativity, s.threat, s.ict_index,
    s.expected_goals, s.expected_assists, s.expected_goal_involvements,
    s.expected_goals_conceded, s.expected_points,
    s.tackles, s.recoveries, s.clearances_blocks_interceptions,
    s.defensive_contribution,
    s.transfers_in, s.transfers_out,
    CASE WHEN s.price_at_deadline > 0
         THEN ROUND(s.total_points / s.price_at_deadline, 3) END
                            AS points_per_million
FROM core.player_fixture_stat s
JOIN core.player   p   ON p.player_id  = s.player_id
JOIN core.season   se  ON se.season_id = s.season_id
LEFT JOIN core.gameweek gw  ON gw.gameweek_id  = s.gameweek_id
LEFT JOIN core.fixture  f   ON f.fixture_id    = s.fixture_id
LEFT JOIN core.position pos ON pos.position_id = s.position_id
LEFT JOIN core.team     t   ON t.team_id       = s.team_id
LEFT JOIN core.team     opp ON opp.team_id     = s.opponent_team_id;

COMMENT ON VIEW analytics.player_gameweek IS
'One row per player per match played. Grain is player x fixture, so a double '
'gameweek gives a player two rows for the same gameweek number. `price` is the '
'price at that gameweek''s deadline, not the current price. To filter by a '
'player a human named, use `player_search ILIKE ''%salah%''` -- `player_name` '
'is FPL''s short form ("M.Salah") and equality on it silently returns nothing.';

DROP VIEW IF EXISTS analytics.player_season;
CREATE VIEW analytics.player_season AS
SELECT
    se.name                 AS season,
    p.player_id,
    p.display_name          AS player_name,
    p.full_name             AS player_full_name,
    analytics.player_search_text(p.display_name, p.full_name,
                                 p.first_name, p.last_name) AS player_search,
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
GROUP BY se.name, p.player_id, p.display_name, p.full_name, p.first_name, p.last_name,
         pos.code, t.name, ps.start_price, ps.end_price;

GRANT SELECT ON analytics.player, analytics.player_gameweek,
                analytics.player_season TO fplq_reader;
GRANT EXECUTE ON FUNCTION analytics.find_player(TEXT) TO fplq_reader;
GRANT EXECUTE ON FUNCTION analytics.player_search_text(TEXT, TEXT, TEXT, TEXT) TO fplq_reader;
