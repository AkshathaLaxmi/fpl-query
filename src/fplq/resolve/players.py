"""Player identity resolution.

The job: given a name (and whatever context the source offers), decide which row
of core.player it refers to -- or decide honestly that we don't know.

Resolution runs as a cascade, most reliable signal first. Each rung records how
it matched, so core.player_source_id carries an audit trail and a bad heuristic
can be measured rather than argued about:

    1. code      FPL's stable cross-season player code. Authoritative when
                 present; this covers the overwhelming majority of rows and is
                 why players_raw.csv is ingested before anything else.
    2. override  The hand-maintained alias file. Beats fuzzy matching by
                 definition -- a human already decided.
    3. exact     Order-independent normalised name key matches exactly one
                 player.
    4. fuzzy     Token-set similarity above threshold, and the runner-up far
                 enough behind that the win is not a coin flip.
    5. unmatched Recorded as a resolution_issue. Not guessed at.

The last rung is the important one. A resolver that always returns something is
a resolver that quietly attributes Salah's goals to someone else. The open-issue
count is a dashboard metric precisely so that gaps get fixed rather than
absorbed.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from fplq.resolve.names import name_key, normalise

log = logging.getLogger(__name__)

# Accept a fuzzy match at or above this score...
FUZZY_ACCEPT = 88.0
# ...but only if the second-best candidate is at least this far behind. Two
# plausible candidates is ambiguity, and ambiguity is an issue to record, not a
# tie to break.
FUZZY_MARGIN = 6.0


@dataclass(frozen=True)
class Candidate:
    player_id: int
    display_name: str
    full_name: str
    key: str


@dataclass
class Match:
    player_id: int | None
    method: str          # 'code' | 'override' | 'exact' | 'fuzzy' | 'unmatched'
    score: float | None = None
    candidates: list[dict[str, object]] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.player_id is not None


def load_overrides(path: Path) -> dict[str, str]:
    """Read the alias file into {normalised alias: canonical full name}.

    Format is deliberately human-first -- the file is edited by a person looking
    at a list of unmatched names:

        aliases:
          "Rodri": "Rodrigo Hernandez Cascante"
          "Son": "Heung-Min Son"
    """
    if not path.exists():
        log.warning("no override file at %s", path)
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    aliases = data.get("aliases") or {}
    return {normalise(alias): canonical for alias, canonical in aliases.items()}


class PlayerResolver:
    """Resolves names against a fixed set of known players.

    Built once per ingestion run from core.player, then queried per row. Holding
    the index in memory is fine at this scale (a few thousand players across all
    seasons) and keeps the cascade a pure function of its inputs, which is what
    makes it testable without a database.
    """

    def __init__(self, candidates: list[Candidate],
                 overrides: dict[str, str] | None = None) -> None:
        self.candidates = candidates
        self.overrides = overrides or {}

        self._by_key: dict[str, list[Candidate]] = defaultdict(list)
        self._by_full: dict[str, Candidate] = {}
        for candidate in candidates:
            self._by_key[candidate.key].append(candidate)
            self._by_full[normalise(candidate.full_name)] = candidate

        # Parallel lists for rapidfuzz's batch scorer.
        self._keys = [c.key for c in candidates]

    # -- the cascade -------------------------------------------------------

    def resolve(self, name: str, *, player_code: int | None = None,
                code_index: dict[int, int] | None = None) -> Match:
        # 1. Authoritative identifier.
        if player_code is not None and code_index and player_code in code_index:
            return Match(code_index[player_code], "code", 100.0)

        key = normalise(name)
        if not key:
            return Match(None, "unmatched")

        # 2. A human already decided.
        if key in self.overrides:
            canonical = normalise(self.overrides[key])
            target = self._by_full.get(canonical)
            if target is None:
                for candidate in self._by_key.get(name_key(self.overrides[key]), []):
                    target = candidate
                    break
            if target is not None:
                return Match(target.player_id, "override", 100.0)
            log.warning("override for %r points at unknown player %r",
                        name, self.overrides[key])

        # 3. Exact, order-independent.
        exact = self._by_key.get(name_key(name), [])
        if len(exact) == 1:
            return Match(exact[0].player_id, "exact", 100.0)
        if len(exact) > 1:
            # Genuinely ambiguous: two players share a normalised name key.
            return Match(None, "unmatched", None, self._describe(exact))

        # 4. Fuzzy, with a margin requirement.
        return self._fuzzy(name)

    def _fuzzy(self, name: str) -> Match:
        if not self._keys:
            return Match(None, "unmatched")
        query = name_key(name)
        scored = process.extract(query, self._keys, scorer=fuzz.token_set_ratio, limit=3)
        if not scored:
            return Match(None, "unmatched")

        _best_key, best_score, best_index = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0

        if best_score >= FUZZY_ACCEPT and (best_score - runner_up) >= FUZZY_MARGIN:
            return Match(self.candidates[best_index].player_id, "fuzzy", float(best_score))

        return Match(
            None, "unmatched", float(best_score),
            [
                {
                    "player_id": self.candidates[index].player_id,
                    "name": self.candidates[index].full_name,
                    "score": float(score),
                }
                for _, score, index in scored
            ],
        )

    @staticmethod
    def _describe(candidates: list[Candidate]) -> list[dict[str, object]]:
        return [
            {"player_id": c.player_id, "name": c.full_name, "score": 100.0}
            for c in candidates
        ]

    # -- construction ------------------------------------------------------

    @classmethod
    def from_rows(cls, rows: list[dict[str, object]],
                  overrides: dict[str, str] | None = None) -> PlayerResolver:
        return cls(
            [
                Candidate(
                    player_id=int(row["player_id"]),          # type: ignore[arg-type]
                    display_name=str(row["display_name"]),
                    full_name=str(row["full_name"]),
                    key=name_key(str(row["full_name"])),
                )
                for row in rows
            ],
            overrides,
        )
