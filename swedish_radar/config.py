"""Runtime configuration.

Everything is read from the environment (or an optional ``.env`` file in the
working directory) so that no secret ever needs to live in the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional

from .errors import ConfigError

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
WEB_DIR = PACKAGE_DIR / "web"

DEFAULT_CACHE_PATH = Path(os.path.expanduser("~/.cache/swedish-radar/cache.sqlite3"))
DEFAULT_STORE_PATH = Path("swedish-radar.sqlite3")

#: Criterion identifiers used on the CLI, in the API and in stored results.
CRITERION_ARTIST = "artist"
CRITERION_LABEL = "label"
CRITERION_WRITER = "writer"
ALL_CRITERIA = (CRITERION_ARTIST, CRITERION_LABEL, CRITERION_WRITER)


def _read_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _env(name: str, dotenv: Dict[str, str], default: Optional[str] = None) -> Optional[str]:
    # Real environment wins over the .env file so CI/secrets override local files.
    return os.environ.get(name) or dotenv.get(name) or default


def _env_float(name: str, dotenv: Dict[str, str], default: float) -> float:
    raw = _env(name, dotenv)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, dotenv: Dict[str, str], default: int) -> int:
    raw = _env(name, dotenv)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable configuration for a discovery run."""

    # --- Spotify ---------------------------------------------------------
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    spotify_api_base: str = "https://api.spotify.com/v1"
    spotify_token_url: str = "https://accounts.spotify.com/api/token"

    # --- MusicBrainz -----------------------------------------------------
    musicbrainz_base: str = "https://musicbrainz.org/ws/2"
    #: MusicBrainz requires a contact address in the User-Agent.
    musicbrainz_contact: str = "swedish-radar (https://github.com/pacemswe/riksbank)"
    #: MusicBrainz asks for at most one request per second from anonymous clients.
    musicbrainz_rate_per_sec: float = 1.0
    musicbrainz_enabled: bool = True

    # --- Discovery window ------------------------------------------------
    markets: List[str] = field(default_factory=lambda: ["SE"])
    days: int = 14
    max_albums: int = 400

    # --- Thresholds ------------------------------------------------------
    min_confidence: float = 0.55
    #: Share of credited writers that must be Swedish for "primarily Swedish".
    writer_majority: float = 0.5

    # --- MusicBrainz budget ----------------------------------------------
    #: MusicBrainz allows ~1 request/second, so a run has to ration lookups.
    #: Everything is cached for a month, so budgets mostly bite on first runs.
    musicbrainz_budget: int = 600
    #: Tracks per release sampled for songwriter credits.
    writer_track_sample: int = 4
    #: How many releases may spend budget on songwriter lookups.
    writer_release_budget: int = 60

    # --- Storage ---------------------------------------------------------
    cache_path: Path = DEFAULT_CACHE_PATH
    store_path: Path = DEFAULT_STORE_PATH
    cache_ttl_short: int = 3600           # browse/new-releases, search
    cache_ttl_long: int = 7 * 86400       # albums, artists, tracks
    cache_ttl_musicbrainz: int = 30 * 86400
    cache_enabled: bool = True

    # --- HTTP ------------------------------------------------------------
    timeout: float = 20.0
    max_retries: int = 4
    user_agent: str = "swedish-radar/1.0 (+https://github.com/pacemswe/riksbank)"

    @property
    def has_spotify_credentials(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    def require_spotify(self) -> None:
        if not self.has_spotify_credentials:
            raise ConfigError(
                "Spotify credentials missing. Set SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET (env or .env file). Create them at "
                "https://developer.spotify.com/dashboard - the app only needs the "
                "Client Credentials flow, no redirect URI."
            )

    def with_overrides(self, **kwargs: object) -> "Settings":
        """Return a copy with non-``None`` keyword arguments applied."""
        clean = {key: value for key, value in kwargs.items() if value is not None}
        return replace(self, **clean)  # type: ignore[arg-type]


def load_settings(dotenv_path: Optional[Path] = None) -> Settings:
    """Build :class:`Settings` from the environment and an optional ``.env``."""
    dotenv = _read_dotenv(dotenv_path or Path(".env"))

    markets_raw = _env("RADAR_MARKETS", dotenv, "SE") or "SE"
    markets = [m.strip().upper() for m in markets_raw.split(",") if m.strip()]
    if not markets:
        markets = ["SE"]
    for market in markets:
        if len(market) != 2 or not market.isalpha():
            raise ConfigError(f"RADAR_MARKETS entries must be ISO country codes, got {market!r}")

    contact = _env("MUSICBRAINZ_CONTACT", dotenv)
    cache_path = _env("RADAR_CACHE_PATH", dotenv)
    store_path = _env("RADAR_STORE_PATH", dotenv)

    settings = Settings(
        spotify_client_id=_env("SPOTIFY_CLIENT_ID", dotenv),
        spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET", dotenv),
        markets=markets,
        days=_env_int("RADAR_DAYS", dotenv, 14),
        max_albums=_env_int("RADAR_MAX_ALBUMS", dotenv, 400),
        min_confidence=_env_float("RADAR_MIN_CONFIDENCE", dotenv, 0.55),
        writer_majority=_env_float("RADAR_WRITER_MAJORITY", dotenv, 0.5),
        musicbrainz_enabled=(_env("RADAR_MUSICBRAINZ", dotenv, "1") or "1").lower()
        not in {"0", "false", "no", "off"},
        musicbrainz_budget=_env_int("RADAR_MUSICBRAINZ_BUDGET", dotenv, 600),
        writer_track_sample=_env_int("RADAR_WRITER_TRACK_SAMPLE", dotenv, 4),
        writer_release_budget=_env_int("RADAR_WRITER_RELEASE_BUDGET", dotenv, 60),
        cache_enabled=(_env("RADAR_CACHE", dotenv, "1") or "1").lower()
        not in {"0", "false", "no", "off"},
        timeout=_env_float("RADAR_TIMEOUT", dotenv, 20.0),
    )
    if contact:
        settings = replace(settings, musicbrainz_contact=contact)
    if cache_path:
        settings = replace(settings, cache_path=Path(cache_path).expanduser())
    if store_path:
        settings = replace(settings, store_path=Path(store_path).expanduser())

    if not 0.0 <= settings.min_confidence <= 1.0:
        raise ConfigError("RADAR_MIN_CONFIDENCE must be between 0 and 1")
    if not 0.0 < settings.writer_majority <= 1.0:
        raise ConfigError("RADAR_WRITER_MAJORITY must be greater than 0 and at most 1")
    if settings.days < 1:
        raise ConfigError("RADAR_DAYS must be at least 1")
    return settings
