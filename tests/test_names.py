"""Name normalisation.

The cases here are all real: every one is a name that actually appears in the
Premier League and actually differs between sources.
"""

from __future__ import annotations

import pytest

from fplq.resolve.names import (
    archive_gw_name,
    name_key,
    normalise,
    significant_tokens,
    strip_accents,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Magalhães", "Magalhaes"),
        ("Sané", "Sane"),
        ("Guéhi", "Guehi"),
        ("Đorđević", "Dordevic"),
        ("Kováčik", "Kovacik"),
        # Letters Unicode decomposition does not touch: these are distinct
        # letters, not accented ones. Højbjerg is the case that matters.
        ("Højbjerg", "Hojbjerg"),
        ("Łukasz", "Lukasz"),
        ("plain", "plain"),
    ],
)
def test_strip_accents(raw: str, expected: str) -> None:
    assert strip_accents(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Heung-Min Son", "heung min son"),
        ("Heung-min  Son", "heung min son"),
        ("O'Brien", "o brien"),
        ("N'Golo Kanté", "n golo kante"),
        ("", ""),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


def test_hyphen_becomes_space_not_nothing() -> None:
    """'Heung-min' and 'Heung min' must agree; 'Heungmin' must not.

    Dropping hyphens entirely would collapse all three, which looks like
    robustness and is actually a way to merge two different people.
    """
    assert normalise("Heung-min") == normalise("Heung min")
    assert normalise("Heung-min") != normalise("Heungmin")


def test_significant_tokens_drops_particles() -> None:
    assert significant_tokens("Gabriel dos Santos Magalhaes") == [
        "gabriel", "santos", "magalhaes",
    ]
    assert significant_tokens("Virgil van Dijk") == ["virgil", "dijk"]


def test_significant_tokens_drops_initials() -> None:
    """'B.Fernandes' must reduce to the surname, not to a one-letter token."""
    assert significant_tokens("B.Fernandes") == ["fernandes"]


def test_name_key_is_order_independent() -> None:
    """The single most common cross-source difference: given/family name order."""
    assert name_key("Son Heung-min") == name_key("Heung-Min Son")
    assert name_key("Mohamed Salah") == name_key("Salah Mohamed")


def test_name_key_distinguishes_different_people() -> None:
    assert name_key("Gabriel Fernando de Jesus") != name_key("Gabriel dos Santos Magalhaes")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Heung_Min_Son_10", "Heung Min Son"),   # older archive seasons
        ("Heung-Min Son", "Heung-Min Son"),      # newer ones
        ("Mohamed_Salah_233", "Mohamed Salah"),
    ],
)
def test_archive_gw_name(raw: str, expected: str) -> None:
    assert archive_gw_name(raw) == expected
