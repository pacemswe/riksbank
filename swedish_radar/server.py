"""A dependency-free web UI.

Bound to localhost by default: it is a personal tool with no authentication,
and the Spotify credentials live in the process that serves it.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from typing import Any, Dict, List, Optional, Tuple

from .cache import Cache
from .clients.musicbrainz import MusicBrainzClient
from .clients.spotify import SpotifyClient
from .config import ALL_CRITERIA, WEB_DIR, Settings
from .errors import RadarError
from .pipeline import Pipeline
from .reference import load_reference
from .store import Store

MAX_BODY_BYTES = 64 * 1024


class Job:
    """One background discovery run."""

    _ids = count(1)

    def __init__(self, criteria: List[str], settings: Settings) -> None:
        self.id = next(self._ids)
        self.criteria = criteria
        self.settings = settings
        self.status = "queued"
        self.log: List[str] = []
        self.run_id: Optional[int] = None
        self.error: Optional[str] = None
        self.match_count = 0
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.finished_at: Optional[str] = None
        self._lock = threading.Lock()

    def note(self, message: str) -> None:
        with self._lock:
            self.log.append(message)
            # Keep the log bounded - a long run can emit hundreds of lines.
            if len(self.log) > 400:
                del self.log[: len(self.log) - 400]

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "criteria": self.criteria,
                "log": list(self.log),
                "run_id": self.run_id,
                "error": self.error,
                "match_count": self.match_count,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


class RadarService:
    """Owns the store and serialises discovery runs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.store_path)
        self.jobs: Dict[int, Job] = {}
        self._run_lock = threading.Lock()
        self._jobs_lock = threading.Lock()

    def start_run(self, criteria: List[str], overrides: Dict[str, Any]) -> Job:
        settings = self.settings.with_overrides(**overrides)
        job = Job(criteria, settings)
        with self._jobs_lock:
            self.jobs[job.id] = job
            # Keep only the most recent jobs in memory.
            for stale in sorted(self.jobs)[:-20]:
                self.jobs.pop(stale, None)
        thread = threading.Thread(target=self._execute, args=(job,), daemon=True)
        thread.start()
        return job

    def _execute(self, job: Job) -> None:
        if not self._run_lock.acquire(blocking=False):
            job.status = "error"
            job.error = "Another discovery run is already in progress."
            job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return
        cache: Optional[Cache] = None
        try:
            job.status = "running"
            settings = job.settings
            settings.require_spotify()
            cache = Cache(settings.cache_path, enabled=settings.cache_enabled)
            spotify = SpotifyClient(settings, cache=cache)
            musicbrainz = (
                MusicBrainzClient(settings, cache=cache) if settings.musicbrainz_enabled else None
            )
            pipeline = Pipeline(
                settings,
                spotify=spotify,
                musicbrainz=musicbrainz,
                cache=cache,
                progress=job.note,
            )
            result = pipeline.run(job.criteria)
            job.run_id = self.store.save_run(result, today=pipeline.today)
            job.match_count = len(result.matches)
            job.status = "done"
            job.note(f"Done: {len(result.matches)} match(es).")
        except RadarError as exc:
            job.status = "error"
            job.error = str(exc)
            job.note(f"Failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures in the UI
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.note(traceback.format_exc(limit=3))
        finally:
            if cache is not None:
                cache.close()
            job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._run_lock.release()

    def job(self, job_id: int) -> Optional[Job]:
        with self._jobs_lock:
            return self.jobs.get(job_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "SwedishRadar/1.0"
    service: RadarService

    # -- plumbing -----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # BaseHTTPRequestHandler logs to stderr for every request; too noisy.
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _static(self, relative: str) -> None:
        # Resolve inside WEB_DIR so "../" cannot escape the asset directory.
        target = (WEB_DIR / relative).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        content_type, _ = mimetypes.guess_type(str(target))
        self._send(200, target.read_bytes(), content_type or "application/octet-stream")

    # -- routing ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._static("index.html")
            return
        if path.startswith("/static/"):
            self._static(path[len("/static/") :])
            return
        if path == "/api/config":
            self._json(self._config_payload())
            return
        if path == "/api/labels":
            reference = load_reference()
            self._json(
                {
                    "labels": [entry.to_dict() for entry in reference.labels.entries],
                    "count": len(reference.labels.entries),
                }
            )
            return
        if path == "/api/runs":
            limit = _int_param(query, "limit", 30)
            self._json({"runs": self.service.store.list_runs(limit)})
            return
        if path == "/api/runs/latest":
            run = self.service.store.latest_run()
            self._json(run or {"matches": [], "count": 0, "empty": True})
            return
        if path.startswith("/api/runs/"):
            try:
                run_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "run id must be an integer")
                return
            run = self.service.store.get_run(run_id)
            if run is None:
                self._error(HTTPStatus.NOT_FOUND, f"no run #{run_id}")
                return
            self._json(run)
            return
        if path.startswith("/api/jobs/"):
            try:
                job_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "job id must be an integer")
                return
            job = self.service.job(job_id)
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, f"no job #{job_id}")
                return
            self._json(job.to_dict())
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/discover":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        criteria = [c for c in payload.get("criteria", list(ALL_CRITERIA)) if c in ALL_CRITERIA]
        if not criteria:
            self._error(HTTPStatus.BAD_REQUEST, "choose at least one criterion")
            return
        overrides: Dict[str, Any] = {}
        for key, caster in (
            ("days", int),
            ("max_albums", int),
            ("min_confidence", float),
            ("writer_majority", float),
        ):
            if key in payload and payload[key] is not None:
                try:
                    overrides[key] = caster(payload[key])
                except (TypeError, ValueError):
                    self._error(HTTPStatus.BAD_REQUEST, f"{key} must be a number")
                    return
        markets = payload.get("markets")
        if isinstance(markets, list) and markets:
            overrides["markets"] = [str(m).upper()[:2] for m in markets]
        job = self.service.start_run(criteria, overrides)
        self._json(job.to_dict(), HTTPStatus.ACCEPTED)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return None
        if length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "body must be JSON")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
            return None
        return payload

    def _config_payload(self) -> Dict[str, Any]:
        settings = self.service.settings
        reference = load_reference()
        return {
            "criteria": list(ALL_CRITERIA),
            "markets": settings.markets,
            "days": settings.days,
            "max_albums": settings.max_albums,
            "min_confidence": settings.min_confidence,
            "writer_majority": settings.writer_majority,
            "musicbrainz": settings.musicbrainz_enabled,
            "has_credentials": settings.has_spotify_credentials,
            "curated_labels": len(reference.labels.entries),
            "store_path": str(settings.store_path),
        }


def _int_param(query: Dict[str, List[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        return max(1, min(200, int(values[0])))
    except ValueError:
        return default


def make_server(settings: Settings, host: str, port: int) -> Tuple[ThreadingHTTPServer, RadarService]:
    service = RadarService(settings)
    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, service


def serve(settings: Settings, *, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> None:
    httpd, service = make_server(settings, host, port)
    url = f"http://{host}:{port}/"
    print(f"Swedish Radar running at {url}")
    if not settings.has_spotify_credentials:
        print("  ! Spotify credentials missing - the UI will load but cannot start a run.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless environments have no browser
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        httpd.server_close()
        service.store.close()
