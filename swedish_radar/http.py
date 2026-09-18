"""A small, dependency-free HTTP layer.

The client code talks to :class:`Transport`, never to ``urllib`` directly, so
tests can swap in :class:`FakeTransport` and run the whole pipeline offline.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from .errors import AuthError, HttpError, NotFound, RateLimited, TransportError

JsonDict = Dict[str, object]


@dataclass(frozen=True)
class Response:
    """A minimal HTTP response."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> JsonDict:
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(self.status, self.url, f"invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            return {"_": parsed}
        return parsed

    def header(self, name: str, default: str = "") -> str:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default


class Transport(Protocol):
    """Anything that can perform one HTTP round trip."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
    ) -> Response:  # pragma: no cover - protocol definition
        ...


class UrllibTransport:
    """Default transport built on the standard library."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
    ) -> Response:
        request = urllib.request.Request(url=url, data=body, method=method.upper())
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        request.add_header("Accept-Encoding", "gzip")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as handle:
                raw = handle.read()
                resp_headers = {k: v for k, v in handle.headers.items()}
                if handle.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return Response(handle.status, resp_headers, raw, url)
        except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
            raw = exc.read() or b""
            resp_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
            if resp_headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return Response(exc.code, resp_headers, raw, url)
        except urllib.error.URLError as exc:
            raise TransportError(f"{method} {url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"{method} {url} timed out after {timeout}s") from exc


@dataclass
class FakeTransport:
    """Transport backed by a canned ``{(method, url): Response}`` table.

    Used by the test-suite and by ``--offline`` runs against recorded fixtures.
    Unknown URLs raise, so a test can never silently pass by hitting nothing.
    """

    routes: Dict[Tuple[str, str], Response] = field(default_factory=dict)
    calls: List[Tuple[str, str]] = field(default_factory=list)
    #: Optional fallback consulted before giving up on an unknown URL.
    fallback: Optional[Callable[[str, str], Optional[Response]]] = None

    def add_json(self, method: str, url: str, payload: object, status: int = 200) -> None:
        self.routes[(method.upper(), url)] = Response(
            status,
            {"Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
            url,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
    ) -> Response:
        key = (method.upper(), url)
        self.calls.append(key)
        if key in self.routes:
            return self.routes[key]
        if self.fallback is not None:
            response = self.fallback(method.upper(), url)
            if response is not None:
                return response
        raise TransportError(f"FakeTransport has no route for {method.upper()} {url}")


class RateLimiter:
    """Thread-safe minimum-interval limiter (MusicBrainz allows ~1 req/s)."""

    def __init__(self, per_second: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self, now: Callable[[], float] = time.monotonic) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            current = now()
            wait = self._next_allowed - current
            if wait > 0:
                self._sleep(wait)
                current = self._next_allowed
            self._next_allowed = current + self._interval


def encode_query(params: Mapping[str, object]) -> str:
    """URL-encode query parameters, dropping ``None`` and preserving order."""
    pairs: List[Tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, (list, tuple)):
            pairs.append((key, ",".join(str(v) for v in value)))
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)


def build_url(base: str, path: str, params: Optional[Mapping[str, object]] = None) -> str:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}" if path else base.rstrip("/")
    query = encode_query(params or {})
    return f"{url}?{query}" if query else url


def retry_after_seconds(response: Response, default: float) -> float:
    raw = response.header("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return default


class JsonClient:
    """Shared retry/backoff behaviour for the API clients."""

    def __init__(
        self,
        transport: Transport,
        *,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 4,
        limiter: Optional[RateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = limiter
        self._sleep = sleep

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        headers.update(extra or {})
        return headers

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        allow_404: bool = False,
    ) -> Optional[JsonDict]:
        """Perform a request, retrying 429/5xx with exponential backoff.

        Returns ``None`` for a 404 when ``allow_404`` is set - the callers treat
        "no such recording/label" as an ordinary outcome, not an error.
        """
        attempt = 0
        while True:
            if self.limiter is not None:
                self.limiter.acquire()
            response = self.transport.request(
                method,
                url,
                headers=self._headers(headers),
                body=body,
                timeout=self.timeout,
            )
            status = response.status

            if 200 <= status < 300:
                return response.json()
            if status == 404:
                if allow_404:
                    return None
                raise NotFound(status, url, response.body.decode("utf-8", "replace"))
            if status in (401, 403):
                raise AuthError(status, url, response.body.decode("utf-8", "replace"))

            retryable = status == 429 or 500 <= status < 600
            if not retryable or attempt >= self.max_retries:
                text = response.body.decode("utf-8", "replace")
                if status == 429:
                    raise RateLimited(status, url, retry_after_seconds(response, 1.0), text)
                raise HttpError(status, url, text)

            backoff = retry_after_seconds(response, min(2.0 ** attempt, 30.0))
            self._sleep(backoff)
            attempt += 1

    def get_json(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        allow_404: bool = False,
    ) -> Optional[JsonDict]:
        return self.request_json("GET", url, headers=headers, allow_404=allow_404)


def chunked(items: Iterable[str], size: int) -> Iterable[List[str]]:
    """Yield ``size``-long chunks - Spotify's batch endpoints cap at 20/50 ids."""
    batch: List[str] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
