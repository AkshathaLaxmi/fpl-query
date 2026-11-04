-- 012_stat_source.sql — who wrote each player-match row (FPL-29).
--
-- Two sources now write core.player_fixture_stat: the archive (fpl_archive,
-- loaded by season) and the live API's element-summary history (fpl_api, for
-- the current season, where the archive mirror lags). They follow the same
-- handoff as price history: the archive owns history, live fills the gap.
--
--   * An archive load writes every row it has and overwrites anything,
--     including rows live wrote before the mirror caught up. The archive
--     carries expected_points (xP), which the API's history does not.
--   * A live run inserts rows the archive does not have yet, and refreshes
--     rows it wrote itself (FPL corrects bonus points after a match), but
--     never overwrites an archive row.
--
-- Rows that exist before this migration all came from the archive.

ALTER TABLE core.player_fixture_stat
    ADD COLUMN source TEXT NOT NULL DEFAULT 'fpl_archive'
        CHECK (source IN ('fpl_archive', 'fpl_api'));
