"""Name normalisation.

The Premier League is a hard case for string matching in a small, specific way:
the names are multilingual, sources disagree about ordering, and the diacritics
survive in some feeds and not others. Concretely, the same footballer appears as

    Son Heung-min / Heung-Min Son / Son / Son Heung-Min
    Gabriel dos Santos Magalhaes / Gabriel Magalhães / Gabriel
    Rodrigo Hernandez / Rodri
    Bruno Borges Fernandes / Bruno Fernandes / B.Fernandes

Normalisation gets us the mechanical differences -- case, accents, punctuation,
whitespace. It does not and cannot get us the human ones (Rodri is not a
substring of Rodrigo Hernandez in any useful sense). Those go to the override
file, which is not an admission of defeat: every real data platform has one, and
pretending otherwise produces silently wrong joins instead of visible gaps.
"""

from __future__ import annotations

import re
import unicodedata

# Particles that appear in some renderings of a name and not others.
_PARTICLES = {"de", "da", "dos", "das", "del", "della", "di", "van", "von",
              "der", "den", "la", "le", "el", "al", "bin", "ibn"}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


# Letters that Unicode decomposition does NOT touch, because they are distinct
# letters rather than a base letter plus a combining mark. NFKD turns "é" into
# "e" + U+0301 and we drop the mark; it leaves "ø" and "đ" exactly as they are.
#
# This is not pedantry -- Højbjerg played in the Premier League, and a source
# that writes "Hojbjerg" would never have matched him.
_LETTER_FOLDS = str.maketrans({
    "ø": "o", "Ø": "O",
    "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D",
    "ł": "l", "Ł": "L",
    "ħ": "h", "Ħ": "H",
    "ı": "i", "İ": "I",
    "þ": "th", "Þ": "Th",
    "æ": "ae", "Æ": "Ae",
    "œ": "oe", "Œ": "Oe",
    "ß": "ss",
})


def strip_accents(text: str) -> str:
    """Magalhães -> Magalhaes, Højbjerg -> Hojbjerg, Đorđević -> Dordevic.

    Two passes, because one is not enough: NFKD decomposition handles anything
    written as a base letter plus a combining mark, and the explicit table above
    handles the letters that have no such decomposition.
    """
    folded = text.translate(_LETTER_FOLDS)
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise(name: str) -> str:
    """Casefold, strip accents, drop punctuation, collapse whitespace.

    Hyphens become spaces rather than disappearing, so 'Heung-min' and
    'Heung min' agree while 'Heungmin' is left as the distinct string it is.
    """
    if not name:
        return ""
    text = strip_accents(name).replace("-", " ").replace("'", " ")
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip().casefold()


def tokens(name: str) -> list[str]:
    return normalise(name).split()


def significant_tokens(name: str) -> list[str]:
    """Tokens with particles and single letters removed.

    'Gabriel dos Santos Magalhaes' -> ['gabriel', 'santos', 'magalhaes']
    'B.Fernandes'                  -> ['fernandes']
    """
    return [t for t in tokens(name) if t not in _PARTICLES and len(t) > 1]


def name_key(name: str) -> str:
    """Order-independent key, so 'Son Heung-min' and 'Heung-Min Son' agree.

    Sorting the significant tokens is crude but it is exactly the failure mode
    the sources actually exhibit, and it is cheap enough to apply to every row.
    """
    return " ".join(sorted(significant_tokens(name)))


def archive_gw_name(name: str) -> str:
    """Undo the archive's merged_gw name encoding.

    Older seasons write 'Heung_Min_Son_10'; newer ones write 'Heung-Min Son'.
    Both become a plain spaced name here.
    """
    text = name.strip()
    text = re.sub(r"_\d+$", "", text)      # trailing element id
    return text.replace("_", " ").strip()
