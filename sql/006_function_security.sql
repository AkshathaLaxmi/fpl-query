-- 006_function_security.sql — let the reader call price_as_of().
--
-- Found by calling it as fplq_reader:
--
--     ERROR: permission denied for schema core
--
-- Views and functions differ here in a way that is easy to miss. A view runs
-- with its *owner's* privileges, which is what lets fplq_reader select from
-- analytics.player_gameweek without any grant on core. A SQL function does not
-- -- by default it runs as the *caller*, so analytics.price_as_of() hits core
-- as the reader and is refused.
--
-- The fix is SECURITY DEFINER, which is a sharp tool and is used here with the
-- two precautions that make it safe:
--
--   1. A pinned search_path. Without it, a caller who can create objects could
--      shadow `core` and have the function resolve to their table instead --
--      the classic SECURITY DEFINER escalation. The reader cannot create
--      anything, but relying on that is relying on a grant staying correct
--      forever rather than on the function being safe on its own terms.
--   2. A narrow body. The function takes an integer and a timestamp and returns
--      one numeric. There is no string interpolation and nothing dynamic, so
--      there is no surface to inject into.
--
-- What it deliberately does NOT become is a general escape hatch. It reads one
-- column of one table. Anything broader belongs in a view, where owner
-- privileges apply without this ceremony.

CREATE OR REPLACE FUNCTION analytics.price_as_of(
    p_player_id INTEGER,
    p_as_of     TIMESTAMPTZ
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = core, pg_temp
AS $$
    SELECT h.price
    FROM core.player_price_history h
    WHERE h.player_id = p_player_id
      AND h.valid_from <= p_as_of
      AND (h.valid_to > p_as_of OR h.valid_to IS NULL)
    ORDER BY h.valid_from DESC
    LIMIT 1;
$$;

COMMENT ON FUNCTION analytics.price_as_of IS
'Price in millions that a player carried at a given instant. Use for "at '
'gameweek N" and "when you bought them" questions; do not use current price. '
'Returns NULL if the player had no price at that time (before their first '
'season, or between spells).';

-- Revoke from PUBLIC first: a SECURITY DEFINER function is executable by
-- everyone by default, which is not what "grant it to the reader" should mean.
REVOKE ALL ON FUNCTION analytics.price_as_of(INTEGER, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.price_as_of(INTEGER, TIMESTAMPTZ) TO fplq_reader;

-- find_player and player_search_text read only analytics views and their own
-- arguments, so they need no elevation and deliberately do not get any.
