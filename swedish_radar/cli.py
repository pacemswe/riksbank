"""Command line interface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .cache import Cache
from .clients.musicbrainz import MusicBrainzClient
from .clients.spotify import SpotifyClient
from .config import ALL_CRITERIA, DATA_DIR, Settings, load_settings
from .errors import ConfigError, RadarError
from .models import Match, Release, run_result_from_dict
from .pipeline import Pipeline
from .reference import load_reference
from .report import CRITERION_LABELS, render
from .store import Store
from .textutil import label_key

SPOTIFY_ID_RE = re.compile(r"(?:album[:/])([A-Za-z0-9]{22})")


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _parse_criteria(raw: Optional[str]) -> List[str]:
    if not raw or raw.strip().lower() == "all":
        return list(ALL_CRITERIA)
    wanted = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [w for w in wanted if w not in ALL_CRITERIA]
    if unknown:
        raise ConfigError(
            f"unknown criteria: {', '.join(unknown)}. Choose from {', '.join(ALL_CRITERIA)}."
        )
    return wanted


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings(Path(args.env_file) if getattr(args, "env_file", None) else None)
    overrides = {}
    if getattr(args, "markets", None):
        overrides["markets"] = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    for name in (
        "days",
        "max_albums",
        "min_confidence",
        "writer_majority",
        "musicbrainz_budget",
        "writer_release_budget",
        "writer_track_sample",
    ):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    if getattr(args, "no_musicbrainz", False):
        overrides["musicbrainz_enabled"] = False
    if getattr(args, "no_cache", False):
        overrides["cache_enabled"] = False
    if getattr(args, "cache_path", None):
        overrides["cache_path"] = Path(args.cache_path)
    if getattr(args, "store_path", None):
        overrides["store_path"] = Path(args.store_path)
    return settings.with_overrides(**overrides)


def build_pipeline(settings: Settings, *, quiet: bool = False, today=None) -> Tuple[Pipeline, Cache]:
    settings.require_spotify()
    cache = Cache(settings.cache_path, enabled=settings.cache_enabled)
    spotify = SpotifyClient(settings, cache=cache)
    musicbrainz = MusicBrainzClient(settings, cache=cache) if settings.musicbrainz_enabled else None
    progress = (lambda _m: None) if quiet else (lambda m: _eprint(m))
    pipeline = Pipeline(
        settings,
        spotify=spotify,
        musicbrainz=musicbrainz,
        cache=cache,
        progress=progress,
        today=today,
    )
    return pipeline, cache


# ----------------------------------------------------------------------
# discover
# ----------------------------------------------------------------------
def cmd_discover(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    criteria = _parse_criteria(args.criteria)
    pipeline, cache = build_pipeline(settings, quiet=args.quiet)
    try:
        result = pipeline.run(criteria)
    finally:
        cache.close()

    output = render(result, args.format, today=pipeline.today)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        _eprint(f"Wrote {len(result.matches)} match(es) to {path}")
    else:
        sys.stdout.write(output)

    if args.save:
        with Store(settings.store_path) as store:
            run_id = store.save_run(result, today=pipeline.today)
        _eprint(f"Saved run #{run_id} to {settings.store_path}")
    return 0


# ----------------------------------------------------------------------
# explain
# ----------------------------------------------------------------------
def cmd_explain(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    criteria = _parse_criteria(args.criteria)
    match = SPOTIFY_ID_RE.search(args.album)
    album_id = match.group(1) if match else args.album.strip()

    pipeline, cache = build_pipeline(settings, quiet=args.quiet)
    try:
        payloads = pipeline.spotify.albums([album_id], market=settings.markets[0])
        if not payloads:
            _eprint(f"No album found for {album_id!r}")
            return 1
        release = Release.from_spotify(payloads[0])
        releases, artists = pipeline.enrich_releases({release.id: release})
        release = releases.get(release.id, release)
        pipeline.enrich_artists_from_musicbrainz(artists)
        release_artists = [artists[a] for a in release.artist_ids if a in artists]
        verdicts = pipeline.evaluate(release, release_artists, criteria)
    finally:
        cache.close()

    if args.format == "json":
        payload = Match(release=release, artists=release_artists, verdicts=verdicts).to_dict(
            pipeline.today
        )
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0

    print(f"{', '.join(release.artist_names)} — {release.name}")
    print(f"  {release.release_date_raw} · {release.album_type} · label: {release.label or '—'}")
    print(f"  {release.spotify_url}")
    print()
    for name, verdict in verdicts.items():
        mark = "MATCH " if verdict.matched else "no    "
        print(f"  [{mark}] {CRITERION_LABELS.get(name, name)} ({verdict.confidence:.2f})")
        print(f"           {verdict.summary}")
        for signal in verdict.signals:
            sign = "+" if signal.weight > 0 else "−"
            print(f"           {sign}{abs(signal.weight):.2f} {signal.detail} [{signal.source}]")
        print()
    return 0


# ----------------------------------------------------------------------
# render
# ----------------------------------------------------------------------
def cmd_render(args: argparse.Namespace) -> int:
    """Re-render a saved run in another format, without touching the network."""
    if args.input:
        path = Path(args.input)
        if not path.is_file():
            _eprint(f"No such file: {path}")
            return 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _eprint(f"{path} is not valid JSON: {exc}")
            return 1
    else:
        settings = _settings_from_args(args)
        with Store(settings.store_path) as store:
            payload = store.get_run(args.run) if args.run else store.latest_run()
        if payload is None:
            where = f"run #{args.run}" if args.run else "any run"
            _eprint(f"No {where} in {settings.store_path}. Use `discover --save` first.")
            return 1

    result = run_result_from_dict(payload)
    output = render(result, args.format)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        _eprint(f"Wrote {len(result.matches)} match(es) to {out_path}")
    else:
        sys.stdout.write(output)
    return 0


# ----------------------------------------------------------------------
# labels
# ----------------------------------------------------------------------
def cmd_labels(args: argparse.Namespace) -> int:
    reference = load_reference()
    if args.labels_command == "list":
        entries = sorted(reference.labels.entries, key=lambda e: e.name.casefold())
        for entry in entries:
            flag = "" if entry.independent else "  (not independent)"
            place = f" · {entry.city}" if entry.city else ""
            scene = f" · {entry.scene}" if entry.scene else ""
            print(f"{entry.confidence:.2f}  {entry.name}{place}{scene}{flag}")
        print(f"\n{len(entries)} label(s) in {DATA_DIR / 'swedish_labels.json'}")
        return 0

    if args.labels_command == "check":
        for name in args.names:
            entry, how = reference.labels.lookup(name)
            major = reference.majors.group_for(name)
            aggregator = reference.majors.aggregator_for(name)
            friend = reference.labels.lookup_false_friend(name)
            if entry is not None:
                verdict = f"Swedish independent: {entry.name} ({entry.city or '—'}) [{how} match]"
            elif friend is not None:
                verdict = f"not Swedish: {friend.name} is {friend.country or 'foreign'} — {friend.note}"
            elif major:
                verdict = f"major label: {major}"
            elif aggregator:
                verdict = f"DIY distributor: {aggregator} (carries no label information)"
            else:
                verdict = "unknown — not on any list"
            print(f"{name}: {verdict}")
        return 0

    if args.labels_command == "add":
        path = DATA_DIR / "swedish_labels.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = {label_key(l["name"]) for l in payload["labels"]}
        if label_key(args.name) in existing:
            _eprint(f"{args.name!r} is already on the list")
            return 1
        payload["labels"].append(
            {
                "name": args.name,
                "aliases": [a for a in (args.alias or [])],
                "city": args.city or "",
                "scene": args.scene or "",
                "confidence": args.confidence,
            }
        )
        payload["labels"].sort(key=lambda l: (-float(l.get("confidence", 0)), l["name"]))
        payload["updated"] = datetime.now(timezone.utc).date().isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        load_reference.cache_clear()
        print(f"Added {args.name} to {path}")
        return 0

    raise ConfigError(f"unknown labels sub-command {args.labels_command!r}")


# ----------------------------------------------------------------------
# serve / cache / doctor
# ----------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    settings = _settings_from_args(args)
    serve(settings, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    cache = Cache(settings.cache_path, enabled=True)
    try:
        if args.cache_command == "clear":
            cache.clear()
            print(f"Cleared {settings.cache_path}")
        else:
            removed = cache.purge_expired()
            print(f"Purged {removed} expired entr{'y' if removed == 1 else 'ies'}")
    finally:
        cache.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    reference = load_reference()
    print("Swedish Radar configuration")
    print(f"  Spotify credentials : {'present' if settings.has_spotify_credentials else 'MISSING'}")
    print(f"  Markets             : {', '.join(settings.markets)}")
    print(f"  Window              : last {settings.days} day(s)")
    print(f"  Min confidence      : {settings.min_confidence}")
    print(f"  Writer majority     : {settings.writer_majority}")
    print(f"  MusicBrainz         : {'on' if settings.musicbrainz_enabled else 'off'}"
          f" (budget {settings.musicbrainz_budget} request(s), contact {settings.musicbrainz_contact})")
    print(f"  Cache               : {settings.cache_path} ({'on' if settings.cache_enabled else 'off'})")
    print(f"  Store               : {settings.store_path}")
    print(f"  Curated labels      : {len(reference.labels.entries)}")
    print(f"  Major markers       : {sum(len(v) for v in reference.majors.groups.values())}")
    if not settings.has_spotify_credentials:
        print()
        print("  Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET before running `discover`.")
        return 1
    if not settings.musicbrainz_enabled:
        print()
        print("  MusicBrainz is off: artist nationality and songwriter credits will be unavailable.")
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swedish-radar",
        description=(
            "Find new Spotify releases by Swedish artists, on Swedish independent "
            "labels, or written primarily by Swedish songwriters."
        ),
    )
    parser.add_argument("--env-file", help="path to a .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--markets", help="comma-separated ISO country codes (default: SE)")
        p.add_argument("--days", type=int, help="how far back to look (default: 14)")
        p.add_argument("--max-albums", type=int, help="cap on releases examined (default: 400)")
        p.add_argument("--min-confidence", type=float, help="match threshold 0-1 (default: 0.55)")
        p.add_argument(
            "--writer-majority",
            type=float,
            help="share of credited writers that must be Swedish (default: 0.5)",
        )
        p.add_argument("--musicbrainz-budget", type=int, help="max MusicBrainz requests per run")
        p.add_argument("--writer-release-budget", type=int, help="releases to check for songwriters")
        p.add_argument("--writer-track-sample", type=int, help="tracks per release checked for songwriters")
        p.add_argument("--no-musicbrainz", action="store_true", help="skip MusicBrainz entirely")
        p.add_argument("--no-cache", action="store_true", help="bypass the response cache")
        p.add_argument("--cache-path", help="override the cache database path")
        p.add_argument("--store-path", help="override the results database path")
        p.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    discover = sub.add_parser("discover", help="run a discovery pass")
    add_common(discover)
    discover.add_argument(
        "-c",
        "--criteria",
        default="all",
        help="comma-separated: artist, label, writer (default: all)",
    )
    discover.add_argument(
        "-f", "--format", default="text", choices=["text", "json", "csv", "markdown", "md"]
    )
    discover.add_argument("-o", "--out", help="write to a file instead of stdout")
    discover.add_argument("--save", action="store_true", help="also store the run in the results database")
    discover.set_defaults(func=cmd_discover)

    explain = sub.add_parser("explain", help="show the full evidence for one album")
    add_common(explain)
    explain.add_argument("album", help="Spotify album id or URL")
    explain.add_argument("-c", "--criteria", default="all")
    explain.add_argument("-f", "--format", default="text", choices=["text", "json"])
    explain.set_defaults(func=cmd_explain)

    render_cmd = sub.add_parser(
        "render", help="re-render a saved run in another format (no network access)"
    )
    render_cmd.add_argument("--input", help="a JSON file written by `discover --format json`")
    render_cmd.add_argument("--run", type=int, help="a run id from the results database")
    render_cmd.add_argument("--store-path", help="override the results database path")
    render_cmd.add_argument(
        "-f", "--format", default="markdown", choices=["text", "json", "csv", "markdown", "md"]
    )
    render_cmd.add_argument("-o", "--out", help="write to a file instead of stdout")
    render_cmd.set_defaults(func=cmd_render)

    serve = sub.add_parser("serve", help="run the web UI")
    add_common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    serve.set_defaults(func=cmd_serve)

    labels = sub.add_parser("labels", help="inspect or extend the curated label list")
    labels_sub = labels.add_subparsers(dest="labels_command", required=True)
    labels_sub.add_parser("list", help="print every curated label")
    check = labels_sub.add_parser("check", help="classify one or more label strings")
    check.add_argument("names", nargs="+")
    add = labels_sub.add_parser("add", help="append a label to the curated list")
    add.add_argument("name")
    add.add_argument("--alias", action="append", help="alternative spelling (repeatable)")
    add.add_argument("--city")
    add.add_argument("--scene")
    add.add_argument("--confidence", type=float, default=0.8)
    labels.set_defaults(func=cmd_labels)

    cache = sub.add_parser("cache", help="manage the HTTP response cache")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_sub.add_parser("clear", help="delete every cached response")
    cache_sub.add_parser("purge", help="delete expired entries only")
    cache.set_defaults(func=cmd_cache)

    doctor = sub.add_parser("doctor", help="check configuration")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        _eprint(f"Configuration error: {exc}")
        return 2
    except RadarError as exc:
        _eprint(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        _eprint("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
