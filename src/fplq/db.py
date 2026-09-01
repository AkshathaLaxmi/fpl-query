"""Database access.

Thin wrapper over psycopg. No ORM: the whole project is about SQL being a
first-class artifact, and hiding it behind a mapper would be working against
the point.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from fplq.config import REPO_ROOT, require_dsn, settings

log = logging.getLogger(__name__)

SQL_DIR = REPO_ROOT / "sql"


@contextmanager
def connect(dsn: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection with dict rows. Commits on clean exit, rolls back otherwise."""
    conn = psycopg.connect(
        require_dsn(dsn or settings.writer_dsn, "FPLQ_WRITER_DSN"), row_factory=dict_row
    )
    conn.autocommit = autocommit
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: Sequence[Any] | dict[str, Any] | None = None,
          *, dsn: str | None = None) -> list[dict[str, Any]]:
    """Run a read query and return all rows."""
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def scalar(sql: str, params: Sequence[Any] | dict[str, Any] | None = None,
           *, dsn: str | None = None) -> Any:
    rows = query(sql, params, dsn=dsn)
    if not rows:
        return None
    return next(iter(rows[0].values()))


MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migrate(conn: psycopg.Connection, *, sql_dir: Path = SQL_DIR) -> list[str]:
    """Apply any .sql file that has not been applied yet, in filename order.

    Plain numbered files applied by fifty lines of code rather than a migration
    framework: at this size a framework is more moving parts than the problem
    has. What is not optional is the ledger -- without it, `bootstrap` is a
    command you can only run once, which makes rebuilding a broken environment
    a manual exercise exactly when you least want one.

    Each file is applied in its own transaction, so a failure names the file
    that broke and leaves the ones before it applied. The checksum is recorded
    and verified: editing a migration that has already run is a mistake worth
    an error rather than a silent divergence between environments.
    """
    with conn.cursor() as cur:
        cur.execute(MIGRATION_TABLE)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migration")
        already = {row["filename"]: row["checksum"] for row in cur.fetchall()}

    applied: list[str] = []
    for path in sorted(sql_dir.glob("*.sql")):
        body = path.read_text()
        checksum = hashlib.sha256(body.encode()).hexdigest()

        if path.name in already:
            if already[path.name] != checksum:
                raise RuntimeError(
                    f"{path.name} has changed since it was applied. "
                    "Add a new migration rather than editing an applied one."
                )
            log.debug("skipping %s (already applied)", path.name)
            continue

        log.info("applying %s", path.name)
        with conn.cursor() as cur:
            cur.execute(body)
            cur.execute(
                "INSERT INTO schema_migration (filename, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
        conn.commit()
        applied.append(path.name)

    return applied
