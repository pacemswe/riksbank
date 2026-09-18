"""Swedish Radar - find new Spotify releases with a Swedish connection.

Three criteria, evaluated independently and combined with OR:

1. ``artist`` - released by a Swedish artist.
2. ``label``  - released by a Swedish independent record label.
3. ``writer`` - written primarily by Swedish songwriters.

Every match carries the evidence it was built from, so a verdict can always be
audited rather than taken on trust.

Typical use::

    from swedish_radar import Pipeline, SpotifyClient, MusicBrainzClient, load_settings

    settings = load_settings()
    spotify = SpotifyClient(settings)
    pipeline = Pipeline(settings, spotify=spotify, musicbrainz=MusicBrainzClient(settings))
    result = pipeline.run(["artist", "label", "writer"])
"""

from __future__ import annotations

__version__ = "1.0.0"

from .cache import Cache
from .clients.musicbrainz import MusicBrainzClient
from .clients.spotify import SpotifyClient
from .config import ALL_CRITERIA, Settings, load_settings
from .detect.artist import detect_swedish_artist
from .detect.label import detect_swedish_independent_label
from .detect.writer import detect_swedish_writers
from .errors import ConfigError, HttpError, RadarError
from .models import Artist, Match, Release, RunResult, Signal, Track, Verdict, Writer
from .pipeline import Pipeline, build_sources
from .reference import Reference, load_reference
from .report import render
from .store import Store

__all__ = [
    "__version__",
    "ALL_CRITERIA",
    "Artist",
    "Cache",
    "ConfigError",
    "HttpError",
    "Match",
    "MusicBrainzClient",
    "Pipeline",
    "RadarError",
    "Reference",
    "Release",
    "RunResult",
    "Settings",
    "Signal",
    "SpotifyClient",
    "Store",
    "Track",
    "Verdict",
    "Writer",
    "build_sources",
    "detect_swedish_artist",
    "detect_swedish_independent_label",
    "detect_swedish_writers",
    "load_reference",
    "load_settings",
    "render",
]
