"""The validator: the gate every generated statement passes through.

The database grants are the hard boundary -- `fplq_reader` can read the
`analytics` schema and nothing else, and holds no write privilege anywhere.
This module is the layer above that boundary, and it exists because the
reader's *other* protections are not protections at all: `statement_timeout`,
`default_transaction_read_only` and `search_path` are `USERSET` parameters, so
a session that can get `SET statement_timeout = '1h'` to the server simply
raises them. The validator is what stops the `SET` from arriving.

The shape of the check is deliberate:

  * Parse with a real parser, never a regex. Every regex-based SQL filter ever
    written has been defeated by a comment, a newline, a nested block comment
    or a unicode escape. `sqlglot` is pure Python (no native build to package
    into a Lambda), reads the Postgres dialect, and gives an AST that can be
    walked rather than a string that has to be guessed at.
  * Decide on the tree, not the text. Statement count, statement kind, table
    names and function names are all read off the parsed tree.
  * Execute what was validated, not what was submitted. The SQL that comes back
    is *regenerated from the AST*, with comments dropped and the `LIMIT`
    already applied. There is no path by which a byte that the checks did not
    see reaches the server.
  * Allow-list, not block-list, wherever the set is knowable. Schemas and
    unrecognised function names are allow-listed; only the syntax categories
    (whose set really is closed) are listed as forbidden.

None of this replaces the grants. A validator bug should be a bug, not an
incident, which is why the reader still cannot see `core` even if every check
here were to pass something through.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

import sqlglot
from sqlglot import exp

from fplq import db
from fplq.config import require_dsn, settings

log = logging.getLogger(__name__)

DIALECT = "postgres"

# The only schema the generated SQL may name. It matches the reader's grants
# and the schema the retrieval corpus describes; the three are kept in step on
# purpose, so that no single one of them being wrong is sufficient.
ALLOWED_SCHEMAS = frozenset({"analytics"})

# sqlglot gives standard SQL functions (SUM, COUNT, COALESCE, EXTRACT, ...)
# their own node types; anything it does not recognise becomes an `Anonymous`
# node carrying the raw name. That split is exactly the security boundary we
# want: the typed functions are the SQL standard and are safe, and everything
# else -- `pg_read_file`, `pg_sleep`, `dblink`, `set_config`, `query_to_xml`,
# `lo_import` -- has to be named here to be callable. This list is therefore
# the set of *our own* functions, and it is short by design.
ALLOWED_FUNCTIONS = frozenset(
    {
        "price_as_of",
        "find_player",
        "player_search_text",
        "like_literal",
    }
)

# Syntax that is never acceptable inside a read query. Unlike the schema and
# function rules this one is a block-list, because the set of statement kinds
# in SQL is closed and small -- and because `exp.Command`, the first entry,
# catches the open end of it: sqlglot parses anything it does not understand
# into a Command rather than failing, and "we did not understand this" is the
# one answer that must never be executed.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Command,
    exp.Set,
    exp.SetItem,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Into,               # SELECT ... INTO writes a table
    exp.Lock,               # FOR UPDATE takes row locks
    exp.Copy,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.UserDefinedFunction,
    exp.Parameter,          # $1 -- we bind no parameters; a placeholder is a smell
    exp.Placeholder,
    exp.SessionParameter,
)

# Statement kinds that may appear at the root. A set operation (UNION, EXCEPT,
# INTERSECT) is allowed because its branches are themselves checked by the same
# walk below.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.SetOperation,
    exp.Subquery,
)


class ValidationError(ValueError):
    """A statement was refused.

    Carries a stable `code` as well as a message: the codes are what the tests
    pin and what the query log records, so that "what got rejected and why"
    stays answerable without parsing English.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedQuery:
    """The statement that is safe to execute -- not the one that was submitted."""

    sql: str
    limit: int
    estimated_cost: float | None = None


@contextmanager
def reader_connection() -> Iterator[Any]:
    """Connect as `fplq_reader`, the role generated SQL runs as."""
    dsn = require_dsn(settings.reader_dsn, "FPLQ_READER_DSN")
    with db.connect(dsn) as conn:
        yield conn


