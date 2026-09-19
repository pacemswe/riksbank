"""Rendering a :class:`~swedish_radar.models.RunResult` for humans and machines."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import List, Optional

from .models import RunResult, Verdict

CRITERION_LABELS = {
    "artist": "Swedish artist",
    "label": "Swedish independent label",
    "writer": "Swedish songwriters",
}


def to_json(result: RunResult, *, today: Optional[date] = None, indent: int = 2) -> str:
    return json.dumps(result.to_dict(today), ensure_ascii=False, indent=indent)


def to_csv(result: RunResult, *, today: Optional[date] = None) -> str:
    """One row per release, with the evidence flattened into readable columns."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "release_date",
            "artist",
            "title",
            "type",
            "label",
            "matched_criteria",
            "score",
            "artist_confidence",
            "label_confidence",
            "writer_confidence",
            "swedish_writers",
            "why",
            "spotify_url",
        ]
    )
    for match in result.matches:
        release = match.release
        verdicts = match.verdicts
        writer_verdict = verdicts.get("writer")
        swedish_writers = (
            ", ".join(writer_verdict.data.get("swedish_writers", [])) if writer_verdict else ""
        )
        writer.writerow(
            [
                release.release_date.isoformat() if release.release_date else release.release_date_raw,
                ", ".join(release.artist_names),
                release.name,
                release.album_type,
                release.label,
                "; ".join(CRITERION_LABELS.get(c, c) for c in match.matched_criteria),
                f"{match.score:.3f}",
                _confidence(verdicts.get("artist")),
                _confidence(verdicts.get("label")),
                _confidence(verdicts.get("writer")),
                swedish_writers,
                " | ".join(
                    verdicts[c].summary for c in match.matched_criteria if c in verdicts
                ),
                release.spotify_url,
            ]
        )
    return buffer.getvalue()


def _confidence(verdict: Optional[Verdict]) -> str:
    return f"{verdict.confidence:.3f}" if verdict is not None else ""


def to_markdown(result: RunResult, *, today: Optional[date] = None, max_signals: int = 3) -> str:
    """A shareable digest - this is what the scheduled workflow commits."""
    lines: List[str] = []
    criteria = ", ".join(CRITERION_LABELS.get(c, c) for c in result.criteria)
    settings = result.settings_summary
    lines.append("# Swedish Radar")
    lines.append("")
    lines.append(
        f"**{len(result.matches)} release(s)** from the last {settings.get('days', '?')} days "
        f"matching: {criteria}."
    )
    lines.append("")
    lines.append(
        f"_Generated {result.stats.finished_at or '—'} · markets "
        f"{', '.join(settings.get('markets', []))} · "
        f"min confidence {settings.get('min_confidence', '?')}_"
    )
    lines.append("")

    if not result.matches:
        lines.append("No matching releases found in this window.")
        lines.append("")
        return "\n".join(lines) + _stats_block(result)

    for match in result.matches:
        release = match.release
        artists = ", ".join(release.artist_names) or "Unknown artist"
        date_text = release.release_date.isoformat() if release.release_date else release.release_date_raw
        badges = " ".join(f"`{CRITERION_LABELS.get(c, c)}`" for c in match.matched_criteria)
        lines.append(f"## {artists} — [{release.name}]({release.spotify_url})")
        lines.append("")
        lines.append(f"{date_text} · {release.album_type} · {release.total_tracks} track(s) · {badges}")
        if release.label:
            lines.append("")
            lines.append(f"Label: **{release.label}**")
        lines.append("")
        for criterion in match.matched_criteria:
            verdict = match.verdicts[criterion]
            lines.append(
                f"- **{CRITERION_LABELS.get(criterion, criterion)}** "
                f"({verdict.confidence:.2f}): {verdict.summary}"
            )
            for signal in verdict.signals[:max_signals]:
                sign = "+" if signal.weight > 0 else "−"
                lines.append(f"  - {sign} {signal.detail} _({signal.source})_")
        lines.append("")

    return "\n".join(lines) + _stats_block(result)


def _stats_block(result: RunResult) -> str:
    stats = result.stats
    lines = [
        "---",
        "",
        "<details><summary>Run statistics</summary>",
        "",
        f"- Sources queried: {stats.sources_queried}",
        f"- Albums seen: {stats.albums_seen} (in window: {stats.albums_in_window})",
        f"- Albums enriched: {stats.albums_enriched}",
        f"- Spotify requests: {stats.spotify_requests}",
        f"- MusicBrainz requests: {stats.musicbrainz_requests}",
        f"- Cache: {stats.cache_hits} hit(s), {stats.cache_misses} miss(es)",
    ]
    if stats.errors:
        lines.append("- Warnings:")
        lines.extend(f"  - {error}" for error in stats.errors[:10])
    lines.extend(["", "</details>", ""])
    return "\n".join(lines)


def to_text(result: RunResult, *, today: Optional[date] = None, width: int = 100) -> str:
    """Compact terminal output."""
    lines: List[str] = []
    if not result.matches:
        return "No matching releases found.\n"
    for match in result.matches:
        release = match.release
        artists = ", ".join(release.artist_names) or "Unknown artist"
        date_text = release.release_date.isoformat() if release.release_date else "????-??-??"
        badges = ",".join(c[0].upper() for c in match.matched_criteria)
        head = f"{date_text}  [{badges}] {artists} — {release.name}"
        lines.append(head[:width])
        if release.label:
            lines.append(f"            label: {release.label}")
        for criterion in match.matched_criteria:
            verdict = match.verdicts[criterion]
            lines.append(
                f"            {CRITERION_LABELS.get(criterion, criterion)} "
                f"({verdict.confidence:.2f}): {verdict.summary}"
            )
        lines.append(f"            {release.spotify_url}")
        lines.append("")
    lines.append(
        f"{len(result.matches)} match(es) · "
        f"{result.stats.albums_in_window} release(s) examined · "
        f"{result.stats.spotify_requests} Spotify + {result.stats.musicbrainz_requests} MusicBrainz request(s)"
    )
    for error in result.stats.errors[:5]:
        lines.append(f"  warning: {error}")
    return "\n".join(lines) + "\n"


RENDERERS = {
    "json": to_json,
    "csv": to_csv,
    "markdown": to_markdown,
    "md": to_markdown,
    "text": to_text,
}


def render(result: RunResult, fmt: str, *, today: Optional[date] = None) -> str:
    renderer = RENDERERS.get(fmt.lower())
    if renderer is None:
        raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(sorted(RENDERERS))}")
    return renderer(result, today=today)
