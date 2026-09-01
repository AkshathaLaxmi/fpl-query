-- Example queries: the five questions from the project brief, answered.
--
-- These exist for three reasons, in ascending order of importance:
--
--   1. They prove the schema can actually answer the questions the service
--      promises. A data model that looks right and cannot answer the brief is a
--      failure discovered far too late.
--   2. They are the seed of the golden set. Each one becomes a
--      question -> expected-result-set pair, and execution accuracy is measured
--      by comparing result sets, not SQL strings -- two different queries can
--      both be correct.
--   3. They are the few-shot examples the retrieval layer serves. The model
--      learns the as-of join pattern from here rather than inventing it.
--
-- Every query runs as fplq_reader, against the analytics schema only.

-- ---------------------------------------------------------------------------
-- Q1. "Which defenders under £5.0m have the best fixtures over the next four
--      gameweeks?"
--
-- Tests: current-season filter, price filter, forward-looking fixtures, and the
-- team_fixture view earning its place (no UNION over home/away).
-- ---------------------------------------------------------------------------

WITH next_four AS (
    SELECT team, AVG(difficulty)::numeric(3,2) AS avg_difficulty,
           COUNT(*) AS fixtures,
           string_agg(opponent_short || CASE WHEN is_home THEN ' (H)' ELSE ' (A)' END,
                      ', ' ORDER BY gameweek) AS run
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY team ORDER BY gameweek) AS rn
        FROM analytics.team_fixture
        WHERE season = '2026-27' AND NOT is_finished
    ) upcoming
    WHERE rn <= 4
    GROUP BY team
),
cheap_defenders AS (
    SELECT DISTINCT ON (player_name, team)
           player_name, team, price
    FROM analytics.player_gameweek
    WHERE season = '2026-27' AND position = 'DEF' AND price < 5.0
    ORDER BY player_name, team, gameweek DESC
)
SELECT d.player_name, d.team, d.price, f.avg_difficulty, f.run
FROM cheap_defenders d
JOIN next_four f ON f.team = d.team
ORDER BY f.avg_difficulty, d.price
LIMIT 10;

-- ---------------------------------------------------------------------------
-- Q2. "Who has the most points per million among midfielders this season?"
--
-- Tests: aggregation over the season, and the value trap -- points per million
-- must divide by the price the player carries now, not the price at each
-- gameweek, or a player whose price rose looks worse than they were.
-- ---------------------------------------------------------------------------

SELECT player_name, team, points, current_price,
       ROUND(points / current_price, 2) AS points_per_million
FROM analytics.player_season
WHERE season = '2025-26' AND position = 'MID' AND minutes >= 900
ORDER BY points_per_million DESC
LIMIT 10;

-- ---------------------------------------------------------------------------
-- Q3. "Compare Salah and Saka's home vs away returns since 2023/24."
--
-- Tests: multi-season span, per-player pivot, home/away split. The season
-- filter is a string comparison on a 'YYYY-YY' name, which is a real trap --
-- it happens to sort correctly, and that is worth knowing rather than assuming.
-- ---------------------------------------------------------------------------

SELECT player_full_name,
       CASE WHEN was_home THEN 'home' ELSE 'away' END AS venue,
       COUNT(*) FILTER (WHERE minutes > 0) AS matches,
       SUM(points) AS points,
       SUM(goals_scored) AS goals,
       SUM(assists) AS assists,
       ROUND(AVG(points), 2) AS points_per_match
FROM analytics.player_gameweek
-- player_search, not player_name: FPL's short name for Mohamed Salah is
-- "M.Salah", so equality on player_name returns Saka and silently drops Salah.
-- See sql/005_player_lookup.sql.
WHERE player_search LIKE ANY (ARRAY['%mohamed salah%', '%bukayo saka%'])
  AND season >= '2023-24'
  AND minutes > 0
GROUP BY player_full_name, was_home
ORDER BY player_full_name, venue;

-- ---------------------------------------------------------------------------
-- Q4. "Which players were underpriced at gameweek 5 relative to what they
--      scored in the next five?"
--
-- The point-in-time question, and the one a current-price column cannot answer
-- at all. The price is read at gameweek 5; the points are summed over gameweeks
-- 6-10. Getting these two from the same row would be the natural mistake and
-- would be wrong.
-- ---------------------------------------------------------------------------

WITH price_at_gw5 AS (
    SELECT DISTINCT ON (player_id)
           player_id, player_name, position, team, price
    FROM analytics.player_gameweek
    WHERE season = '2025-26' AND gameweek = 5
    ORDER BY player_id, kickoff_at
),
next_five AS (
    SELECT player_id,
           SUM(points) AS points_gw6_10,
           SUM(minutes) AS minutes_gw6_10
    FROM analytics.player_gameweek
    WHERE season = '2025-26' AND gameweek BETWEEN 6 AND 10
    GROUP BY player_id
)
SELECT p.player_name, p.position, p.team,
       p.price AS price_at_gw5,
       n.points_gw6_10,
       ROUND(n.points_gw6_10 / p.price, 2) AS points_per_million_gw6_10
FROM price_at_gw5 p
JOIN next_five n USING (player_id)
WHERE n.minutes_gw6_10 >= 200
ORDER BY points_per_million_gw6_10 DESC
LIMIT 10;

-- ---------------------------------------------------------------------------
-- Q5. "Which teams concede the most goals in the last fifteen minutes?"
--
-- Deliberately included as the one that must be REFUSED. Minute-level goal
-- timings are not in the FPL feed at all -- the data has goals per match, not
-- goals per minute. The correct behaviour is to say so, not to answer a
-- neighbouring question and let the user assume it was the one they asked.
--
-- The closest honest answer available is goals conceded per match, which is a
-- different question and must be labelled as one:
-- ---------------------------------------------------------------------------

SELECT team,
       COUNT(*) AS matches,
       SUM(goals_against) AS goals_conceded,
       ROUND(AVG(goals_against), 2) AS conceded_per_match
FROM analytics.team_fixture
WHERE season = '2025-26' AND is_finished
GROUP BY team
ORDER BY conceded_per_match DESC
LIMIT 10;

-- ---------------------------------------------------------------------------
-- Q6. Point-in-time correctness, checked directly.
--
-- Not from the brief -- this is the assertion the whole price_history design
-- exists to support. For any instant, exactly one price interval is live per
-- player. If this ever returns rows, the SCD is broken.
-- ---------------------------------------------------------------------------

SELECT player_name, price, valid_from, valid_to
FROM analytics.player_price_history
WHERE player_name = 'M.Salah' AND season = '2025-26'
ORDER BY valid_from
LIMIT 10;
