"""The resolution cascade.

The tests that matter most here are the ones asserting that the resolver
*declines*. A resolver that always returns a player_id will happily attribute
one footballer's goals to another, and every downstream number is then wrong in
a way no schema constraint can catch.
"""

from __future__ import annotations

import pytest

from fplq.resolve.names import name_key
from fplq.resolve.players import Candidate, PlayerResolver

ROWS = [
    {"player_id": 1, "display_name": "M.Salah", "full_name": "Mohamed Salah"},
    {"player_id": 2, "display_name": "Son",     "full_name": "Heung-Min Son"},
    {"player_id": 3, "display_name": "Gabriel", "full_name": "Gabriel dos Santos Magalhaes"},
    {"player_id": 4, "display_name": "Jesus",   "full_name": "Gabriel Fernando de Jesus"},
    {"player_id": 5, "display_name": "Rodri",   "full_name": "Rodrigo Hernandez Cascante"},
    {"player_id": 6, "display_name": "Saka",    "full_name": "Bukayo Saka"},
]


@pytest.fixture
def resolver() -> PlayerResolver:
    return PlayerResolver.from_rows(ROWS, overrides={"rodri": "Rodrigo Hernandez Cascante"})


# --- rung 1: the authoritative identifier -----------------------------------


def test_code_wins_outright(resolver: PlayerResolver) -> None:
    """A stable player code beats every name heuristic, including a wrong name."""
    match = resolver.resolve("Completely Different Name",
                             player_code=99, code_index={99: 1})
    assert (match.player_id, match.method) == (1, "code")


# --- rung 2: the override file ----------------------------------------------


def test_override_resolves_a_nickname(resolver: PlayerResolver) -> None:
    """No string metric gets from 'Rodri' to 'Rodrigo Hernandez Cascante'."""
    match = resolver.resolve("Rodri")
    assert (match.player_id, match.method) == (5, "override")


def test_override_is_accent_insensitive() -> None:
    resolver = PlayerResolver.from_rows(
        [{"player_id": 7, "display_name": "Sane", "full_name": "Leroy Sane"}],
        overrides={"sane": "Leroy Sane"},
    )
    assert resolver.resolve("Sané").player_id == 7


# --- rung 3: exact, order-independent ---------------------------------------


def test_exact_match_ignores_name_order(resolver: PlayerResolver) -> None:
    match = resolver.resolve("Son Heung-min")
    assert (match.player_id, match.method) == (2, "exact")


def test_exact_match_ignores_accents(resolver: PlayerResolver) -> None:
    match = resolver.resolve("Gabriel dos Santos Magalhães")
    assert (match.player_id, match.method) == (3, "exact")


# --- rung 5: declining, which is the important one --------------------------


def test_unknown_name_is_not_guessed(resolver: PlayerResolver) -> None:
    match = resolver.resolve("Someone Not In The League")
    assert not match.matched
    assert match.method == "unmatched"


def test_ambiguous_name_is_declined_with_candidates() -> None:
    """Two people share a normalised key -> record the ambiguity, do not pick.

    This is the case that would otherwise be resolved by whichever row the
    dictionary happened to hold, which is a coin flip dressed as a decision.
    """
    rows = [
        {"player_id": 10, "display_name": "Silva", "full_name": "Bernardo Silva"},
        {"player_id": 11, "display_name": "Silva", "full_name": "Silva Bernardo"},
    ]
    resolver = PlayerResolver.from_rows(rows)
    assert name_key("Bernardo Silva") == name_key("Silva Bernardo")   # premise

    match = resolver.resolve("Bernardo Silva")
    assert not match.matched
    assert {c["player_id"] for c in match.candidates} == {10, 11}


def test_two_similar_gabriels_are_not_confused(resolver: PlayerResolver) -> None:
    """The real trap: two Arsenal players whose names share a first token.

    Fuzzy matching without a margin requirement resolves both to whichever
    scores marginally higher.
    """
    magalhaes = resolver.resolve("Gabriel dos Santos Magalhaes")
    jesus = resolver.resolve("Gabriel Fernando de Jesus")
    assert magalhaes.player_id == 3
    assert jesus.player_id == 4


def test_bare_gabriel_is_ambiguous_and_declined(resolver: PlayerResolver) -> None:
    """'Gabriel' alone genuinely does not identify one of the two."""
    match = resolver.resolve("Gabriel")
    assert not match.matched


def test_empty_name_is_declined(resolver: PlayerResolver) -> None:
    assert not resolver.resolve("").matched
    assert not resolver.resolve("   ").matched


# --- rung 4: fuzzy, when it should fire -------------------------------------


def test_fuzzy_handles_a_minor_misspelling(resolver: PlayerResolver) -> None:
    match = resolver.resolve("Bukayo Sakka")
    assert match.player_id == 6
    assert match.method == "fuzzy"


def test_resolver_with_no_candidates_declines() -> None:
    assert not PlayerResolver([]).resolve("Anyone").matched


def test_candidate_construction_normalises_the_key() -> None:
    candidate = Candidate(1, "Son", "Heung-Min Son", name_key("Heung-Min Son"))
    assert candidate.key == "heung min son"
