"""Exception hierarchy for Swedish Radar."""

from __future__ import annotations


class RadarError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(RadarError):
    """Raised when configuration is missing or invalid."""


class TransportError(RadarError):
    """Raised when an HTTP request fails at the network level."""


class HttpError(RadarError):
    """Raised for non-success HTTP responses that we chose not to retry."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.url = url
        self.body = body
        snippet = body[:300].replace("\n", " ")
        super().__init__(f"HTTP {status} for {url}: {snippet}")


class AuthError(HttpError):
    """Raised when credentials are rejected (401/403 on the token endpoint)."""


class RateLimited(HttpError):
    """Raised when a request is rate limited and retries are exhausted."""

    def __init__(self, status: int, url: str, retry_after: float, body: str = "") -> None:
        self.retry_after = retry_after
        super().__init__(status, url, body)


class NotFound(HttpError):
    """Raised for 404 responses that callers usually want to treat as 'no data'."""
