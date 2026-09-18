"""Text normalisation helpers shared by the detectors.

Label strings, artist names and track titles arrive from Spotify in wildly
inconsistent shapes ("Adrian Recordings", "adrian recordings ab",
"Rama Lama Records/Sony Music Entertainment Sweden AB").  Everything that
compares two such strings goes through here so the rules stay consistent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List

# Characters that are letters in Swedish but fold to ASCII under NFKD.
_FOLD_MAP = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "AE",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ß": "ss",
        "ł": "l",
    }
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Corporate-form suffixes that carry no identity ("Adrian Recordings AB" is the
# same label as "Adrian Recordings").  Kept separately from the generic music
# words because their presence is itself a Swedish signal (see is_swedish_company_form).
_COMPANY_SUFFIXES = (
    "ab",
    "aktiebolag",
    "hb",
    "kb",
    "ek for",
    "ekonomisk forening",
    "publ",
    "inc",
    "llc",
    "ltd",
    "limited",
    "gmbh",
    "bv",
    "oy",
    "as",
    "aps",
    "sarl",
    "srl",
    "sa",
    "co",
    "corp",
    "corporation",
    "company",
    "group",
    "holding",
    "holdings",
)

# Generic words dropped when building the *loose* key used for fuzzy matching.
# "Adrian Recordings" and "Adrian Records" should collapse to the same key.
_GENERIC_WORDS = (
    "records",
    "record",
    "recordings",
    "recording",
    "recs",
    "music",
    "musik",
    "musique",
    "entertainment",
    "productions",
    "production",
    "produktion",
    "produktioner",
    "label",
    "skivbolag",
    "media",
    "sounds",
    "sound",
    "audio",
    "digital",
    "international",
    "worldwide",
    "the",
)

# Phrases Spotify uses to express "distributed by" / "licensed to" inside one
# label string.  Splitting on them keeps the imprint separate from the major.
_SPLIT_PHRASES = (
    " under exclusive license to ",
    " under exclusive licence to ",
    " under licens till ",
    " under license to ",
    " under licence to ",
    " licensed to ",
    " distributed by ",
    " distribuerad av ",
    " a division of ",
    " an imprint of ",
    " marketed by ",
    " exclusively licensed to ",
    " p/k/a ",
)

_SPLIT_CHARS_RE = re.compile(r"\s*(?:/|\||;|—|–| - |,\s*(?=a\s+division)|\bvia\b)\s*")


def strip_accents(value: str) -> str:
    """Fold diacritics to ASCII (å -> a, ö -> o, ø -> o)."""
    folded = value.translate(_FOLD_MAP)
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(value: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace."""
    if not value:
        return ""
    text = strip_accents(value).casefold()
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def tokens(value: str) -> List[str]:
    """Normalised word tokens."""
    normalized = normalize(value)
    return normalized.split() if normalized else []


def strip_company_suffix(value: str) -> str:
    """Drop trailing corporate forms from an already-normalised string."""
    words = value.split()
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def label_key(value: str) -> str:
    """Strict comparison key: normalised, without corporate suffixes.

    "Adrian Recordings AB" and "adrian recordings" both yield
    "adrian recordings"; "Adrian Records" does not (use :func:`loose_key`).
    """
    return strip_company_suffix(normalize(value))


def loose_key(value: str) -> str:
    """Fuzzy comparison key: also drops generic industry words.

    "Adrian Recordings", "Adrian Records" and "Adrian Music AB" all yield
    "adrian".  Returns "" when nothing distinctive survives, which callers must
    treat as "no match" rather than "matches everything".
    """
    words = [w for w in strip_company_suffix(normalize(value)).split() if w not in _GENERIC_WORDS]
    return " ".join(words)


def split_label_string(value: str) -> List[str]:
    """Split a compound label credit into its constituent imprints.

    >>> split_label_string("Rama Lama Records / Sony Music Entertainment")
    ['Rama Lama Records', 'Sony Music Entertainment']
    >>> split_label_string("1631 Recordings under exclusive license to Decca")
    ['1631 Recordings', 'Decca']
    """
    if not value:
        return []
    parts = [value]
    lowered_phrases = _SPLIT_PHRASES
    for phrase in lowered_phrases:
        expanded: List[str] = []
        for part in parts:
            idx = part.casefold().find(phrase)
            while idx != -1:
                expanded.append(part[:idx])
                part = part[idx + len(phrase) :]
                idx = part.casefold().find(phrase)
            expanded.append(part)
        parts = expanded

    expanded = []
    for part in parts:
        for piece in _SPLIT_CHARS_RE.split(part):
            expanded.append(piece)
    # Keep only fragments with at least two characters of substance so that
    # names such as "S/T" or stray separators do not produce empty imprints.
    out: List[str] = []
    for piece in expanded:
        cleaned = piece.strip(" \t-–—/|;,")
        if len(normalize(cleaned)) >= 2:
            out.append(cleaned)
    return out or ([value.strip()] if value.strip() else [])


def contains_any(haystack: str, needles: Iterable[str]) -> List[str]:
    """Return the needles that appear as substrings of the normalised haystack."""
    normalized = normalize(haystack)
    if not normalized:
        return []
    padded = f" {normalized} "
    hits = []
    for needle in needles:
        norm_needle = normalize(needle)
        if norm_needle and f" {norm_needle} " in padded:
            hits.append(needle)
    return hits


def has_swedish_letters(value: str) -> bool:
    """True when the raw string uses å/ä/ö (Swedish's extra vowels)."""
    return any(ch in value for ch in "åäöÅÄÖ")


def has_danish_norwegian_letters(value: str) -> bool:
    """True when the raw string uses æ/ø, which Swedish does not use."""
    return any(ch in value for ch in "æøÆØ")


def is_swedish_company_form(value: str) -> bool:
    """True when the string ends in a Swedish corporate form (AB, HB, KB)."""
    words = normalize(value).split()
    return bool(words) and words[-1] in {"ab", "hb", "kb", "aktiebolag"}
