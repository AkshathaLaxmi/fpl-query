-- 007_letter_folding.sql — fold the letters Unicode decomposition misses.
--
-- The search text built in 005 used a single translate(), which handles any
-- letter that is "a base letter plus a mark" -- é, ñ, ü, š. It does not handle
-- letters that are simply *different letters*: ø, đ, ł, æ, ß have no accented
-- form to strip.
--
-- That is not a theoretical gap. Six players in the current data have ø in
-- their name, including Ødegaard and Højlund, and nobody types the ø:
--
--     SELECT * FROM analytics.find_player('odegaard');   -- 0 rows, before this
--
-- The one that would have hurt is that it fails silently. No error, an empty
-- result set, and a perfectly confident "no data for that player".
--
-- Two mechanisms, because one cannot do both jobs:
--   * replace() for the expansions, where one letter becomes two (æ -> ae,
--     ß -> ss). translate() maps character to character and cannot.
--   * translate() for everything one-to-one, in a single pass.
--
-- Mirrors _LETTER_FOLDS in src/fplq/resolve/names.py deliberately: the Python
-- resolver and the SQL search must agree on what counts as the same name, or
-- ingestion and query disagree about who a player is.

CREATE OR REPLACE FUNCTION analytics.fold_name(t TEXT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT lower(
        translate(
            -- Expansions first: one letter to two.
            replace(replace(replace(replace(replace(replace(
                coalesce(t, ''),
                'æ', 'ae'), 'Æ', 'Ae'),
                'œ', 'oe'), 'Œ', 'Oe'),
                'ß', 'ss'), 'þ', 'th'),
            -- Then everything one-to-one, in one pass: accented forms and the
            -- stroked/barred letters that have no decomposition.
            'àáâãäåçèéêëìíîïñòóôõöùúûüýÿšžćčńřśťžøđðłħıàÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝØĐÐŁĦİ',
            'aaaaaaceeeeiiiinooooouuuuyyszccnrstzoddlhiaAAAAAACEEEEIIIINOOOOOUUUUYODDLHI'
        )
    );
$$;

COMMENT ON FUNCTION analytics.fold_name IS
'Normalise a name for searching: lowercase, strip accents, and fold letters '
'that have no accented decomposition (ø, đ, ł, æ, ß). Mirrors _LETTER_FOLDS '
'in fplq.resolve.names -- change both together.';

-- Rebuild the search text on top of the corrected fold.
CREATE OR REPLACE FUNCTION analytics.player_search_text(
    p_display TEXT, p_full TEXT, p_first TEXT, p_last TEXT
) RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT analytics.fold_name(concat_ws(' ', p_display, p_full, p_first, p_last));
$$;

CREATE OR REPLACE FUNCTION analytics.find_player(q TEXT)
RETURNS TABLE (player_id INTEGER, player_name TEXT, full_name TEXT,
               current_team TEXT, last_season TEXT, match_rank INTEGER)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH needle AS (SELECT analytics.fold_name(btrim(q)) AS text)
    SELECT p.player_id, p.player_name, p.full_name, p.current_team, p.last_season,
           CASE
               WHEN analytics.fold_name(p.full_name)   = n.text THEN 1
               WHEN analytics.fold_name(p.player_name) = n.text THEN 2
               -- Whole word, so 'son' finds Son rather than Robertson.
               WHEN p.player_search ~ ('\y' || n.text || '\y')  THEN 3
               -- Substring, which is how 'salah' finds 'M.Salah'.
               ELSE 4
           END AS match_rank
    FROM analytics.player p, needle n
    WHERE n.text <> '' AND p.player_search LIKE '%' || n.text || '%'
    ORDER BY match_rank, p.last_season DESC NULLS LAST, p.full_name;
$$;

REVOKE ALL ON FUNCTION analytics.fold_name(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.fold_name(TEXT) TO fplq_reader;
GRANT EXECUTE ON FUNCTION analytics.find_player(TEXT) TO fplq_reader;
GRANT EXECUTE ON FUNCTION analytics.player_search_text(TEXT, TEXT, TEXT, TEXT) TO fplq_reader;
