"""In-process fakes for the Spotify and MusicBrainz HTTP APIs.

They route by URL shape rather than exact string so tests stay readable, and
they count requests so the tests can assert on batching and rate limiting.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from swedish_radar.http import Response


def _json_response(url: str, payload: Any, status: int = 200) -> Response:
    return Response(status, {"Content-Type": "application/json"}, json.dumps(payload).encode(), url)


def _parse(url: str) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlparse(url)
    return parsed.path, urllib.parse.parse_qs(parsed.query)


@dataclass
class FakeSpotify:
    """A tiny in-memory Spotify.

    ``albums`` maps album id -> full album object; ``artists`` artist id ->
    artist object; ``tracks`` track id -> full track object (with ISRC).
    ``new_releases`` and ``searches`` hold the ids each source should return.
    """

    albums: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    artists: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tracks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    new_releases: Dict[str, List[str]] = field(default_factory=dict)
    searches: Dict[str, List[str]] = field(default_factory=dict)
    artist_search: Dict[str, List[str]] = field(default_factory=dict)
    artist_albums: Dict[str, List[str]] = field(default_factory=dict)
    calls: List[str] = field(default_factory=list)
    token_calls: int = 0

    def handle(self, method: str, url: str) -> Optional[Response]:
        if url.startswith("https://accounts.spotify.com/api/token"):
            self.token_calls += 1
            return _json_response(url, {"access_token": "fake-token", "expires_in": 3600})
        if "api.spotify.com" not in url:
            return None
        path, query = _parse(url)
        self.calls.append(path)

        if path.endswith("/browse/new-releases"):
            market = (query.get("country") or ["SE"])[0]
            offset = int((query.get("offset") or ["0"])[0])
            limit = int((query.get("limit") or ["50"])[0])
            ids = self.new_releases.get(market, [])
            page = ids[offset : offset + limit]
            return _json_response(
                url,
                {
                    "albums": {
                        "items": [self._simplified(i) for i in page],
                        "next": None if offset + limit >= len(ids) else "more",
                    }
                },
            )

        if path.endswith("/search"):
            q = (query.get("q") or [""])[0]
            search_type = (query.get("type") or ["album"])[0]
            offset = int((query.get("offset") or ["0"])[0])
            limit = int((query.get("limit") or ["50"])[0])
            if search_type == "artist":
                ids = self.artist_search.get(q, [])
                page = ids[offset : offset + limit]
                return _json_response(
                    url,
                    {"artists": {"items": [self.artists[i] for i in page if i in self.artists], "next": None}},
                )
            ids = self.searches.get(q, [])
            page = ids[offset : offset + limit]
            return _json_response(
                url,
                {
                    "albums": {
                        "items": [self._simplified(i) for i in page],
                        "next": None if offset + limit >= len(ids) else "more",
                    }
                },
            )

        if "/artists/" in path and path.endswith("/albums"):
            artist_id = path.split("/artists/")[1].split("/")[0]
            ids = self.artist_albums.get(artist_id, [])
            return _json_response(url, {"items": [self._simplified(i) for i in ids], "next": None})

        if path.endswith("/albums"):
            ids = (query.get("ids") or [""])[0].split(",")
            return _json_response(
                url, {"albums": [self.albums[i] for i in ids if i in self.albums]}
            )

        if path.endswith("/artists"):
            ids = (query.get("ids") or [""])[0].split(",")
            return _json_response(
                url, {"artists": [self.artists[i] for i in ids if i in self.artists]}
            )

        if path.endswith("/tracks"):
            ids = (query.get("ids") or [""])[0].split(",")
            return _json_response(
                url, {"tracks": [self.tracks[i] for i in ids if i in self.tracks]}
            )

        return _json_response(url, {"error": {"status": 404, "message": "not found"}}, 404)

    def _simplified(self, album_id: str) -> Dict[str, Any]:
        """Spotify's browse/search results omit label, copyrights and ISRCs."""
        album = self.albums.get(album_id, {"id": album_id, "name": album_id})
        return {
            key: value
            for key, value in album.items()
            if key not in ("label", "copyrights", "external_ids", "tracks", "popularity")
        }


