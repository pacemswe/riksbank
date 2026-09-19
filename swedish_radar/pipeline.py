"""The discovery pipeline: sources -> enrichment -> detection -> ranking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .cache import Cache
from .clients.musicbrainz import ArtistResolver, MusicBrainzClient
from .clients.spotify import SpotifyClient, label_query, new_release_query
from .config import (
    ALL_CRITERIA,
    CRITERION_ARTIST,
    CRITERION_LABEL,
    CRITERION_WRITER,
    Settings,
)
from .detect.artist import detect_swedish_artist
from .detect.label import LabelContext, detect_swedish_independent_label
from .detect.writer import TrackWriters, WriterContext, detect_swedish_writers, writer_from_credit
from .models import Artist, Match, Release, RunResult, RunStats, Track, Verdict
from .reference import Reference, label_search_names, load_reference

ProgressFn = Callable[[str], None]


def _noop(_message: str) -> None:
    pass


@dataclass
class Budget:
    """A simple countdown used to ration MusicBrainz lookups."""

    remaining: int

    def spend(self, amount: int = 1) -> bool:
        if self.remaining < amount:
            return False
        self.remaining -= amount
        return True

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass
class SourceSpec:
    """One way of asking Spotify for candidate releases."""

    kind: str          # "new-releases" | "label-search" | "tag-new" | "genre-artists"
    label: str         # human-readable description
    market: str = "SE"
    query: str = ""
    limit: int = 50


def build_sources(
    settings: Settings,
    criteria: Sequence[str],
    reference: Reference,
    *,
    today: Optional[date] = None,
    label_query_limit: int = 30,
) -> List[SourceSpec]:
    """Choose which Spotify queries to run for the requested criteria."""
    today = today or datetime.now(timezone.utc).date()
    year = today.year
    sources: List[SourceSpec] = []

    for market in settings.markets:
        sources.append(
            SourceSpec(
                kind="new-releases",
                label=f"New releases feed ({market})",
                market=market,
                limit=min(settings.max_albums, 200),
            )
        )

    # Targeted label searches are by far the highest-precision source for the
    # "Swedish independent label" criterion, so they only run when asked for.
    if CRITERION_LABEL in criteria:
        for name in label_search_names(reference, label_query_limit):
            sources.append(
                SourceSpec(
                    kind="label-search",
                    label=f"Label search: {name}",
                    market=settings.markets[0],
                    query=label_query(name, new_only=True),
                    limit=50,
                )
            )

    # A broad sweep catches Swedish artists and writers whose releases never
    # appear on the Swedish new-releases carousel.
    if CRITERION_ARTIST in criteria or CRITERION_WRITER in criteria:
        sources.append(
            SourceSpec(
                kind="tag-new",
                label=f"Broad new-release sweep ({settings.markets[0]}, {year})",
                market=settings.markets[0],
                query=new_release_query(year),
                limit=min(settings.max_albums, 200),
            )
        )
        for genre in ("swedish pop", "swedish indie", "swedish hip hop", "swedish metal"):
            sources.append(
                SourceSpec(
                    kind="genre-artists",
                    label=f"Artists tagged '{genre}'",
                    market=settings.markets[0],
                    query=f'genre:"{genre}"',
                    limit=20,
                )
            )
    return sources


class Pipeline:
    """Runs one discovery pass."""

    def __init__(
        self,
        settings: Settings,
        *,
        spotify: SpotifyClient,
        musicbrainz: Optional[MusicBrainzClient] = None,
        reference: Optional[Reference] = None,
        cache: Optional[Cache] = None,
        progress: ProgressFn = _noop,
        today: Optional[date] = None,
    ) -> None:
        self.settings = settings
        self.spotify = spotify
        self.musicbrainz = musicbrainz
        self.reference = reference or load_reference()
        self.cache = cache
        self.progress = progress
        self.today = today or datetime.now(timezone.utc).date()
        self.stats = RunStats()
        self._mb_budget = Budget(settings.musicbrainz_budget if musicbrainz else 0)
        self._artist_resolver = ArtistResolver(musicbrainz) if musicbrainz else None

    # ------------------------------------------------------------------
    # Stage 1: candidate collection
    # ------------------------------------------------------------------
    def collect_candidates(self, sources: Sequence[SourceSpec]) -> Dict[str, Release]:
        """Run every source and merge the simplified album objects it returns."""
        candidates: Dict[str, Release] = {}
        for source in sources:
            if len(candidates) >= self.settings.max_albums:
                self.progress(f"Album cap ({self.settings.max_albums}) reached - stopping source scan")
                break
            self.stats.sources_queried += 1
            try:
                albums = self._run_source(source)
            except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
                self.stats.errors.append(f"{source.label}: {exc}")
                self.progress(f"  ! {source.label}: {exc}")
                continue
            added = 0
            for payload in albums:
                release = Release.from_spotify(payload)
                if not release.id:
                    continue
                self.stats.albums_seen += 1
                existing = candidates.get(release.id)
                if existing is not None:
                    if source.label not in existing.sources:
                        existing.sources.append(source.label)
                    continue
                if not self._in_window(release):
                    continue
                release.sources.append(source.label)
                candidates[release.id] = release
                added += 1
                if len(candidates) >= self.settings.max_albums:
                    break
            self.progress(f"  {source.label}: {added} new candidate(s) [{len(candidates)} total]")
        self.stats.albums_in_window = len(candidates)
        return candidates

    def _run_source(self, source: SourceSpec) -> List[dict]:
        if source.kind == "new-releases":
            return self.spotify.new_releases(source.market, limit=source.limit)
        if source.kind in ("label-search", "tag-new"):
            return self.spotify.search_albums(source.query, market=source.market, limit=source.limit)
        if source.kind == "genre-artists":
            artists = self.spotify.search_artists(
                source.query, market=source.market, limit=source.limit
            )
            albums: List[dict] = []
            for artist in artists:
                artist_id = artist.get("id")
                if not isinstance(artist_id, str):
                    continue
                albums.extend(
                    self.spotify.artist_albums(artist_id, market=source.market, limit=10)
                )
            return albums
        raise ValueError(f"unknown source kind {source.kind!r}")

    def _in_window(self, release: Release) -> bool:
        age = release.age_days(self.today)
        if age is None:
            # Undated releases are rare; keep them rather than silently dropping.
            return True
        return -7 <= age <= self.settings.days

    # ------------------------------------------------------------------
    # Stage 2: Spotify enrichment
    # ------------------------------------------------------------------
    def enrich_releases(self, candidates: Dict[str, Release]) -> Tuple[Dict[str, Release], Dict[str, Artist]]:
        """Fetch full albums, artists and track ISRCs for the candidate set."""
        market = self.settings.markets[0]
        ids = list(candidates)
        self.progress(f"Fetching full album data for {len(ids)} release(s)…")
        releases: Dict[str, Release] = {}
        for payload in self.spotify.albums(ids, market=market):
            release = Release.from_spotify(payload)
            if not release.id:
                continue
            previous = candidates.get(release.id)
            if previous is not None:
                release.sources = list(previous.sources)
                if not release.release_date:
                    release.release_date = previous.release_date
                    release.release_date_raw = previous.release_date_raw
            releases[release.id] = release

        # Albums longer than 50 tracks come back paginated.
        for release in releases.values():
            if release.total_tracks > len(release.tracks) and release.total_tracks > 50:
                extra = self.spotify.album_tracks(release.id, market=market)
                known = {t.id for t in release.tracks}
                for payload in extra:
                    track = Track.from_spotify(payload)
                    if track.id and track.id not in known:
                        release.tracks.append(track)
                        known.add(track.id)

        artist_ids: Set[str] = set()
        for release in releases.values():
            artist_ids.update(release.artist_ids)
        self.progress(f"Fetching {len(artist_ids)} artist profile(s)…")
        artists: Dict[str, Artist] = {}
        for payload in self.spotify.artists(sorted(artist_ids)):
            artist = Artist.from_spotify(payload)
            if artist.id:
                artists[artist.id] = artist

        # Album track objects are "simplified" and carry no ISRC, which every
        # criterion leans on - one batched call per 50 tracks fixes that.
        track_ids = [t.id for release in releases.values() for t in release.tracks if t.id]
        if track_ids:
            self.progress(f"Fetching ISRCs for {len(track_ids)} track(s)…")
            isrc_by_id: Dict[str, str] = {}
            for payload in self.spotify.tracks(track_ids, market=market):
                track = Track.from_spotify(payload)
                if track.id and track.isrc:
                    isrc_by_id[track.id] = track.isrc
            for release in releases.values():
                for track in release.tracks:
                    if not track.isrc and track.id in isrc_by_id:
                        track.isrc = isrc_by_id[track.id]

        self.stats.albums_enriched = len(releases)
        return releases, artists

    # ------------------------------------------------------------------
    # Stage 3: MusicBrainz enrichment
    # ------------------------------------------------------------------
    def enrich_artists_from_musicbrainz(self, artists: Dict[str, Artist]) -> None:
        if self._artist_resolver is None or not artists:
            return
        self.progress(f"Resolving nationality for {len(artists)} artist(s) via MusicBrainz…")
        for index, artist in enumerate(artists.values(), start=1):
            if self._mb_budget.exhausted:
                self.stats.errors.append(
                    "MusicBrainz budget exhausted during artist lookup - "
                    "some nationalities are unresolved (raise RADAR_MUSICBRAINZ_BUDGET)"
                )
                break
            if not self._mb_budget.spend(2):  # url lookup + artist lookup
                break
            mb_artist, how = self._artist_resolver.resolve_spotify_artist(artist.id, artist.name)
            if mb_artist is not None:
                artist.mbid = mb_artist.mbid
                artist.country = mb_artist.country
                artist.area = mb_artist.area or mb_artist.begin_area
                artist.begin_area = mb_artist.begin_area
                artist.mb_resolved_by = how
            if index % 25 == 0:
                self.progress(f"  …{index}/{len(artists)} artists resolved")

    def label_context(self, release: Release) -> LabelContext:
        """Resolve a release's labels in MusicBrainz via its barcode."""
        if self.musicbrainz is None or not release.upc:
            return LabelContext()
        if self._mb_budget.remaining < 3:
            return LabelContext()
        self._mb_budget.spend(1)
        mb_labels = self.musicbrainz.labels_for_barcode(release.upc)
        resolved = []
        for label in mb_labels[:2]:
            if not self._mb_budget.spend(1):
                break
            full = self.musicbrainz.label(label.mbid)
            if full is not None:
                resolved.append(full)
        return LabelContext(mb_labels=resolved)

    def writer_context(self, release: Release) -> WriterContext:
        """Resolve songwriter credits for a sample of the release's tracks."""
        if self.musicbrainz is None or self._artist_resolver is None:
            return WriterContext(attempted=0)

        sample = [t for t in release.tracks if t.isrc][: self.settings.writer_track_sample]
        context = WriterContext(attempted=len(sample))
        if not sample:
            return context

        for track in sample:
            if self._mb_budget.remaining < 3:
                context.lookup_failed = True
                break
            if not self._mb_budget.spend(1):
                context.lookup_failed = True
                break
            work_ids = self.musicbrainz.works_for_isrc(track.isrc or "")
            entry = TrackWriters(
                track_id=track.id,
                track_name=track.name,
                isrc=track.isrc,
                work_mbids=list(work_ids),
            )
            seen_writers: Set[str] = set()
            for work_id in work_ids[:2]:
                if not self._mb_budget.spend(1):
                    context.lookup_failed = True
                    break
                work = self.musicbrainz.work(work_id)
                if work is None:
                    continue
                for credit in work.writers:
                    if credit.mbid in seen_writers:
                        continue
                    seen_writers.add(credit.mbid)
                    country = None
                    if self._mb_budget.spend(1):
                        mb_artist = self._artist_resolver.by_mbid(credit.mbid)
                        country = mb_artist.country if mb_artist else None
                    entry.writers.append(
                        writer_from_credit(credit.name, credit.mbid, sorted(credit.roles), country)
                    )
            context.tracks.append(entry)
        return context

    # ------------------------------------------------------------------
    # Stage 4: detection
    # ------------------------------------------------------------------
    def evaluate(
        self,
        release: Release,
        artists: Sequence[Artist],
        criteria: Sequence[str],
        *,
        writer_allowed: bool = True,
    ) -> Dict[str, Verdict]:
        threshold = self.settings.min_confidence
        verdicts: Dict[str, Verdict] = {}

        # Label and artist evidence feed each other, so the label verdict is
        # computed twice: once blind, once knowing whether the artist is Swedish.
        preliminary_label = detect_swedish_independent_label(
            release, artists, self.reference, threshold=threshold
        )

        if CRITERION_ARTIST in criteria or CRITERION_LABEL in criteria:
            artist_verdict = detect_swedish_artist(
                release,
                artists,
                self.reference,
                threshold=threshold,
                label_is_swedish_indie=preliminary_label.matched,
            )
        else:
            artist_verdict = None

        if CRITERION_ARTIST in criteria and artist_verdict is not None:
            verdicts[CRITERION_ARTIST] = artist_verdict

        if CRITERION_LABEL in criteria:
            context = LabelContext()
            if not preliminary_label.matched and self.musicbrainz is not None:
                context = self.label_context(release)
            verdicts[CRITERION_LABEL] = detect_swedish_independent_label(
                release,
                artists,
                self.reference,
                context=context,
                artist_is_swedish=bool(artist_verdict and artist_verdict.matched),
                threshold=threshold,
            )

        if CRITERION_WRITER in criteria:
            if writer_allowed:
                context = self.writer_context(release)
            else:
                context = WriterContext(attempted=0, lookup_failed=True)
            verdict = detect_swedish_writers(
                release,
                context,
                self.reference,
                threshold=threshold,
                majority=self.settings.writer_majority,
            )
            if not writer_allowed:
                verdict.summary = (
                    "Songwriter credits not checked - the per-run lookup budget was spent "
                    "(raise --writer-release-budget)."
                )
            verdicts[CRITERION_WRITER] = verdict

        return verdicts

    # ------------------------------------------------------------------
    def run(self, criteria: Sequence[str]) -> RunResult:
        criteria = [c for c in criteria if c in ALL_CRITERIA] or list(ALL_CRITERIA)
        self.stats.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        sources = build_sources(self.settings, criteria, self.reference, today=self.today)
        self.progress(f"Scanning {len(sources)} source(s) for releases from the last {self.settings.days} day(s)…")
        candidates = self.collect_candidates(sources)
        if not candidates:
            return self._finish([], criteria)

        releases, artists = self.enrich_releases(candidates)
        if CRITERION_ARTIST in criteria or CRITERION_LABEL in criteria:
            self.enrich_artists_from_musicbrainz(artists)

        ordered = sorted(
            releases.values(),
            key=lambda r: (r.release_date or date.min, r.id),
            reverse=True,
        )
        self.progress(f"Evaluating {len(ordered)} release(s) against {', '.join(criteria)}…")

        writer_slots = self.settings.writer_release_budget if CRITERION_WRITER in criteria else 0
        matches: List[Match] = []
        for index, release in enumerate(ordered, start=1):
            release_artists = [artists[aid] for aid in release.artist_ids if aid in artists]
            writer_allowed = writer_slots > 0 and not self._mb_budget.exhausted
            verdicts = self.evaluate(release, release_artists, criteria, writer_allowed=writer_allowed)
            if writer_allowed and CRITERION_WRITER in criteria:
                writer_slots -= 1
            matched = [name for name, verdict in verdicts.items() if verdict.matched]
            if matched:
                matches.append(
                    Match(
                        release=release,
                        artists=release_artists,
                        verdicts=verdicts,
                        score=_score(verdicts, release, self.today),
                    )
                )
            if index % 50 == 0:
                self.progress(f"  …{index}/{len(ordered)} evaluated, {len(matches)} match(es)")

        matches.sort(key=lambda m: (-m.score, m.release.name))
        return self._finish(matches, criteria)

    def _finish(self, matches: List[Match], criteria: Sequence[str]) -> RunResult:
        self.stats.matches = len(matches)
        self.stats.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.stats.spotify_requests = self.spotify.request_count
        if self.musicbrainz is not None:
            self.stats.musicbrainz_requests = self.musicbrainz.request_count
            self.stats.errors.extend(self.musicbrainz.errors[:10])
        if self.cache is not None:
            self.stats.cache_hits = self.cache.hits
            self.stats.cache_misses = self.cache.misses
        return RunResult(
            matches=matches,
            stats=self.stats,
            criteria=list(criteria),
            settings_summary={
                "markets": list(self.settings.markets),
                "days": self.settings.days,
                "max_albums": self.settings.max_albums,
                "min_confidence": self.settings.min_confidence,
                "writer_majority": self.settings.writer_majority,
                "musicbrainz": self.settings.musicbrainz_enabled,
                "today": self.today.isoformat(),
            },
        )


def _score(verdicts: Dict[str, Verdict], release: Release, today: date) -> float:
    """Rank matches: strength of evidence first, then freshness.

    Matching several criteria is worth more than matching one strongly, so the
    strongest verdict sets the base and each additional match adds a bonus.
    """
    matched = [v for v in verdicts.values() if v.matched]
    if not matched:
        return 0.0
    base = max(v.confidence for v in matched)
    bonus = 0.08 * (len(matched) - 1)
    age = release.age_days(today)
    freshness = 0.0
    if age is not None and age >= 0:
        freshness = 0.06 * max(0.0, 1.0 - age / 30.0)
    return round(min(1.2, base + bonus + freshness), 4)
