"""Criterion 1 - released by a Swedish artist."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import CRITERION_ARTIST
from ..models import Artist, Release, Signal, Verdict
from ..reference import Reference
from .common import analyse_genres, analyse_isrcs, analyse_language, combine, signal

NORDIC_NEIGHBOURS = ("NO", "DK", "FI", "IS")


@dataclass
class ArtistScore:
    """Per-artist evaluation, kept so the UI can say *which* artist is Swedish."""

    artist: Artist
    signals: List[Signal] = field(default_factory=list)
    confidence: float = 0.0
    swedish: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.artist.id,
            "name": self.artist.name,
            "confidence": round(self.confidence, 3),
            "swedish": self.swedish,
            "country": self.artist.country,
            "mbid": self.artist.mbid,
            "signals": [s.to_dict() for s in self.signals],
        }


def _market_signal(release: Release) -> Optional[Signal]:
    """A release available only in Sweden is usually a Swedish release.

    Note Spotify omits ``available_markets`` when a request is market-scoped,
    so this signal is simply absent most of the time.
    """
    markets = release.available_markets
    if not markets:
        return None
    if markets == ["SE"]:
        return signal("markets_se_only", "Released in Sweden only", 0.35, source="spotify")
    if "SE" in markets and len(markets) <= 5 and all(m in ("SE",) + NORDIC_NEIGHBOURS for m in markets):
        return signal(
            "markets_nordic",
            f"Released only in the Nordics ({', '.join(markets)})",
            0.2,
            source="spotify",
        )
    return None


def _artist_signals(artist: Artist, reference: Reference) -> List[Signal]:
    signals: List[Signal] = []

    if artist.country:
        if artist.country == "SE":
            weight = 0.95 if artist.mb_resolved_by == "spotify-url" else 0.75
            where = f" ({artist.area})" if artist.area else ""
            how = (
                "matched via the Spotify link on the MusicBrainz artist"
                if artist.mb_resolved_by == "spotify-url"
                else "matched by name search"
            )
            signals.append(
                Signal(
                    "mb_country_se",
                    f"MusicBrainz places {artist.name} in Sweden{where} - {how}",
                    weight,
                    source="musicbrainz",
                )
            )
        else:
            weight = -0.9 if artist.mb_resolved_by == "spotify-url" else -0.6
            signals.append(
                Signal(
                    "mb_country_other",
                    f"MusicBrainz places {artist.name} in {artist.country}, not Sweden",
                    weight,
                    source="musicbrainz",
                )
            )

    genre_hint = analyse_genres(artist.genres, reference.markers)
    if genre_hint.score:
        signals.append(
            Signal("genre", f"{artist.name}: {genre_hint.detail}", genre_hint.score, source="spotify")
        )

    name_hint = analyse_language([artist.name], reference.markers)
    if name_hint.score:
        signals.append(
            Signal(
                "artist_name_language",
                f"Artist name looks Swedish ({name_hint.detail})",
                min(name_hint.score, 0.3),
                source="heuristic",
            )
        )
    return signals


def detect_swedish_artist(
    release: Release,
    artists: Sequence[Artist],
    reference: Reference,
    *,
    threshold: float = 0.55,
    label_is_swedish_indie: bool = False,
) -> Verdict:
    """Decide whether ``release`` is by a Swedish artist.

    A collaboration counts when *any* credited artist is Swedish; the per-artist
    breakdown in ``data['artists']`` records which one.
    """
    # Release-level evidence applies to whichever artist we end up scoring.
    shared: List[Signal] = []

    isrc_hint = analyse_isrcs(release.isrcs, reference.markers)
    if isrc_hint.total and isrc_hint.swedish_share >= 0.5:
        shared.append(Signal("isrc_se", isrc_hint.detail, 0.4, source="spotify"))

    if label_is_swedish_indie:
        shared.append(
            Signal(
                "swedish_label",
                f"Released on the Swedish independent label '{release.label}'",
                0.45,
                source="derived",
            )
        )

    market_signal = _market_signal(release)
    if market_signal is not None:
        shared.append(market_signal)

    title_hint = analyse_language(
        [release.name, *(t.name for t in release.tracks)], reference.markers
    )
    if title_hint.score:
        shared.append(
            Signal(
                "title_language",
                f"Titles are in Swedish ({title_hint.detail})",
                title_hint.score,
                source="heuristic",
            )
        )

    scores: List[ArtistScore] = []
    for artist in artists:
        artist_signals = _artist_signals(artist, reference)
        confidence = combine(artist_signals + shared)
        scores.append(
            ArtistScore(
                artist=artist,
                signals=artist_signals,
                confidence=confidence,
                swedish=confidence >= threshold,
            )
        )

    if not scores:
        return Verdict(
            criterion=CRITERION_ARTIST,
            matched=False,
            confidence=0.0,
            signals=shared,
            summary="No artist information on the release.",
            data={"artists": []},
        )

    best = max(scores, key=lambda s: s.confidence)
    all_signals = best.signals + shared
    confidence = best.confidence
    matched = confidence >= threshold

    swedish_names = [s.artist.name for s in scores if s.swedish]
    verified = [s.artist.name for s in scores if s.artist.country == "SE"]

    if matched:
        if verified:
            summary = f"{', '.join(verified)} " + (
                "is" if len(verified) == 1 else "are"
            ) + " Swedish (confirmed in MusicBrainz)."
        else:
            summary = f"{', '.join(swedish_names) or best.artist.name} looks Swedish on Spotify's own signals."
    elif best.artist.country and best.artist.country != "SE":
        summary = f"{best.artist.name} is from {best.artist.country}, not Sweden."
    else:
        summary = f"No evidence that {best.artist.name} is Swedish."

    return Verdict(
        criterion=CRITERION_ARTIST,
        matched=matched,
        confidence=round(confidence, 3),
        signals=all_signals,
        summary=summary,
        data={
            "artists": [s.to_dict() for s in scores],
            "primary_artist": best.artist.name,
            "verified_swedish": verified,
        },
    )