@dataclass(frozen=True)
class Validator:
    """Static checks and the cost gate, with the thresholds in one place."""

    allowed_schemas: frozenset[str] = ALLOWED_SCHEMAS
    allowed_functions: frozenset[str] = ALLOWED_FUNCTIONS

    # A cap, not a suggestion: a query asking for more rows than this has its
    # LIMIT lowered rather than being rejected, because the usual cause is a
    # model writing LIMIT 100000 out of habit on a question that a hundred rows
    # answers.
    max_limit: int = 500

    # EXPLAIN's arbitrary cost units. The number is a ceiling on "how much work
    # would this be", checked *before* execution -- a statement timeout only
    # notices after the server has already spent ten seconds on it, and only if
    # the session did not raise the timeout first.
    max_cost: float = 50_000.0

    # Nothing legitimate the generator produces is anywhere near this long;
    # very long input is either an injection payload or a runaway generation,
    # and parsing it is work done on behalf of neither.
    max_length: int = 8_000

    # -- static checks ----------------------------------------------------

    def check(self, sql: str) -> ValidatedQuery:
        """Run every check that does not need a database.

        Returns the statement to execute -- regenerated from the AST, with a
        LIMIT applied -- or raises ValidationError.
        """
        text = sql.strip()
        if not text:
            raise ValidationError("empty", "Empty statement.")
        if len(text) > self.max_length:
            raise ValidationError(
                "too_long",
                f"Statement is {len(text)} characters; the limit is {self.max_length}.",
            )

        root = self._parse_single(text)

        if not isinstance(root, _ALLOWED_ROOTS):
            raise ValidationError(
                "not_a_select",
                f"Only SELECT is allowed; this is {type(root).__name__.upper()}.",
            )

        self._reject_forbidden_syntax(root)
        self._check_functions(root)
        self._check_tables(root)
        limited, effective_limit = self._apply_limit(root)

        return ValidatedQuery(
            sql=limited.sql(dialect=DIALECT, comments=False),
            limit=effective_limit,
        )

    def _parse_single(self, text: str) -> exp.Expression:
        """Parse, and insist on exactly one statement.

        This is where `;`-chaining dies, including the versions that hide the
        separator in a comment: the parser sees the comment as a comment, so
        `SELECT 1 -- x\\n; SET statement_timeout = '1h'` parses as two
        statements and is refused for being two, not for containing a SET.
        """
        try:
            statements = [s for s in sqlglot.parse(text, dialect=DIALECT) if s is not None]
        except sqlglot.errors.ParseError as err:
            # The message carries the offending token and position, which is
            # what makes a rejection actionable when a human reads the log.
            raise ValidationError("parse_error", f"Could not parse as SQL: {err}") from err

        if not statements:
            raise ValidationError("empty", "No statement found.")
        if len(statements) > 1:
            raise ValidationError(
                "multiple_statements",
                f"Expected exactly one statement, found {len(statements)}.",
            )
        return statements[0]

    def _reject_forbidden_syntax(self, root: exp.Expression) -> None:
        for node in root.walk():
            if isinstance(node, _FORBIDDEN_NODES):
                raise ValidationError(
                    "forbidden_syntax",
                    f"{type(node).__name__.upper()} is not allowed in a generated query.",
                )

    def _check_functions(self, root: exp.Expression) -> None:
        """Every unrecognised function name must be one of ours."""
        for node in root.find_all(exp.Anonymous):
            name = str(node.this).lower()
            schema = self._dotted_prefix(node)
            if schema is not None and schema not in self.allowed_schemas:
                raise ValidationError(
                    "forbidden_schema",
                    f"Function {schema}.{name}() is outside the allowed schemas.",
                )
            if name not in self.allowed_functions:
                raise ValidationError(
                    "forbidden_function",
                    f"{name}() is not on the allow-list of callable functions.",
                )

    @staticmethod
    def _dotted_prefix(node: exp.Expression) -> str | None:
        """The schema a qualified call was written with, if it was qualified.

        sqlglot models `analytics.price_as_of(...)` as a Dot whose right-hand
        side is the call, so the qualifier is on the parent rather than the
        function node.
        """
        parent = node.parent
        if isinstance(parent, exp.Dot):
            return parent.this.name.lower() or None
        return None

    def _check_tables(self, root: exp.Expression) -> None:
        """Only `analytics.<table>` and the query's own CTEs are referenceable.

        Unqualified names are refused rather than resolved. Letting them
        through would make the check depend on `search_path`, which is a
        `USERSET` parameter -- exactly the class of thing this module exists to
        not rely on.
        """
        cte_names = {cte.alias.lower() for cte in root.find_all(exp.CTE)}

        for table in root.find_all(exp.Table):
            # A table-valued function -- `FROM pg_read_file(...)`,
            # `FROM generate_series(...)` -- parses as a Table whose `this` is
            # a function rather than an identifier, and has no name to check.
            if not isinstance(table.this, exp.Identifier):
                raise ValidationError(
                    "forbidden_function",
                    "Table functions are not allowed in FROM.",
                )

            name = table.name.lower()
            schema = table.db.lower()
            catalog = table.catalog.lower()

            if catalog:
                raise ValidationError(
                    "forbidden_schema",
                    f"Cross-database references are not allowed: {catalog}.{schema}.{name}.",
                )
            if not schema:
                if name in cte_names:
                    continue
                raise ValidationError(
                    "forbidden_schema",
                    f"Table {name} must be schema-qualified, e.g. analytics.{name}.",
                )
            if schema not in self.allowed_schemas:
                raise ValidationError(
                    "forbidden_schema",
                    f"Schema {schema} is not readable; only {', '.join(sorted(self.allowed_schemas))}.",
                )

    def _apply_limit(self, root: exp.Expression) -> tuple[exp.Expression, int]:
        """Inject a LIMIT, or cap the one that is there."""
        limit = root.args.get("limit")

        if limit is None:
            return root.limit(self.max_limit), self.max_limit

        if not isinstance(limit, exp.Limit):
            # FETCH FIRST n ROWS ONLY and friends. Legal SQL, but a second
            # spelling of the same thing is a second thing to get right; the
            # generator is told to write LIMIT.
            raise ValidationError("bad_limit", "Use LIMIT n rather than FETCH.")

        value = limit.expression
        if not isinstance(value, exp.Literal) or not value.is_int:
            raise ValidationError("bad_limit", "LIMIT must be an integer literal.")

        requested = int(value.name)
        if requested <= 0:
            raise ValidationError("bad_limit", "LIMIT must be positive.")

        if requested > self.max_limit:
            return root.limit(self.max_limit), self.max_limit
        return root, requested

    # -- the cost gate ----------------------------------------------------

    def explain_cost(self, sql: str, conn: Any) -> float:
        """Total cost of the plan Postgres would use, without running it.

        Plain EXPLAIN plans but does not execute -- ANALYZE is what executes,
        and is deliberately not used here. The statement passed in must be one
        that `check()` returned: this interpolates it, because EXPLAIN takes a
        statement rather than a parameter, and interpolating anything that has
        not been through the parser would undo the entire module.
        """
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
            row = cur.fetchone()

        if not row:
            raise ValidationError("explain_failed", "EXPLAIN returned no plan.")

        plan = next(iter(row.values())) if isinstance(row, dict) else row[0]
        if isinstance(plan, str):
            plan = json.loads(plan)

        try:
            return float(plan[0]["Plan"]["Total Cost"])
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise ValidationError("explain_failed", f"Unreadable EXPLAIN output: {err}") from err

    def validate(self, sql: str, *, conn: Any | None = None) -> ValidatedQuery:
        """Static checks, then the cost gate. The full gate before execution.

        `conn` should be the connection the query will actually run on, so that
        the plan costed is the plan executed. One is opened as the reader if
        none is given.
        """
        checked = self.check(sql)

        if conn is not None:
            cost = self.explain_cost(checked.sql, conn)
        else:
            with reader_connection() as owned:
                cost = self.explain_cost(checked.sql, owned)

        if cost > self.max_cost:
            raise ValidationError(
                "cost_exceeded",
                f"Estimated cost {cost:,.0f} exceeds the ceiling of {self.max_cost:,.0f}. "
                "Narrow the question -- a season, a team or a gameweek range.",
            )

        log.debug("validated (cost %.0f, limit %d): %s", cost, checked.limit, checked.sql)
        return replace(checked, estimated_cost=cost)


# The configured instance. Callers that want different thresholds construct
# their own rather than mutating this one -- it is frozen for that reason.
validator = Validator()


def check_sql(sql: str) -> ValidatedQuery:
    """Static checks with the default thresholds."""
    return validator.check(sql)


def validate_sql(sql: str, *, conn: Any | None = None) -> ValidatedQuery:
    """Static checks and the cost gate with the default thresholds."""
    return validator.validate(sql, conn=conn)
