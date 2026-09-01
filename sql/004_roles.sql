-- 004_roles.sql — the database half of the validator.
--
-- The application connects as two different users. Generated SQL is executed as
-- fplq_reader and nothing else. This is defence in depth: the validator in
-- Python is the first line, but a validator bug should not be able to become a
-- data-loss incident. If the model emits DELETE, the database refuses it even
-- if every check above it failed.
--
-- The roles themselves are created by `fplq bootstrap`, not here: roles are
-- cluster-level objects, so creating them inside a per-database migration is
-- the wrong scope and produces a chicken-and-egg (the migration cannot run as
-- a role the migration creates). This file only grants. No password is ever
-- committed to the repo; bootstrap reads them from the environment.

-- --- writer: owns the schemas, used only by the ingestion pipeline -----------

GRANT USAGE, CREATE ON SCHEMA raw, core, analytics TO fplq_writer;
GRANT ALL ON ALL TABLES    IN SCHEMA raw, core, analytics TO fplq_writer;
GRANT ALL ON ALL SEQUENCES IN SCHEMA raw, core, analytics TO fplq_writer;

-- --- reader: the identity generated SQL runs as ------------------------------
--
-- Deliberately NOT granted anything on raw or core. The model is told about
-- analytics, the allow-list permits analytics, and the role can only reach
-- analytics. Three independent layers, so no single mistake is sufficient.

REVOKE ALL ON SCHEMA public FROM PUBLIC;

GRANT USAGE  ON SCHEMA analytics TO fplq_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO fplq_reader;
GRANT EXECUTE ON FUNCTION analytics.price_as_of(INTEGER, TIMESTAMPTZ) TO fplq_reader;

-- Views are defined over core, so the reader needs to be able to see through
-- them without being able to query core directly. Views run with the owner's
-- privileges by default, which is exactly what we want here.
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
    GRANT SELECT ON TABLES TO fplq_reader;

-- The reader's session settings -- statement_timeout, read-only transactions,
-- a search_path that cannot see core -- are applied by `fplq bootstrap`.
-- ALTER ROLE requires privileges the writer deliberately does not have, and
-- giving the writer role-administration rights to tidy up a migration file
-- would be a worse trade than splitting the two steps.
