"""MusicBrainz client.

Spotify knows nothing about nationality or songwriting credits, so the two
hardest criteria - "Swedish artist" and "written primarily by Swedish
songwriters" - are answered here.

MusicBrainz asks anonymous clients for at most one request per second and for a
User-Agent that identifies the application and a contact address.  Both are
enforced by this class; results are cached for a month because the underlying
facts (where an artist is from, who wrote a work) essentially never change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..cache import Cache
from ..config import Settings
from ..errors import HttpError, RadarError
from ..http import JsonClient, RateLimiter, Transport, UrllibTransport, build_url

#: Work->artist relation types that count as "wrote the song".
#: ``arranger``/``orchestrator``/``translator`` deliberately do not.
WRITER_ROLES = frozenset({"composer", "lyricist", "writer", "librettist"})

SWEDEN = "SE"


@dataclass
class MBArtist:
    mbid: str
    name: str
    country: Optional[str] = None
    area: Optional[str] = None
    begin_area: Optional[str] = None
    disambiguation: str = ""
    type: str = ""

    @property
    def is_swedish(self) -> bool:
        return self.country == SWEDEN


@dataclass
class MBWriterCredit:
    """One writer of one work."""

    mbid: str
    name: str
    roles: Set[str] = field(default_factory=set)


@dataclass
class MBWork:
    mbid: str
    title: str
    writers: List[MBWriterCredit] = field(default_factory=list)


@dataclass
class MBLabel:
    mbid: str
    name: str
    country: Optional[str] = None
    area: Optional[str] = None
    type: str = ""

    @property
    def is_swedish(self) -> bool:
        return self.country == SWEDEN


def _area_country(payload: Mapping[str, Any]) -> Optional[str]:
    """Pull an ISO country code out of an ``area``/``begin-area`` object."""
    if not isinstance(payload, Mapping):
        return None
    codes = payload.get("iso-3166-1-codes")
    if isinstance(codes, list) and codes:
        first = codes[0]
        if isinstance(first, str):
            return first.upper()
    return None


def _area_name(payload: Any) -> Optional[str]:
    if isinstance(payload, Mapping):
        name = payload.get("name")
        if isinstance(name, str):
            return name
    return None


class MusicBrainzClient:
    """Rate-limited, cached read-only MusicBrainz client."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[Transport] = None,
        cache: Optional[Cache] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.enabled = settings.musicbrainz_enabled
        user_agent = f"{settings.user_agent} {settings.musicbrainz_contact}".strip()
        self._client = JsonClient(
            transport or UrllibTransport(),
            user_agent=user_agent,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            limiter=RateLimiter(settings.musicbrainz_rate_per_sec, sleep=sleep),
            sleep=sleep,
        )
        self.request_count = 0
        #: Errors are collected rather than raised: MusicBrainz being flaky must
        #: degrade the confidence of a run, not abort it.
        self.errors: List[str] = []

    # ------------------------------------------------------------------
    def _get(self, path: str, params: Mapping[str, object]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        query = dict(params)
        query.setdefault("fmt", "json")
        url = build_url(self.settings.musicbrainz_base, path, query)
        cache_key = f"mb:{url}"
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached if isinstance(cached, dict) else None
        self.request_count += 1
        try:
            payload = self._client.get_json(url, allow_404=True)
        except (HttpError, RadarError) as exc:
            self.errors.append(f"musicbrainz: {exc}")
            return None
        if self.cache is not None:
            # Negative results are cached too (as ``{}``) so a missing ISRC is
            # not re-requested on every run.
            self.cache.set(cache_key, payload or {}, self.settings.cache_ttl_musicbrainz)
        return payload

    # ------------------------------------------------------------------
    # Artists
    # ------------------------------------------------------------------
    def artist_mbid_for_spotify(self, spotify_artist_id: str) -> Optional[str]:
        """Exact mapping via the Spotify URL stored on the MusicBrainz artist."""
        if not spotify_artist_id:
            return None
        resource = f"https://open.spotify.com/artist/{spotify_artist_id}"
        payload = self._get("url", {"resource": resource, "inc": "artist-rels"})
        if not payload:
            return None
        for relation in payload.get("relations") or []:
            if not isinstance(relation, Mapping):
                continue
            artist = relation.get("artist")
            if isinstance(artist, Mapping) and isinstance(artist.get("id"), str):
                return artist["id"]
        return None

    def artist(self, mbid: str) -> Optional[MBArtist]:
        if not mbid:
            return None
        payload = self._get(f"artist/{mbid}", {})
        if not payload or not payload.get("id"):
            return None
        country = payload.get("country")
        area = payload.get("area")
        begin_area = payload.get("begin-area") or payload.get("begin_area")
        resolved_country = country if isinstance(country, str) else None
        if not resolved_country:
            resolved_country = _area_country(area if isinstance(area, Mapping) else {})
        if not resolved_country:
            resolved_country = _area_country(begin_area if isinstance(begin_area, Mapping) else {})
        return MBArtist(
            mbid=str(payload["id"]),
            name=str(payload.get("name", "")),
            country=resolved_country.upper() if resolved_country else None,
            area=_area_name(area),
            begin_area=_area_name(begin_area),
            disambiguation=str(payload.get("disambiguation", "")),
            type=str(payload.get("type", "") or ""),
        )

    def search_artist(self, name: str, *, min_score: int = 92) -> Optional[MBArtist]:
        """Name-search fallback when no Spotify URL relation exists.

        Only an unambiguous, high-scoring, exactly-named hit is accepted -
        MusicBrainz name search happily returns near-misses, and a wrong artist
        would poison the nationality verdict.
        """
        if not name.strip():
            return None
        payload = self._get("artist", {"query": f'artist:"{name}"', "limit": 5})
        if not payload:
            return None
        candidates = [a for a in (payload.get("artists") or []) if isinstance(a, Mapping)]
        if not candidates:
            return None
        target = name.strip().casefold()
        exact = [
            a
            for a in candidates
            if str(a.get("name", "")).strip().casefold() == target
            and int(a.get("score", 0) or 0) >= min_score
        ]
        if len(exact) != 1:
            return None
        hit = exact[0]
        country = hit.get("country")
        area = hit.get("area") if isinstance(hit.get("area"), Mapping) else {}
        begin_area = hit.get("begin-area") if isinstance(hit.get("begin-area"), Mapping) else {}
        resolved = country if isinstance(country, str) else (
            _area_country(area) or _area_country(begin_area)
        )
        return MBArtist(
            mbid=str(hit.get("id", "")),
            name=str(hit.get("name", "")),
            country=resolved.upper() if resolved else None,
            area=_area_name(area),
            begin_area=_area_name(begin_area),
            disambiguation=str(hit.get("disambiguation", "")),
            type=str(hit.get("type", "") or ""),
        )

    # ------------------------------------------------------------------
    # Works / songwriters
    # ------------------------------------------------------------------
    def works_for_isrc(self, isrc: str) -> List[str]:
        """Work MBIDs reachable from an ISRC via its recordings."""
        if not isrc:
            return []
        payload = self._get(f"isrc/{isrc.upper()}", {"inc": "work-rels"})
        if not payload:
            return []
        work_ids: List[str] = []
        for recording in payload.get("recordings") or []:
            if not isinstance(recording, Mapping):
                continue
            for relation in recording.get("relations") or []:
                if not isinstance(relation, Mapping):
                    continue
                if relation.get("target-type") not in (None, "work"):
                    continue
                work = relation.get("work")
                if isinstance(work, Mapping) and isinstance(work.get("id"), str):
                    if work["id"] not in work_ids:
                        work_ids.append(work["id"])
        return work_ids

    def work(self, mbid: str) -> Optional[MBWork]:
        """A work plus its writer credits."""
        if not mbid:
            return None
        payload = self._get(f"work/{mbid}", {"inc": "artist-rels"})
        if not payload or not payload.get("id"):
            return None
        credits: Dict[str, MBWriterCredit] = {}
        for relation in payload.get("relations") or []:
            if not isinstance(relation, Mapping):
                continue
            rel_type = str(relation.get("type", "")).lower()
            if rel_type not in WRITER_ROLES:
                continue
            artist = relation.get("artist")
            if not isinstance(artist, Mapping):
                continue
            artist_id = artist.get("id")
            artist_name = str(artist.get("name", ""))
            if not isinstance(artist_id, str) or not artist_name:
                continue
            credit = credits.setdefault(artist_id, MBWriterCredit(artist_id, artist_name))
            credit.roles.add(rel_type)
        return MBWork(
            mbid=str(payload["id"]),
            title=str(payload.get("title", "")),
            writers=list(credits.values()),
        )

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------
    def labels_for_barcode(self, barcode: str) -> List[MBLabel]:
        """Resolve a release's labels from its UPC/EAN barcode."""
        if not barcode:
            return []
        payload = self._get("release", {"query": f"barcode:{barcode}", "limit": 5})
        if not payload:
            return []
        out: List[MBLabel] = []
        seen: Set[str] = set()
        for release in payload.get("releases") or []:
            if not isinstance(release, Mapping):
                continue
            for info in release.get("label-info") or []:
                if not isinstance(info, Mapping):
                    continue
                label = info.get("label")
                if not isinstance(label, Mapping):
                    continue
                mbid = label.get("id")
                if not isinstance(mbid, str) or mbid in seen:
                    continue
                seen.add(mbid)
                out.append(MBLabel(mbid=mbid, name=str(label.get("name", ""))))
        return out

    def label(self, mbid: str) -> Optional[MBLabel]:
        if not mbid:
            return None
        payload = self._get(f"label/{mbid}", {})
        if not payload or not payload.get("id"):
            return None
        country = payload.get("country")
        area = payload.get("area") if isinstance(payload.get("area"), Mapping) else {}
        resolved = country if isinstance(country, str) else _area_country(area)
        return MBLabel(
            mbid=str(payload["id"]),
            name=str(payload.get("name", "")),
            country=resolved.upper() if resolved else None,
            area=_area_name(area),
            type=str(payload.get("type", "") or ""),
        )

    def search_label(self, name: str, *, min_score: int = 92) -> Optional[MBLabel]:
        if not name.strip():
            return None
        payload = self._get("label", {"query": f'label:"{name}"', "limit": 5})
        if not payload:
            return None
        candidates = [l for l in (payload.get("labels") or []) if isinstance(l, Mapping)]
        target = name.strip().casefold()
        exact = [
            l
            for l in candidates
            if str(l.get("name", "")).strip().casefold() == target
            and int(l.get("score", 0) or 0) >= min_score
        ]
        if len(exact) != 1:
            return None
        hit = exact[0]
        country = hit.get("country")
        area = hit.get("area") if isinstance(hit.get("area"), Mapping) else {}
        resolved = country if isinstance(country, str) else _area_country(area)
        return MBLabel(
            mbid=str(hit.get("id", "")),
            name=str(hit.get("name", "")),
            country=resolved.upper() if resolved else None,
            area=_area_name(area),
            type=str(hit.get("type", "") or ""),
        )


class ArtistResolver:
    """Caches MusicBrainz artist lookups for the duration of a run."""

    def __init__(self, client: MusicBrainzClient) -> None:
        self.client = client
        self._by_spotify_id: Dict[str, Optional[MBArtist]] = {}
        self._by_mbid: Dict[str, Optional[MBArtist]] = {}

    def resolve_spotify_artist(self, spotify_id: str, name: str) -> Tuple[Optional[MBArtist], str]:
        """Return ``(artist, how)`` with *how* in ``spotify-url``/``name-search``/``""``."""
        if spotify_id in self._by_spotify_id:
            cached = self._by_spotify_id[spotify_id]
            return cached, ("spotify-url" if cached else "")
        mbid = self.client.artist_mbid_for_spotify(spotify_id)
        if mbid:
            artist = self.by_mbid(mbid)
            self._by_spotify_id[spotify_id] = artist
            if artist:
                return artist, "spotify-url"
        artist = self.client.search_artist(name)
        self._by_spotify_id[spotify_id] = artist
        return (artist, "name-search") if artist else (None, "")

    def by_mbid(self, mbid: str) -> Optional[MBArtist]:
        if mbid not in self._by_mbid:
            self._by_mbid[mbid] = self.client.artist(mbid)
        return self._by_mbid[mbid]

    def countries(self, mbids: Sequence[str]) -> Dict[str, Optional[str]]:
        return {mbid: (self.by_mbid(mbid).country if self.by_mbid(mbid) else None) for mbid in mbids}
