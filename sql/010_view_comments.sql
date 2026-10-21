-- 010_view_comments.sql — bring the view comments in line with name resolution.
--
-- 005 told the reader to match a person's name with analytics.find_player()
-- (on analytics.player) or with `player_search ILIKE '%salah%'` (on
-- analytics.player_gameweek). That was the plan when the model was expected
-- to find players itself. FPL-9 changed it: names are resolved by the
-- pipeline before generation, the question arrives with "Mohamed Salah =
-- player_id 306", find_player is not on the validator's allow-list, and
-- generated SQL filters on the player_id it is given.
--
-- The model never reads these comments -- it reads corpus/schema.yaml -- but
-- anyone opening the database in psql does, and a comment that contradicts the
-- design is worse than none. So the wording below follows the corpus: the
-- top-level `players:` note and each view's description.
--
-- While here, three views had no comment at all: player_price_history and
-- fixture never got one, and player_season lost the one 003 gave it when 005
-- dropped and recreated the view (a comment belongs to the object, so it goes
-- with the DROP). Each gets its corpus description.
--
-- Comments only; no view definition changes.

COMMENT ON VIEW analytics.player_gameweek IS
'One row per player per match played, with player, team, opponent and fixture '
'already joined. Grain is player x fixture, so a double gameweek gives a player '
'two rows with the same gameweek number. `price` is the price at that '
'gameweek''s deadline, not today''s price. Players named in a question are '
'resolved to a player_id before SQL is generated: filter on player_id, never on '
'player_name ("M.Salah") or player_search.';

COMMENT ON VIEW analytics.player IS
'One row per player across all seasons. `player_name` is FPL''s short display '
'name ("M.Salah"), not what a person types. Never match a person''s name in SQL, '
'with equality on player_name or with LIKE on player_search: players named in a '
'question are resolved to a player_id before SQL is generated, so filter on '
'that. player_search exists for the pipeline''s name resolution.';

COMMENT ON VIEW analytics.player_season IS
'Season totals per player, summed from the same match rows as player_gameweek, '
'so a season total never disagrees with its matches. `points_per_million` uses '
'end-of-season (or current) price; for value at a point in time use '
'player_gameweek or price_as_of(). Filter players on player_id.';

COMMENT ON VIEW analytics.fixture IS
'One row per match, home and away side by side. For anything about one team''s '
'fixtures, use team_fixture instead; here a team appears in either home_team or '
'away_team.';

COMMENT ON VIEW analytics.player_price_history IS
'Every price a player has had, as intervals: the price held from valid_from '
'(inclusive) until valid_to (exclusive; NULL means still current). For the '
'price at one instant, call price_as_of() rather than writing the interval '
'comparison by hand.';
