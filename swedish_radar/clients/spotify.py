"""Spotify Web API client (Client Credentials flow).

Only endpoints that work without a logged-in user are used, so the app needs
nothing but a Client ID and Secret from the Spotify developer dashboard.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from ..cache import Cache
from ..config import Settings
from ..errors import AuthError, HttpError
from ..http import JsonClient, Transport, UrllibTransport, build_url, chunked, encode_query

#: Spotify's batch endpoints cap the number of ids per request.
MAX_ALBUM_IDS = 20
MAX_ARTIST_IDS = 50
MAX_TRACK_IDS = 50
MAX_PAGE_LIMIT = 50
#: The search endpoint refuses offset + limit > 1000.
MAX_SEARCH_OFFSET = 950


@dataclass
class _Token:
    value: str
    expires_at: float

    def valid(self, now: float, skew: float = 30.0) -> bool:
        return bool(self.value) and now + skew < self.expires_at


class SpotifyClient:
    """Thin, cached wrapper over the endpoints the pipeline needs."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[Transport] = None,
        cache: Optional[Cache] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self._clock = clock
        self._token: Optional[_Token] = None
        self._client = JsonClient(
            transport or UrllibTransport(),
            user_agent=settings.user_agent,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            sleep=sleep,
        )
        self.request_count = 0

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _authorization(self) -> str:
        self.settings.require_spotify()
        pair = f"{self.settings.spotify_client_id}:{self.settings.spotify_client_secret}"
        return "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")

    def token(self, force: bool = False) -> str:
        now = self._clock()
        if not force and self._token is not None and self._token.valid(now):
            return self._token.value
        self.request_count += 1
        payload = self._client.request_json(
            "POST",
            self.settings.spotify_token_url,
            headers={
                "Authorization": self._authorization(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=b"grant_type=client_credentials",
        )
        if not payload or not isinstance(payload.get("access_token"), str):
            raise AuthError(200, self.settings.spotify_token_url, "token response had no access_token")
        expires_in = payload.get("expires_in")
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        self._token = _Token(str(payload["access_token"]), now + ttl)
        return self._token.value

    # ------------------------------------------------------------------
    # Core request helper
    # ------------------------------------------------------------------
    def _get(
        self,
        path: str,
        params: Optional[Mapping[str, object]] = None,
        *,
        ttl: Optional[int] = None,
        allow_404: bool = False,
    ) -> Optional[Dict[str, Any]]:
        url = build_url(self.settings.spotify_api_base, path, params)
        cache_key = f"spotify:{url}"
        cache_ttl = self.settings.cache_ttl_long if ttl is None else ttl
        if self.cache is not None and cache_ttl > 0:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached if isinstance(cached, dict) else None

        headers = {"Authorization": f"Bearer {self.token()}"}
        self.request_count += 1
        try:
            payload = self._client.get_json(url, headers=headers, allow_404=allow_404)
        except AuthError as exc:
            if exc.status != 401:
                raise
            # The cached token expired early or was revoked - one forced refresh.
            headers = {"Authorization": f"Bearer {self.token(force=True)}"}
            self.request_count += 1
            payload = self._client.get_json(url, headers=headers, allow_404=allow_404)

        if payload is not None and self.cache is not None and cache_ttl > 0:
            self.cache.set(cache_key, payload, cache_ttl)
        return payload

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    def new_releases(self, market: str, limit: int = 200) -> List[Dict[str, Any]]:
        """``/browse/new-releases`` for one market, paged."""
        items: List[Dict[str, Any]] = []
        offset = 0
        while len(items) < limit:
            page_size = min(MAX_PAGE_LIMIT, limit - len(items))
            payload = self._get(
                "browse/new-releases",
                {"country": market, "limit": page_size, "offset": offset},
                ttl=self.settings.cache_ttl_short,
            )
            albums = ((payload or {}).get("albums") or {}) if payload else {}
            batch = albums.get("items") or []
            items.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < page_size or not albums.get("next"):
                break
            offset += page_size
        return items

    def search_albums(
        self,
        query: str,
        *,
        market: str = "SE",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """``/search?type=album``, paged up to ``limit`` results."""
        items: List[Dict[str, Any]] = []
        offset = 0
        while len(items) < limit and offset <= MAX_SEARCH_OFFSET:
            page_size = min(MAX_PAGE_LIMIT, limit - len(items))
            payload = self._get(
                "search",
                {
                    "q": query,
                    "type": "album",
                    "market": market,
                    "limit": page_size,
                    "offset": offset,
                },
                ttl=self.settings.cache_ttl_short,
            )
            albums = ((payload or {}).get("albums") or {}) if payload else {}
            batch = albums.get("items") or []
            items.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < page_size or not albums.get("next"):
                break
            offset += page_size
        return items

    def search_artists(
        self,
        query: str,
        *,
        market: str = "SE",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        while len(items) < limit and offset <= MAX_SEARCH_OFFSET:
            page_size = min(MAX_PAGE_LIMIT, limit - len(items))
            payload = self._get(
                "search",
                {
                    "q": query,
                    "type": "artist",
                    "market": market,
                    "limit": page_size,
                    "offset": offset,
                },
                ttl=self.settings.cache_ttl_short,
            )
            artists = ((payload or {}).get("artists") or {}) if payload else {}
            batch = artists.get("items") or []
            items.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < page_size or not artists.get("next"):
                break
            offset += page_size
        return items

    def artist_albums(
        self,
        artist_id: str,
        *,
        market: str = "SE",
        include_groups: Sequence[str] = ("album", "single"),
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        payload = self._get(
            f"artists/{artist_id}/albums",
            {
                "include_groups": ",".join(include_groups),
                "market": market,
                "limit": min(MAX_PAGE_LIMIT, limit),
            },
            ttl=self.settings.cache_ttl_short,
            allow_404=True,
        )
        items = (payload or {}).get("items") or []
        return [a for a in items if isinstance(a, dict)]

    def albums(self, album_ids: Iterable[str], *, market: str = "SE") -> List[Dict[str, Any]]:
        """Full album objects (these carry ``label``, ``copyrights`` and ``external_ids``)."""
        out: List[Dict[str, Any]] = []
        for batch in chunked(_dedupe(album_ids), MAX_ALBUM_IDS):
            payload = self._get("albums", {"ids": batch, "market": market})
            for album in (payload or {}).get("albums") or []:
                if isinstance(album, dict) and album.get("id"):
                    out.append(album)
        return out

    def artists(self, artist_ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Full artist objects (these carry ``genres`` and ``popularity``)."""
        out: List[Dict[str, Any]] = []
        for batch in chunked(_dedupe(artist_ids), MAX_ARTIST_IDS):
            payload = self._get("artists", {"ids": batch})
            for artist in (payload or {}).get("artists") or []:
                if isinstance(artist, dict) and artist.get("id"):
                    out.append(artist)
        return out

    def tracks(self, track_ids: Iterable[str], *, market: str = "SE") -> List[Dict[str, Any]]:
        """Full track objects - the only place Spotify exposes ISRCs."""
        out: List[Dict[str, Any]] = []
        for batch in chunked(_dedupe(track_ids), MAX_TRACK_IDS):
            payload = self._get("tracks", {"ids": batch, "market": market})
            for track in (payload or {}).get("tracks") or []:
                if isinstance(track, dict) and track.get("id"):
                    out.append(track)
        return out

    def album_tracks(self, album_id: str, *, market: str = "SE", limit: int = 300) -> List[Dict[str, Any]]:
        """Remaining pages of a long album's tracklist."""
        items: List[Dict[str, Any]] = []
        offset = 0
        while len(items) < limit:
            page_size = min(MAX_PAGE_LIMIT, limit - len(items))
            try:
                payload = self._get(
                    f"albums/{album_id}/tracks",
                    {"market": market, "limit": page_size, "offset": offset},
                    allow_404=True,
                )
            except HttpError:
                break
            if payload is None:
                break
            batch = payload.get("items") or []
            items.extend(t for t in batch if isinstance(t, dict))
            if len(batch) < page_size or not payload.get("next"):
                break
            offset += page_size
        return items


def _dedupe(values: Iterable[str]) -> Iterator[str]:
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            yield value


def quote_search_value(value: str) -> str:
    """Quote a value for use inside a Spotify search filter.

    Spotify has no escape syntax inside quoted filter values, so an embedded
    double quote would end the phrase early and silently corrupt the query -
    strip them instead.
    """
    return value.replace('"', "").strip()


def label_query(label: str, *, new_only: bool = True, year: Optional[int] = None) -> str:
    parts = [f'label:"{quote_search_value(label)}"']
    if new_only:
        parts.append("tag:new")
    if year:
        parts.append(f"year:{year}")
    return " ".join(parts)


def new_release_query(year: Optional[int] = None) -> str:
    return f"tag:new year:{year}" if year else "tag:new"


__all__ = [
    "SpotifyClient",
    "label_query",
    "new_release_query",
    "quote_search_value",
    "encode_query",
]