@dataclass
class FakeMusicBrainz:
    """A tiny in-memory MusicBrainz."""

    #: spotify artist id -> mbid
    spotify_to_mbid: Dict[str, str] = field(default_factory=dict)
    #: mbid -> artist document
    mb_artists: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: isrc -> list of work mbids
    isrc_to_works: Dict[str, List[str]] = field(default_factory=dict)
    #: work mbid -> work document
    works: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: barcode -> list of label mbids
    barcode_to_labels: Dict[str, List[str]] = field(default_factory=dict)
    #: mbid -> label document
    mb_labels: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    artist_name_search: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    calls: List[str] = field(default_factory=list)

    def handle(self, method: str, url: str) -> Optional[Response]:
        if "musicbrainz.org" not in url:
            return None
        path, query = _parse(url)
        self.calls.append(path)
        tail = path.split("/ws/2/")[-1]

        if tail == "url":
            resource = (query.get("resource") or [""])[0]
            spotify_id = resource.rsplit("/", 1)[-1]
            mbid = self.spotify_to_mbid.get(spotify_id)
            if not mbid:
                return _json_response(url, {"error": "Not Found"}, 404)
            return _json_response(
                url,
                {
                    "id": "url-1",
                    "resource": resource,
                    "relations": [
                        {
                            "type": "free streaming",
                            "target-type": "artist",
                            "artist": {"id": mbid, "name": self.mb_artists.get(mbid, {}).get("name", "")},
                        }
                    ],
                },
            )

        if tail.startswith("artist/"):
            mbid = tail.split("/", 1)[1]
            doc = self.mb_artists.get(mbid)
            return _json_response(url, doc) if doc else _json_response(url, {}, 404)

        if tail == "artist":
            q = (query.get("query") or [""])[0]
            return _json_response(url, {"artists": self.artist_name_search.get(q, [])})

        if tail.startswith("isrc/"):
            isrc = tail.split("/", 1)[1]
            work_ids = self.isrc_to_works.get(isrc)
            if work_ids is None:
                return _json_response(url, {"error": "Not Found"}, 404)
            return _json_response(
                url,
                {
                    "isrc": isrc,
                    "recordings": [
                        {
                            "id": f"rec-{isrc}",
                            "title": "Recording",
                            "relations": [
                                {
                                    "type": "performance",
                                    "target-type": "work",
                                    "work": {"id": wid, "title": self.works.get(wid, {}).get("title", "")},
                                }
                                for wid in work_ids
                            ],
                        }
                    ],
                },
            )

        if tail.startswith("work/"):
            mbid = tail.split("/", 1)[1]
            doc = self.works.get(mbid)
            return _json_response(url, doc) if doc else _json_response(url, {}, 404)

        if tail == "release":
            q = (query.get("query") or [""])[0]
            barcode = q.split("barcode:")[-1] if "barcode:" in q else ""
            label_ids = self.barcode_to_labels.get(barcode, [])
            return _json_response(
                url,
                {
                    "releases": [
                        {
                            "id": "rel-1",
                            "label-info": [
                                {"label": {"id": lid, "name": self.mb_labels.get(lid, {}).get("name", "")}}
                                for lid in label_ids
                            ],
                        }
                    ]
                    if label_ids
                    else []
                },
            )

        if tail.startswith("label/"):
            mbid = tail.split("/", 1)[1]
            doc = self.mb_labels.get(mbid)
            return _json_response(url, doc) if doc else _json_response(url, {}, 404)

        if tail == "label":
            return _json_response(url, {"labels": []})

        return _json_response(url, {"error": "Not Found"}, 404)


@dataclass
class RoutingTransport:
    """Dispatches to the fakes; anything unrouted is a hard error."""

    spotify: FakeSpotify
    musicbrainz: FakeMusicBrainz
    request_count: int = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
    ) -> Response:
        self.request_count += 1
        for handler in (self.spotify.handle, self.musicbrainz.handle):
            response = handler(method, url)
            if response is not None:
                return response
        raise AssertionError(f"unrouted request: {method} {url}")
