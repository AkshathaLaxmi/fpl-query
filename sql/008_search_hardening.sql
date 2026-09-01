-- 008_search_hardening.sql — stop treating user input as a pattern.
--
-- analytics.find_player() takes a string that comes, by design, from whatever a
-- stranger typed into the service. Until this migration it put that string into
-- two places where characters have meaning:
--
--   1. A LIKE pattern:  player_search LIKE '%' || q || '%'
--      So find_player('%') matched every player -- 2,210 rows -- and '_'
--      matched any single character. Not a data breach (the role can read this
--      view anyway), but it is unbounded output from a one-character input,
--      which is a cost and latency lever a caller should not have.
--
--   2. A POSIX regex:   player_search ~ ('\y' || q || '\y')
--      This one is worse in kind. Regex metacharacters from an untrusted
--      string reach a backtracking engine. Today it is hard to exploit,
--      because the LIKE filter runs first and a string containing regex
--      metacharacters usually matches no rows -- but that is the planner's
--      evaluation order protecting us, not the code. Depending on an optimiser
--      detail for a safety property is how a latent bug becomes an incident
--      after an unrelated version upgrade.
--
-- Both are fixed the same way: escape the input and use LIKE for everything.
-- The whole-word test that needed a regex is done by padding with spaces --
-- ' ' || haystack || ' ' LIKE '% needle %' -- which is exactly as expressive
-- as \y here and involves no pattern language the caller can reach into.
--
-- A LIMIT is added as well. Ambiguity should return a handful of candidates for
-- the clarification step to ask about; it should never return the whole league.

-- Escape the three characters LIKE treats as special, so an input is matched
-- literally. The backslash must be escaped first or it doubles the others.
CREATE OR REPLACE FUNCTION analytics.like_literal(t TEXT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT replace(replace(replace(coalesce(t, ''), '\', '\\'), '%', '\%'), '_', '\_');
$$;

COMMENT ON FUNCTION analytics.like_literal IS
'Escape LIKE metacharacters so untrusted input is matched as a literal string. '
'Every LIKE built from caller input must go through this.';

-- Drop the old single-argument signature FIRST. Postgres overloads on the
-- argument list, so creating the two-argument version alongside it leaves both
-- callable -- including the unescaped one this migration exists to remove --
-- and makes every unqualified reference to the name ambiguous.
DROP FUNCTION IF EXISTS analytics.find_player(TEXT);

CREATE OR REPLACE FUNCTION analytics.find_player(q TEXT, max_results INTEGER DEFAULT 25)
RETURNS TABLE (player_id INTEGER, player_name TEXT, full_name TEXT,
               current_team TEXT, last_season TEXT, match_rank INTEGER)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH needle AS (
        SELECT analytics.fold_name(btrim(q))                            AS text,
               analytics.like_literal(analytics.fold_name(btrim(q)))    AS pattern
    )
    SELECT p.player_id, p.player_name, p.full_name, p.current_team, p.last_season,
           CASE
               WHEN analytics.fold_name(p.full_name)   = n.text THEN 1
               WHEN analytics.fold_name(p.player_name) = n.text THEN 2
               -- Whole-word match without a regex: pad both sides with a space
               -- and look for the padded needle. 'son' hits Son, not Robertson.
               WHEN ' ' || p.player_search || ' ' LIKE '% ' || n.pattern || ' %' ESCAPE '\'
                    THEN 3
               -- Substring, which is how 'salah' finds 'M.Salah'.
               ELSE 4
           END AS match_rank
    FROM analytics.player p, needle n
    WHERE n.text <> ''
      AND p.player_search LIKE '%' || n.pattern || '%' ESCAPE '\'
    ORDER BY match_rank, p.last_season DESC NULLS LAST, p.full_name
    LIMIT greatest(1, least(coalesce(max_results, 25), 100));
$$;

COMMENT ON FUNCTION analytics.find_player(TEXT, INTEGER) IS
'Resolve a name a human typed to player_ids, best match first, capped at '
'max_results (default 25, hard ceiling 100). Input is escaped and matched '
'literally -- it is never treated as a pattern. Returns MANY rows when the '
'name is ambiguous ("Silva"): that is a signal to ask the user which one, not '
'to take the first row.';

REVOKE ALL ON FUNCTION analytics.like_literal(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION analytics.find_player(TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.like_literal(TEXT) TO fplq_reader;
GRANT EXECUTE ON FUNCTION analytics.find_player(TEXT, INTEGER) TO fplq_reader;

-- ---------------------------------------------------------------------------
-- Role hardening
-- ---------------------------------------------------------------------------

-- Temporary-object creation is granted to PUBLIC on every database by default.
-- The execution role has no need of it, and it is a way to consume disk that
-- no query timeout bounds.
REVOKE TEMPORARY ON DATABASE fplq FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE fplq FROM fplq_reader;

-- Stop the reader enumerating the parts of the catalogue it has no business in.
-- (It can still see what it can select, which is what a schema description
-- needs; this only removes the convenience of reading everything.)
REVOKE ALL ON SCHEMA public FROM PUBLIC;
