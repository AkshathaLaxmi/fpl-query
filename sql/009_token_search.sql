-- 009_token_search.sql — match all the words, not one contiguous string.
--
-- Found by a test written to prove that escaping input had not broken real
-- lookups. It had not -- but it exposed a gap that predates it:
--
--     find_player('bruno fernandes')  -> 0 rows
--
-- His full name is "Bruno Miguel Borges Fernandes", so the search text reads
-- "fernandes bruno miguel borges fernandes". The words a person typed are both
-- present, and neither the old regex nor the escaped LIKE found him, because
-- both looked for one *contiguous* substring. Two words separated by the
-- player's middle names never match.
--
-- This is the single most natural thing a user types -- a first name and a
-- surname -- and it is a large share of Premier League players, because
-- Portuguese and Brazilian naming conventions put several names in between.
--
-- Fix: require every token of the query to appear, in any order and any
-- position. Contiguous matches still rank above scattered ones, so
-- "Bruno Fernandes" beats a player who merely happens to have both words.
--
-- Still no pattern language reaches the caller: each token is escaped with
-- like_literal before it is used.

CREATE OR REPLACE FUNCTION analytics.find_player(q TEXT, max_results INTEGER DEFAULT 25)
RETURNS TABLE (player_id INTEGER, player_name TEXT, full_name TEXT,
               current_team TEXT, last_season TEXT, match_rank INTEGER)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH needle AS (
        SELECT folded                                        AS text,
               analytics.like_literal(folded)                AS pattern,
               -- Tokens, escaped individually. Empty strings from repeated
               -- spaces are dropped so "a  b" is two tokens, not three.
               ARRAY(
                   SELECT analytics.like_literal(tok)
                   FROM unnest(string_to_array(folded, ' ')) AS tok
                   WHERE tok <> ''
               )                                             AS tokens
        FROM (SELECT analytics.fold_name(btrim(q)) AS folded) f
    )
    SELECT p.player_id, p.player_name, p.full_name, p.current_team, p.last_season,
           CASE
               WHEN analytics.fold_name(p.full_name)   = n.text THEN 1
               WHEN analytics.fold_name(p.player_name) = n.text THEN 2
               -- Whole-word contiguous match: 'son' hits Son, not Robertson.
               WHEN ' ' || p.player_search || ' ' LIKE '% ' || n.pattern || ' %' ESCAPE '\'
                    THEN 3
               -- Contiguous substring: how 'salah' finds 'M.Salah'.
               WHEN p.player_search LIKE '%' || n.pattern || '%' ESCAPE '\'
                    THEN 4
               -- All tokens present but scattered: how 'bruno fernandes' finds
               -- 'Bruno Miguel Borges Fernandes'. Ranked last, so an exact or
               -- contiguous match always wins.
               ELSE 5
           END AS match_rank
    FROM analytics.player p, needle n
    WHERE n.text <> ''
      AND cardinality(n.tokens) > 0
      -- Every token must appear. NOT EXISTS over the tokens rather than a
      -- string_agg of patterns, so a missing token excludes the row outright.
      AND NOT EXISTS (
          SELECT 1 FROM unnest(n.tokens) AS tok
          WHERE p.player_search NOT LIKE '%' || tok || '%' ESCAPE '\'
      )
    ORDER BY match_rank, p.last_season DESC NULLS LAST, p.full_name
    LIMIT greatest(1, least(coalesce(max_results, 25), 100));
$$;

COMMENT ON FUNCTION analytics.find_player(TEXT, INTEGER) IS
'Resolve a name a human typed to player_ids, best match first, capped at '
'max_results (default 25, hard ceiling 100). Every word of the query must '
'appear somewhere in the player''s names, in any order -- so "bruno fernandes" '
'finds "Bruno Miguel Borges Fernandes". Input is escaped and matched '
'literally; it is never treated as a pattern. Returns MANY rows when the name '
'is ambiguous ("silva"): a signal to ask the user which one, not to take the '
'first row.';

REVOKE ALL ON FUNCTION analytics.find_player(TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.find_player(TEXT, INTEGER) TO fplq_reader;
