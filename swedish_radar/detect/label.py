"""Criterion 2 - released by a Swedish independent record label."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..clients.musicbrainz import MBLabel
from ..config import CRITERION_LABEL
from ..models import Artist, Release, Signal, Verdict
from ..reference import LabelEntry, Reference
from ..textutil import (
    has_swedish_letters,
    is_swedish_company_form,
    label_key,
    normalize,
    split_label_string,
)
from .common import analyse_isrcs, combine

#: "℗ 2026 Adrian Recordings" / "(C) 2025 Rama Lama Records"
_COPYRIGHT_PREFIX = re.compile(
    r"^\s*(?:[℗©(p)(c)]+\s*)?(?:\(\s*[pc]\s*\)\s*)?(?:19|20)\d{2}\s*", re.IGNORECASE
)


@dataclass
class LabelContext:
    """Optional MusicBrainz enrichment for one release."""

    mb_labels: Sequence[MBLabel] = field(default_factory=tuple)


def label_candidates(release: Release) -> List[str]:
    """All imprint strings worth inspecting, most authoritative first.

    ``album.label`` is the primary credit; the ℗/© lines often name the same
    company, but sometimes name the real imprint when ``label`` holds only a
    distributor.
    """
    candidates: List[str] = []
    seen = set()

    def _add(value: str) -> None:
        cleaned = value.strip()
        key = label_key(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            candidates.append(cleaned)

    for imprint in split_label_string(release.label):
        _add(imprint)
    for line in release.copyrights:
        stripped = _COPYRIGHT_PREFIX.sub("", line).strip()
        # Drop a leading bare year that survived (e.g. "2026 Hybris").
        stripped = re.sub(r"^(?:19|20)\d{2}\s+", "", stripped)
        for imprint in split_label_string(stripped):
            _add(imprint)
    return candidates


def _artist_name_keys(artists: Sequence[Artist], release: Release) -> set:
    keys = {label_key(a.name) for a in artists if a.name}
    keys |= {label_key(name) for name in release.artist_names if name}
    return {k for k in keys if k}


def detect_swedish_independent_label(
    release: Release,
    artists: Sequence[Artist],
    reference: Reference,
    *,
    context: Optional[LabelContext] = None,
    artist_is_swedish: Optional[bool] = None,
    threshold: float = 0.55,
) -> Verdict:
    """Decide whether ``release`` came out on a Swedish independent label."""
    context = context or LabelContext()
    signals: List[Signal] = []
    data: Dict[str, Any] = {
        "label_raw": release.label,
        "imprints": [],
        "matched_label": None,
        "major_group": None,
        "aggregator": None,
        "self_released": False,
    }

    candidates = label_candidates(release)
    data["imprints"] = candidates

    if not candidates:
        return Verdict(
            criterion=CRITERION_LABEL,
            matched=False,
            confidence=0.0,
            signals=[],
            summary="No label credit on the release.",
            data=data,
        )

    major_group: Optional[str] = None
    aggregator: Optional[str] = None
    best_entry: Optional[LabelEntry] = None
    best_entry_weight = 0.0
    best_match_kind = ""
    foreign_hits: List[str] = []
    heuristic_hits: List[str] = []
    mb_hits: List[str] = []
    swedish_distributor: Optional[str] = None

    mb_by_key = {label_key(lbl.name): lbl for lbl in context.mb_labels if lbl.name}

    for imprint in candidates:
        group = reference.majors.group_for(imprint)
        if group and major_group is None:
            major_group = group

        agg = reference.majors.aggregator_for(imprint)
        if agg and aggregator is None:
            aggregator = agg

        friend = reference.labels.lookup_false_friend(imprint)
        if friend is not None:
            foreign_hits.append(f"{imprint} ({friend.country or 'non-SE'})")
            continue

        distributor = reference.labels.lookup_distributor(imprint)
        if distributor is not None and swedish_distributor is None:
            swedish_distributor = distributor.name
            continue

        entry, how = reference.labels.lookup(imprint)
        if entry is not None and entry.independent:
            weight = entry.confidence if how == "exact" else entry.confidence * 0.8
            if weight > best_entry_weight:
                best_entry, best_entry_weight, best_match_kind = entry, weight, how
        elif entry is not None and not entry.independent:
            # Swedish, but knowingly major-owned: an explicit disqualifier.
            foreign_hits.append(f"{entry.name} (not independent: {entry.note or 'major-owned'})")
            continue

        mb_label = mb_by_key.get(label_key(imprint))
        if mb_label is not None and mb_label.is_swedish:
            mb_hits.append(f"{mb_label.name} registered in Sweden (MusicBrainz)")

        if entry is None and not group and not agg:
            if is_swedish_company_form(imprint):
                heuristic_hits.append(f"{imprint} is a Swedish company form (AB/HB/KB)")
            elif has_swedish_letters(imprint):
                heuristic_hits.append(f"{imprint} uses å/ä/ö")
            else:
                normalized = normalize(imprint)
                city = next(
                    (c for c in reference.markers.swedish_cities if f" {c} " in f" {normalized} "),
                    None,
                )
                if city:
                    heuristic_hits.append(f"{imprint} names a Swedish place ({city})")

    data["major_group"] = major_group
    data["aggregator"] = aggregator

    # --- self-release -----------------------------------------------------
    artist_keys = _artist_name_keys(artists, release)
    self_released = any(label_key(c) in artist_keys for c in candidates)
    data["self_released"] = self_released

    # --- assemble signals -------------------------------------------------
    if best_entry is not None:
        data["matched_label"] = best_entry.to_dict()
        data["matched_label"]["match"] = best_match_kind
        where = f" ({best_entry.city})" if best_entry.city else ""
        signals.append(
            Signal(
                "curated_label",
                f"'{best_entry.name}'{where} is on the curated Swedish independent list"
                + (" (fuzzy name match)" if best_match_kind == "loose" else ""),
                round(best_entry_weight, 3),
                source="reference",
            )
        )

    for hit in mb_hits:
        signals.append(Signal("mb_label_country", hit, 0.75, source="musicbrainz"))

    for hit in heuristic_hits[:3]:
        signals.append(Signal("label_name_hint", hit, 0.3, source="heuristic"))

    if swedish_distributor:
        signals.append(
            Signal(
                "swedish_distributor",
                f"Credited to {swedish_distributor}, a Swedish distributor - a Swedish release, "
                "but a distribution credit is not a label",
                0.2,
                source="reference",
            )
        )

    isrc_hint = analyse_isrcs(release.isrcs, reference.markers)
    if isrc_hint.total and isrc_hint.swedish_share >= 0.5:
        signals.append(
            Signal(
                "isrc_se",
                isrc_hint.detail + " - the recordings were registered by a Swedish rights holder",
                0.45,
                source="spotify",
            )
        )

    if self_released and artist_is_swedish:
        signals.append(
            Signal(
                "self_released_swedish",
                "Self-released: the label credit matches the artist name, and the artist is Swedish",
                0.5,
                source="derived",
            )
        )
    elif self_released:
        signals.append(
            Signal(
                "self_released",
                "Self-released: the label credit matches the artist name",
                0.1,
                source="derived",
            )
        )

    # --- counter-signals --------------------------------------------------
    if major_group is not None:
        # A curated indie credited *alongside* a major is a licensing or
        # distribution deal, not ownership: the release is still that indie's.
        # It is discounted, not rejected.  A bare major credit is fatal.
        weight = -0.35 if best_entry is not None else -0.95
        signals.append(
            Signal(
                "major_label",
                f"Credited to {major_group}"
                + (" alongside the indie imprint (licensing/distribution)" if best_entry else ""),
                weight,
                source="reference",
            )
        )
    for hit in foreign_hits[:3]:
        signals.append(Signal("non_swedish_label", f"{hit} is not a Swedish independent", -0.8, source="reference"))
    if aggregator is not None and best_entry is None:
        signals.append(
            Signal(
                "aggregator",
                f"Label credit is the DIY distributor '{aggregator}', which carries no label information",
                -0.35,
                source="reference",
            )
        )

    confidence = combine(signals)
    # A purely heuristic score must not claim a label match on its own; it can
    # only support one.  Naming an actual label - curated or Sweden-registered
    # in MusicBrainz - is required, with self-released Swedish artists as the
    # one exception (flagged as such in the summary).
    named_a_label = best_entry is not None or bool(mb_hits)
    matched = confidence >= threshold and (
        named_a_label or (self_released and bool(artist_is_swedish))
    )

    summary = _summarise(best_entry, mb_hits, major_group, self_released, matched, candidates)
    return Verdict(
        criterion=CRITERION_LABEL,
        matched=matched,
        confidence=round(confidence, 3),
        signals=signals,
        summary=summary,
        data=data,
    )


def _summarise(
    entry: Optional[LabelEntry],
    mb_hits: Sequence[str],
    major_group: Optional[str],
    self_released: bool,
    matched: bool,
    candidates: Sequence[str],
) -> str:
    if matched and entry is not None:
        where = f", {entry.city}" if entry.city else ""
        scene = f" - {entry.scene}" if entry.scene else ""
        return f"Released on {entry.name} (Swedish independent{where}){scene}."
    if matched and mb_hits:
        return f"Released on {candidates[0]}, a Sweden-registered label (MusicBrainz)."
    if matched and self_released:
        return f"Self-released by a Swedish artist as '{candidates[0]}'."
    if major_group:
        return f"Released on {major_group} - a major, not an independent."
    if candidates:
        return (
            f"Label '{candidates[0]}' is not a known Swedish independent "
            "(add it with `swedish-radar labels add` if it is)."
        )
    return "No label credit."
