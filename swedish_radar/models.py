"""Domain objects.

Spotify's JSON is kept at the edge: everything downstream of the clients works
with these dataclasses, so a change in Spotify's payload shape touches one file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_list(value: object) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_dict(value: object) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def parse_release_date(raw: str, precision: str) -> Optional[date]:
    """Spotify dates come as ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``.

    Partial dates are widened to the *first* day of the period; a release dated
    "2026" should not be treated as newer than it is.
    """
    if not raw:
        return None
    try:
        if precision == "year" or len(raw) == 4:
            return date(int(raw[:4]), 1, 1)
        if precision == "month" or len(raw) == 7:
            return date(int(raw[:4]), int(raw[5:7]), 1)
        return date(int(raw[:4]), int(raw[5:7]), int(raw[8:10]))
    except (ValueError, IndexError):
        return None


@dataclass
class Artist:
    """A Spotify artist, optionally enriched with MusicBrainz facts."""

    id: str
    name: str
    genres: List[str] = field(default_factory=list)
    popularity: Optional[int] = None
    followers: Optional[int] = None
    image_url: Optional[str] = None
    # --- MusicBrainz enrichment -----------------------------------------
    mbid: Optional[str] = None
    country: Optional[str] = None
    area: Optional[str] = None
    begin_area: Optional[str] = None
    mb_resolved_by: Optional[str] = None  # "spotify-url" | "name-search" | None

    @classmethod
    def from_spotify(cls, payload: Mapping[str, Any]) -> "Artist":
        images = _as_list(payload.get("images"))
        return cls(
            id=_as_str(payload.get("id")),
            name=_as_str(payload.get("name")),
            genres=[g for g in _as_list(payload.get("genres")) if isinstance(g, str)],
            popularity=payload.get("popularity") if isinstance(payload.get("popularity"), int) else None,
            followers=_as_dict(payload.get("followers")).get("total"),
            image_url=_as_str(_as_dict(images[-1]).get("url")) or None if images else None,
        )

    @property
    def spotify_url(self) -> str:
        return f"https://open.spotify.com/artist/{self.id}"


@dataclass
class Track:
    """A track on a release.  ``isrc`` is the join key into MusicBrainz."""

    id: str
    name: str
    track_number: int = 0
    duration_ms: int = 0
    isrc: Optional[str] = None
    artist_ids: List[str] = field(default_factory=list)
    artist_names: List[str] = field(default_factory=list)
    explicit: bool = False

    @classmethod
    def from_spotify(cls, payload: Mapping[str, Any]) -> "Track":
        artists = [_as_dict(a) for a in _as_list(payload.get("artists"))]
        isrc = _as_str(_as_dict(payload.get("external_ids")).get("isrc")) or None
        return cls(
            id=_as_str(payload.get("id")),
            name=_as_str(payload.get("name")),
            track_number=payload.get("track_number") or 0,
            duration_ms=payload.get("duration_ms") or 0,
            isrc=isrc.upper() if isrc else None,
            artist_ids=[_as_str(a.get("id")) for a in artists if a.get("id")],
            artist_names=[_as_str(a.get("name")) for a in artists if a.get("name")],
            explicit=bool(payload.get("explicit")),
        )

    @property
    def spotify_url(self) -> str:
        return f"https://open.spotify.com/track/{self.id}"


@dataclass
class Release:
    """A Spotify album/single/EP plus everything we learned about it."""

    id: str
    name: str
    album_type: str = "album"
    release_date: Optional[date] = None
    release_date_raw: str = ""
    total_tracks: int = 0
    label: str = ""
    copyrights: List[str] = field(default_factory=list)
    upc: Optional[str] = None
    image_url: Optional[str] = None
    available_markets: List[str] = field(default_factory=list)
    artist_ids: List[str] = field(default_factory=list)
    artist_names: List[str] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    #: Which discovery source(s) surfaced this release.
    sources: List[str] = field(default_factory=list)

    @classmethod
    def from_spotify(cls, payload: Mapping[str, Any]) -> "Release":
        artists = [_as_dict(a) for a in _as_list(payload.get("artists"))]
        images = [_as_dict(i) for i in _as_list(payload.get("images"))]
        raw_date = _as_str(payload.get("release_date"))
        precision = _as_str(payload.get("release_date_precision"), "day")
        copyrights = [
            _as_str(_as_dict(c).get("text"))
            for c in _as_list(payload.get("copyrights"))
            if _as_str(_as_dict(c).get("text"))
        ]
        tracks_payload = _as_dict(payload.get("tracks"))
        tracks = [Track.from_spotify(_as_dict(t)) for t in _as_list(tracks_payload.get("items"))]
        upc = _as_str(_as_dict(payload.get("external_ids")).get("upc")) or None
        return cls(
            id=_as_str(payload.get("id")),
            name=_as_str(payload.get("name")),
            album_type=_as_str(payload.get("album_type"), "album"),
            release_date=parse_release_date(raw_date, precision),
            release_date_raw=raw_date,
            total_tracks=payload.get("total_tracks") or 0,
            label=_as_str(payload.get("label")),
            copyrights=copyrights,
            upc=upc,
            image_url=_as_str(images[0].get("url")) or None if images else None,
            available_markets=[
                m for m in _as_list(payload.get("available_markets")) if isinstance(m, str)
            ],
            artist_ids=[_as_str(a.get("id")) for a in artists if a.get("id")],
            artist_names=[_as_str(a.get("name")) for a in artists if a.get("name")],
            tracks=[t for t in tracks if t.id],
        )

    @property
    def spotify_url(self) -> str:
        return f"https://open.spotify.com/album/{self.id}"

    @property
    def isrcs(self) -> List[str]:
        return [t.isrc for t in self.tracks if t.isrc]

    def age_days(self, today: Optional[date] = None) -> Optional[int]:
        if self.release_date is None:
            return None
        reference = today or datetime.now(timezone.utc).date()
        return (reference - self.release_date).days


@dataclass
class Writer:
    """A songwriter credit resolved through MusicBrainz."""

    name: str
    mbid: Optional[str] = None
    roles: List[str] = field(default_factory=list)
    country: Optional[str] = None
    is_swedish: Optional[bool] = None  # None = country unknown

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "mbid": self.mbid,
            "roles": sorted(set(self.roles)),
            "country": self.country,
            "is_swedish": self.is_swedish,
        }


@dataclass
class Signal:
    """One piece of evidence for (or against) a criterion.

    ``weight`` is the probability this signal alone assigns to the claim.
    A negative weight means the signal argues against it.
    """

    code: str
    detail: str
    weight: float
    source: str = "spotify"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "weight": round(self.weight, 3),
            "source": self.source,
        }


@dataclass
class Verdict:
    """The outcome of evaluating one criterion against one release."""

    criterion: str
    matched: bool
    confidence: float
    signals: List[Signal] = field(default_factory=list)
    summary: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion": self.criterion,
            "matched": self.matched,
            "confidence": round(self.confidence, 3),
            "summary": self.summary,
            "signals": [s.to_dict() for s in self.signals],
            "data": self.data,
        }


@dataclass
class Match:
    """A release that satisfied at least one requested criterion."""

    release: Release
    artists: List[Artist]
    verdicts: Dict[str, Verdict]
    score: float = 0.0

    @property
    def matched_criteria(self) -> List[str]:
        return [name for name, verdict in self.verdicts.items() if verdict.matched]

    def to_dict(self, today: Optional[date] = None) -> Dict[str, Any]:
        release = self.release
        return {
            "id": release.id,
            "name": release.name,
            "album_type": release.album_type,
            "release_date": release.release_date.isoformat() if release.release_date else None,
            "release_date_raw": release.release_date_raw,
            "age_days": release.age_days(today),
            "total_tracks": release.total_tracks,
            "label": release.label,
            "copyrights": release.copyrights,
            "upc": release.upc,
            "image_url": release.image_url,
            "spotify_url": release.spotify_url,
            "sources": sorted(set(release.sources)),
            "artists": [
                {
                    "id": a.id,
                    "name": a.name,
                    "genres": a.genres,
                    "country": a.country,
                    "area": a.area,
                    "mbid": a.mbid,
                    "spotify_url": a.spotify_url,
                    "image_url": a.image_url,
                }
                for a in self.artists
            ],
            "tracks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "isrc": t.isrc,
                    "track_number": t.track_number,
                    "spotify_url": t.spotify_url,
                }
                for t in release.tracks
            ],
            "matched_criteria": self.matched_criteria,
            "score": round(self.score, 3),
            "verdicts": {name: v.to_dict() for name, v in self.verdicts.items()},
        }


@dataclass
class RunStats:
    """Counters surfaced at the end of a run so the numbers are auditable."""

    sources_queried: int = 0
    albums_seen: int = 0
    albums_in_window: int = 0
    albums_enriched: int = 0
    matches: int = 0
    spotify_requests: int = 0
    musicbrainz_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    errors: List[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sources_queried": self.sources_queried,
            "albums_seen": self.albums_seen,
            "albums_in_window": self.albums_in_window,
            "albums_enriched": self.albums_enriched,
            "matches": self.matches,
            "spotify_requests": self.spotify_requests,
            "musicbrainz_requests": self.musicbrainz_requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class RunResult:
    """Everything a discovery run produced."""

    matches: List[Match]
    stats: RunStats
    criteria: Sequence[str]
    settings_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, today: Optional[date] = None) -> Dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "criteria": list(self.criteria),
            "settings": self.settings_summary,
            "stats": self.stats.to_dict(),
            "count": len(self.matches),
            "matches": [m.to_dict(today) for m in self.matches],
        }


# ----------------------------------------------------------------------
# Deserialisation
#
# ``to_dict`` output is the app's interchange format: it is what the store
# holds, what the HTTP API returns and what ``--format json`` writes.  Being
# able to read it back makes a saved run re-renderable in any other format
# without touching the network again.
# ----------------------------------------------------------------------
def _date_from_iso(value: object) -> Optional[date]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def signal_from_dict(payload: Mapping[str, Any]) -> Signal:
    return Signal(
        code=_as_str(payload.get("code")),
        detail=_as_str(payload.get("detail")),
        weight=float(payload.get("weight") or 0.0),
        source=_as_str(payload.get("source"), "unknown"),
    )


def verdict_from_dict(payload: Mapping[str, Any]) -> Verdict:
    return Verdict(
        criterion=_as_str(payload.get("criterion")),
        matched=bool(payload.get("matched")),
        confidence=float(payload.get("confidence") or 0.0),
        signals=[signal_from_dict(_as_dict(s)) for s in _as_list(payload.get("signals"))],
        summary=_as_str(payload.get("summary")),
        data=_as_dict(payload.get("data")),
    )


def match_from_dict(payload: Mapping[str, Any]) -> Match:
    release = Release(
        id=_as_str(payload.get("id")),
        name=_as_str(payload.get("name")),
        album_type=_as_str(payload.get("album_type"), "album"),
        release_date=_date_from_iso(payload.get("release_date")),
        release_date_raw=_as_str(payload.get("release_date_raw")),
        total_tracks=int(payload.get("total_tracks") or 0),
        label=_as_str(payload.get("label")),
        copyrights=[c for c in _as_list(payload.get("copyrights")) if isinstance(c, str)],
        upc=_as_str(payload.get("upc")) or None,
        image_url=_as_str(payload.get("image_url")) or None,
        sources=[s for s in _as_list(payload.get("sources")) if isinstance(s, str)],
    )
    artists: List[Artist] = []
    for raw in _as_list(payload.get("artists")):
        item = _as_dict(raw)
        artist = Artist(
            id=_as_str(item.get("id")),
            name=_as_str(item.get("name")),
            genres=[g for g in _as_list(item.get("genres")) if isinstance(g, str)],
            image_url=_as_str(item.get("image_url")) or None,
            mbid=_as_str(item.get("mbid")) or None,
            country=_as_str(item.get("country")) or None,
            area=_as_str(item.get("area")) or None,
        )
        artists.append(artist)
        release.artist_ids.append(artist.id)
        release.artist_names.append(artist.name)
    for raw in _as_list(payload.get("tracks")):
        item = _as_dict(raw)
        release.tracks.append(
            Track(
                id=_as_str(item.get("track_id") or item.get("id")),
                name=_as_str(item.get("name")),
                track_number=int(item.get("track_number") or 0),
                isrc=_as_str(item.get("isrc")) or None,
            )
        )
    verdicts = {
        str(name): verdict_from_dict(_as_dict(raw))
        for name, raw in _as_dict(payload.get("verdicts")).items()
    }
    return Match(
        release=release,
        artists=artists,
        verdicts=verdicts,
        score=float(payload.get("score") or 0.0),
    )


def run_result_from_dict(payload: Mapping[str, Any]) -> RunResult:
    """Rebuild a :class:`RunResult` from ``RunResult.to_dict`` output."""
    stats_payload = _as_dict(payload.get("stats"))
    stats = RunStats(
        sources_queried=int(stats_payload.get("sources_queried") or 0),
        albums_seen=int(stats_payload.get("albums_seen") or 0),
        albums_in_window=int(stats_payload.get("albums_in_window") or 0),
        albums_enriched=int(stats_payload.get("albums_enriched") or 0),
        matches=int(stats_payload.get("matches") or 0),
        spotify_requests=int(stats_payload.get("spotify_requests") or 0),
        musicbrainz_requests=int(stats_payload.get("musicbrainz_requests") or 0),
        cache_hits=int(stats_payload.get("cache_hits") or 0),
        cache_misses=int(stats_payload.get("cache_misses") or 0),
        errors=[e for e in _as_list(stats_payload.get("errors")) if isinstance(e, str)],
        started_at=_as_str(stats_payload.get("started_at")),
        finished_at=_as_str(stats_payload.get("finished_at")),
    )
    return RunResult(
        matches=[match_from_dict(_as_dict(m)) for m in _as_list(payload.get("matches"))],
        stats=stats,
        criteria=[c for c in _as_list(payload.get("criteria")) if isinstance(c, str)],
        settings_summary=_as_dict(payload.get("settings")),
    )
