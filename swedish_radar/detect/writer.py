"""Criterion 3 - written primarily by Swedish songwriters.

Spotify publishes no songwriter credits at all, so this criterion is answered
entirely through MusicBrainz: ISRC -> recording -> work -> composer/lyricist
relations -> the writers' countries.  Coverage is therefore partial, and the
verdict says so rather than guessing: a release is only ever *matched* here when
at least one song's writing credits were actually resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import CRITERION_WRITER
from ..models import Release, Signal, Verdict, Writer
from ..reference import Reference
from .common import analyse_isrcs, combine


@dataclass
class TrackWriters:
    """Writer credits resolved for one track."""

    track_id: str
    track_name: str
    isrc: Optional[str]
    writers: List[Writer] = field(default_factory=list)
    work_mbids: List[str] = field(default_factory=list)

    @property
    def known(self) -> List[Writer]:
        """Writers whose nationality MusicBrainz actually knows."""
        return [w for w in self.writers if w.country]

    @property
    def swedish(self) -> List[Writer]:
        return [w for w in self.writers if w.is_swedish]

    @property
    def swedish_share(self) -> float:
        known = self.known
        return len(self.swedish) / len(known) if known else 0.0

    @property
    def resolved(self) -> bool:
        return bool(self.known)

    def qualifies(self, majority: float) -> bool:
        return self.resolved and self.swedish_share >= majority

    def to_dict(self, majority: float) -> Dict[str, Any]:
        return {
            "track_id": self.track_id,
            "track_name": self.track_name,
            "isrc": self.isrc,
            "work_mbids": self.work_mbids,
            "writers": [w.to_dict() for w in self.writers],
            "writers_total": len(self.writers),
            "writers_known_country": len(self.known),
            "writers_swedish": len(self.swedish),
            "swedish_share": round(self.swedish_share, 3),
            "qualifies": self.qualifies(majority),
        }


@dataclass
class WriterContext:
    """Writer credits for the tracks that were looked up.

    ``attempted`` is how many tracks we tried, which may be fewer than the
    tracklist when a per-release lookup budget is in force.
    """

    tracks: List[TrackWriters] = field(default_factory=list)
    attempted: int = 0
    lookup_failed: bool = False


def detect_swedish_writers(
    release: Release,
    context: WriterContext,
    reference: Reference,
    *,
    threshold: float = 0.55,
    majority: float = 0.5,
) -> Verdict:
    """Decide whether ``release`` was written primarily by Swedish songwriters."""
    resolved = [t for t in context.tracks if t.resolved]
    qualifying = [t for t in resolved if t.qualifies(majority)]
    attempted = max(context.attempted, len(context.tracks))

    data: Dict[str, Any] = {
        "majority_threshold": majority,
        "tracks_attempted": attempted,
        "tracks_resolved": len(resolved),
        "tracks_qualifying": len(qualifying),
        "qualifying_track_names": [t.track_name for t in qualifying],
        "coverage": round(len(resolved) / attempted, 3) if attempted else 0.0,
        "tracks": [t.to_dict(majority) for t in context.tracks],
        "swedish_writers": sorted(
            {w.name for t in resolved for w in t.swedish}
        ),
    }

    if not resolved:
        summary = (
            "MusicBrainz has no songwriter credits for this release yet."
            if not context.lookup_failed
            else "Songwriter lookup failed - treat this criterion as unknown."
        )
        return Verdict(
            criterion=CRITERION_WRITER,
            matched=False,
            confidence=0.0,
            signals=[],
            summary=summary,
            data=data,
        )

    signals: List[Signal] = []
    track_share = len(qualifying) / len(resolved)
    # How completely we know the writers of the songs that did qualify - a work
    # with 1 of 5 writers identified is weaker evidence than 4 of 4.
    if qualifying:
        completeness = sum(
            len(t.known) / len(t.writers) for t in qualifying if t.writers
        ) / len(qualifying)
    else:
        completeness = 0.0

    if track_share > 0:
        names = sorted({w.name for t in qualifying for w in t.swedish})
        detail = (
            f"{len(qualifying)} of {len(resolved)} songs with credits are written "
            f"mostly by Swedish writers ({', '.join(names[:4])}"
            + (", …" if len(names) > 4 else "")
            + ")"
        )
        signals.append(
            Signal(
                "mb_writers_swedish",
                detail,
                round(0.95 * track_share * (0.7 + 0.3 * completeness), 3),
                source="musicbrainz",
            )
        )

    non_swedish = [
        w.name
        for t in resolved
        for w in t.known
        if not w.is_swedish
    ]
    if non_swedish and track_share < 1.0:
        signals.append(
            Signal(
                "non_swedish_writers",
                f"Non-Swedish writers credited: {', '.join(sorted(set(non_swedish))[:4])}",
                -0.25 * (1.0 - track_share),
                source="musicbrainz",
            )
        )

    coverage = len(resolved) / attempted if attempted else 0.0
    if coverage < 0.5:
        signals.append(
            Signal(
                "low_coverage",
                f"Credits found for only {len(resolved)} of {attempted} songs checked",
                round(-0.3 * (1.0 - coverage), 3),
                source="musicbrainz",
            )
        )

    isrc_hint = analyse_isrcs(release.isrcs, reference.markers)
    if isrc_hint.total and isrc_hint.swedish_share >= 0.5:
        signals.append(
            Signal(
                "isrc_se",
                isrc_hint.detail + " - consistent with a Swedish writing/production setup",
                0.2,
                source="spotify",
            )
        )

    confidence = combine(signals)
    matched = bool(qualifying) and track_share >= majority and confidence >= threshold

    if matched:
        names = data["swedish_writers"]
        shown = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        summary = (
            f"Written primarily by Swedish songwriters ({shown}) - "
            f"{len(qualifying)} of {len(resolved)} credited songs."
        )
    elif qualifying:
        summary = (
            f"Only {len(qualifying)} of {len(resolved)} credited songs are primarily "
            "Swedish-written."
        )
    else:
        summary = "Credited writers are not primarily Swedish."

    return Verdict(
        criterion=CRITERION_WRITER,
        matched=matched,
        confidence=round(confidence, 3),
        signals=signals,
        summary=summary,
        data=data,
    )


def writer_from_credit(name: str, mbid: str, roles: Sequence[str], country: Optional[str]) -> Writer:
    return Writer(
        name=name,
        mbid=mbid,
        roles=list(roles),
        country=country,
        is_swedish=(country == "SE") if country else None,
    )
