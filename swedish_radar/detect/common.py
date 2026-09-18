"""Scoring primitives shared by the three criteria.

Every detector produces a list of :class:`~swedish_radar.models.Signal` objects
and combines them with :func:`combine`.  Keeping the combination rule in one
place is what makes the confidence numbers comparable across criteria, and what
makes the "why did this match?" panel in the UI honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ..models import Signal
from ..reference import Markers
from ..textutil import (
    has_danish_norwegian_letters,
    has_swedish_letters,
    normalize,
    tokens,
)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def combine(signals: Sequence[Signal]) -> float:
    """Combine independent signals with noisy-OR; negatives discount the result.

    Two weak positives reinforce each other (0.4 and 0.4 -> 0.64) without ever
    exceeding 1, and a counter-signal of -0.5 halves whatever the positives
    produced.  No single weak signal can push a verdict over the threshold on
    its own, which is the property we want.
    """
    remaining = 1.0
    for signal in signals:
        if signal.weight > 0:
            remaining *= 1.0 - clamp(signal.weight)
    score = 1.0 - remaining
    for signal in signals:
        if signal.weight < 0:
            score *= 1.0 - clamp(-signal.weight)
    return clamp(score)


@dataclass(frozen=True)
class LanguageHint:
    """Outcome of the Swedish-language heuristic over a blob of text."""

    swedish_words: Tuple[str, ...]
    competing_words: Tuple[str, ...]
    swedish_letters: bool
    competing_letters: bool

    @property
    def score(self) -> float:
        """A deliberately small number - language is a hint, never proof."""
        # Without a Swedish-distinctive word, any neighbouring-language evidence
        # wins outright: Finnish and Danish also use ä/ö and æ/ø respectively,
        # so bare diacritics say "Nordic", not "Swedish".
        if not self.swedish_words and (self.competing_letters or self.competing_words):
            return 0.0
        score = 0.0
        if len(self.swedish_words) >= 2:
            score = 0.45
        elif len(self.swedish_words) == 1:
            score = 0.3
        elif self.swedish_letters:
            score = 0.15
        if self.competing_words:
            score *= 0.4
        return round(clamp(score), 3)

    @property
    def detail(self) -> str:
        bits: List[str] = []
        if self.swedish_words:
            bits.append("Swedish words: " + ", ".join(self.swedish_words[:5]))
        if self.swedish_letters:
            bits.append("uses å/ä/ö")
        if self.competing_words:
            bits.append("but also Danish/Norwegian/Finnish words: " + ", ".join(self.competing_words[:3]))
        if self.competing_letters:
            bits.append("but uses æ/ø")
        return "; ".join(bits)


def analyse_language(texts: Iterable[str], markers: Markers) -> LanguageHint:
    """Look for Swedish-distinctive words, discounting Nordic near-neighbours."""
    joined_raw = " ".join(t for t in texts if t)
    word_set = set(tokens(joined_raw))
    swedish = tuple(sorted(word_set & set(markers.swedish_words)))
    competing = tuple(
        sorted(word_set & (set(markers.danish_norwegian_words) | set(markers.finnish_words)))
    )
    return LanguageHint(
        swedish_words=swedish,
        competing_words=competing,
        swedish_letters=has_swedish_letters(joined_raw),
        competing_letters=has_danish_norwegian_letters(joined_raw),
    )


@dataclass(frozen=True)
class GenreHint:
    """Outcome of matching Spotify genre tags against Sweden-specific markers."""

    explicit: Tuple[str, ...]
    specific: Tuple[str, ...]
    city: Tuple[str, ...]
    ambiguous: Tuple[str, ...]

    @property
    def score(self) -> float:
        if self.explicit:
            # "swedish pop", "svensk hiphop" - Spotify literally says Swedish.
            return 0.8
        if self.specific:
            # "dansband", "trallpunk", "gothenburg metal" - Sweden-only scenes.
            return 0.7
        if self.city:
            return 0.5
        if self.ambiguous:
            return 0.15
        return 0.0

    @property
    def detail(self) -> str:
        for label, values in (
            ("explicitly Swedish genre", self.explicit),
            ("Sweden-specific genre", self.specific),
            ("Swedish city genre", self.city),
            ("Nordic (not Sweden-specific) genre", self.ambiguous),
        ):
            if values:
                return f"{label}: {', '.join(values[:4])}"
        return ""


def analyse_genres(genres: Iterable[str], markers: Markers) -> GenreHint:
    explicit: List[str] = []
    specific: List[str] = []
    city: List[str] = []
    ambiguous: List[str] = []
    prefixes = tuple(normalize(p) for p in markers.genre_prefixes)
    specific_set = {normalize(g) for g in markers.sweden_specific_genres}
    ambiguous_set = {normalize(g) for g in markers.nordic_ambiguous_genres}
    cities = {normalize(c) for c in markers.swedish_cities}

    for genre in genres:
        norm = normalize(genre)
        if not norm:
            continue
        words = norm.split()
        if any(word.startswith(prefixes) for word in words if prefixes):
            explicit.append(genre)
        elif norm in specific_set:
            specific.append(genre)
        elif any(word in cities for word in words):
            city.append(genre)
        elif norm in ambiguous_set or any(
            norm.startswith(amb) or amb in norm for amb in ambiguous_set
        ):
            ambiguous.append(genre)
    return GenreHint(tuple(explicit), tuple(specific), tuple(city), tuple(ambiguous))


@dataclass(frozen=True)
class IsrcHint:
    """ISRC country prefixes on a release.

    The first two characters of an ISRC identify the *national agency that
    registered the recording*, not the artist's nationality - so ``SE`` is
    strong evidence of a Swedish label/production setup and only weak evidence
    about the artist.
    """

    total: int
    swedish: int
    nordic: int
    other: int

    @property
    def swedish_share(self) -> float:
        return self.swedish / self.total if self.total else 0.0

    @property
    def detail(self) -> str:
        if not self.total:
            return "no ISRCs available"
        return f"{self.swedish}/{self.total} ISRCs registered in Sweden (SE prefix)"


def analyse_isrcs(isrcs: Sequence[str], markers: Markers) -> IsrcHint:
    swedish = nordic = other = 0
    nordic_codes = set(markers.isrc_country_nordic)
    for isrc in isrcs:
        prefix = isrc[:2].upper()
        if prefix == markers.isrc_country_swedish:
            swedish += 1
        elif prefix in nordic_codes:
            nordic += 1
        else:
            other += 1
    return IsrcHint(total=len(isrcs), swedish=swedish, nordic=nordic, other=other)


def signal(code: str, detail: str, weight: float, source: str = "spotify") -> Optional[Signal]:
    """Build a signal, or ``None`` when the weight rounds to nothing."""
    if abs(weight) < 0.005:
        return None
    return Signal(code=code, detail=detail, weight=round(weight, 3), source=source)


def collect(*candidates: Optional[Signal]) -> List[Signal]:
    """Drop the ``None``s produced by :func:`signal`."""
    return [c for c in candidates if c is not None]
