"""Configuration, resolved once from the environment.

Deliberately boring. Everything that differs between a laptop, a Lambda and CI
is a field here, so that no module below reads os.environ directly and tests can
construct a Settings object instead of monkeypatching the process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Load .env if present, without overriding anything already in the environment.
# Deployment sets real variables from Secrets Manager and must win; .env is a
# local-development convenience holding credentials `fplq bootstrap` generated.
try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover -- optional at runtime
    pass

# Seasons the community archive covers in the layout we parse. Earlier seasons
# exist but have enough schema drift that they are opt-in rather than default.
ARCHIVE_SEASONS = (
    "2019-20",
    "2020-21",
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
    "2025-26",
    "2026-27",
)

CURRENT_SEASON = "2026-27"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


@dataclass(frozen=True)
class Settings:
    # --- database ---------------------------------------------------------
    # Two DSNs on purpose: the pipeline writes as one role, generated SQL is
    # executed as another that cannot see anything but the analytics schema.
    # No password appears in this file, or anywhere else in the repository.
    #
    # An earlier revision carried a "harmless" development default in these
    # DSNs. That is a bad habit dressed as a convenience: a default password in
    # source is a real credential the moment someone deploys without setting the
    # environment, and it is a known one, published on GitHub. The failure mode
    # is silent -- everything works, and the database is reachable by anyone who
    # read the repo.
    #
    # Instead: `fplq bootstrap` generates a random password per role on first
    # run and writes it to .env, which is gitignored. Nothing is shared, nothing
    # is guessable, and nothing is committed. In deployment the environment is
    # populated from Secrets Manager and bootstrap generates nothing.
    writer_dsn: str = field(default_factory=lambda: _env("FPLQ_WRITER_DSN", ""))
    reader_dsn: str = field(default_factory=lambda: _env("FPLQ_READER_DSN", ""))
    admin_dsn: str = field(
        default_factory=lambda: _env(
            "FPLQ_ADMIN_DSN",
            "postgresql://postgres@/postgres?host=/var/run/postgresql",
        )
    )
    database_name: str = field(default_factory=lambda: _env("FPLQ_DATABASE", "fplq"))

    writer_password: str = field(default_factory=lambda: _env("FPLQ_WRITER_PASSWORD", ""))
    reader_password: str = field(default_factory=lambda: _env("FPLQ_READER_PASSWORD", ""))

    # --- raw landing ------------------------------------------------------
    # s3://bucket/prefix in deployment; a local directory in development. The
    # store abstraction in ingest/store.py takes either.
    raw_store_uri: str = field(
        default_factory=lambda: _env("FPLQ_RAW_STORE", str(REPO_ROOT / "data" / "raw"))
    )

    # --- sources ----------------------------------------------------------
    fpl_api_base: str = field(
        default_factory=lambda: _env(
            "FPLQ_FPL_API_BASE", "https://fantasy.premierleague.com/api"
        )
    )
    archive_base: str = field(
        default_factory=lambda: _env(
            "FPLQ_ARCHIVE_BASE",
            "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data",
        )
    )

    # The FPL API is undocumented and unlicensed. We identify ourselves, we go
    # slowly, and we never proxy it live -- see docs/data-sources.md.
    user_agent: str = field(
        default_factory=lambda: _env(
            "FPLQ_USER_AGENT",
            "fpl-query/0.1 (non-commercial research; +https://github.com/akshathalaxmi/fpl-query)",
        )
    )
    request_timeout_s: float = 30.0
    request_delay_s: float = 1.0

    overrides_path: Path = field(
        default_factory=lambda: REPO_ROOT / "data" / "overrides" / "player_aliases.yaml"
    )


settings = Settings()


class ConfigError(RuntimeError):
    """Raised when a required credential is missing, rather than defaulted."""


def require_dsn(dsn: str, name: str) -> str:
    """Return a DSN, or fail with an instruction instead of a stack trace.

    Deliberately not "fall back to something that works". A connection string
    that appears out of nowhere is how a service ends up talking to the wrong
    database, or to the right one with a password from a README.
    """
    if not dsn:
        raise ConfigError(
            f"{name} is not set. Run `fplq bootstrap` to create a local "
            f"database and generate credentials into .env, or set {name} "
            f"in the environment (from Secrets Manager in deployment)."
        )
    return dsn
