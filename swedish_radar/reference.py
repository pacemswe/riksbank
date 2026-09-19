"""Loads and indexes the curated reference data in ``swedish_radar/data``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import DATA_DIR
from .errors import ConfigError
from .textutil import label_key, loose_key, normalize


@dataclass(frozen=True)
class LabelEntry:
    """One curated label."""

    name: str
    aliases: Tuple[str, ...] = ()
    city: str = ""
    scene: str = ""
    confidence: float = 0.8
    independent: bool = True
    role: str = "label"
    note: str = ""
    country: str = "SE"

    def all_names(self) -> List[str]:
        return [self.name, *self.aliases]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "city": self.city,
            "scene": self.scene,
            "confidence": self.confidence,
            "independent": self.independent,
            "role": self.role,
            "note": self.note,
            "country": self.country,
        }


@dataclass
class LabelIndex:
    """Lookup structure over the curated label lists."""

    by_key: Dict[str, LabelEntry] = field(default_factory=dict)
    by_loose: Dict[str, LabelEntry] = field(default_factory=dict)
    entries: List[LabelEntry] = field(default_factory=list)
    false_friends: Dict[str, LabelEntry] = field(default_factory=dict)
    distributors: Dict[str, LabelEntry] = field(default_factory=dict)

    def _register(self, entry: LabelEntry) -> None:
        self.entries.append(entry)
        for name in entry.all_names():
            key = label_key(name)
            if key:
                # First registration wins so aliases never shadow a canonical name.
                self.by_key.setdefault(key, entry)
            loose = loose_key(name)
            if loose:
                self.by_loose.setdefault(loose, entry)

    def lookup(self, name: str) -> Tuple[Optional[LabelEntry], str]:
        """Resolve a raw label string.

        Returns ``(entry, how)`` where *how* is ``"exact"``, ``"loose"`` or ``""``.
        """
        key = label_key(name)
        if key and key in self.by_key:
            return self.by_key[key], "exact"
        loose = loose_key(name)
        # A loose key must still be distinctive - "Music AB" collapses to ""
        # and must not match every label whose loose key is also empty.
        if loose and len(loose) >= 3 and loose in self.by_loose:
            return self.by_loose[loose], "loose"
        return None, ""

    def lookup_false_friend(self, name: str) -> Optional[LabelEntry]:
        key = label_key(name)
        if key in self.false_friends:
            return self.false_friends[key]
        loose = loose_key(name)
        if loose and len(loose) >= 3 and loose in self.false_friends:
            return self.false_friends[loose]
        return None

    def lookup_distributor(self, name: str) -> Optional[LabelEntry]:
        key = label_key(name)
        if key in self.distributors:
            return self.distributors[key]
        loose = loose_key(name)
        if loose and len(loose) >= 3 and loose in self.distributors:
            return self.distributors[loose]
        return None


@dataclass(frozen=True)
class Markers:
    """Language and genre heuristics."""

    genre_prefixes: Tuple[str, ...]
    sweden_specific_genres: Tuple[str, ...]
    nordic_ambiguous_genres: Tuple[str, ...]
    swedish_cities: Tuple[str, ...]
    swedish_words: Tuple[str, ...]
    danish_norwegian_words: Tuple[str, ...]
    finnish_words: Tuple[str, ...]
    isrc_country_swedish: str = "SE"
    isrc_country_nordic: Tuple[str, ...] = ("NO", "DK", "FI", "IS")


@dataclass(frozen=True)
class Majors:
    """Major-label and aggregator markers."""

    groups: Dict[str, Tuple[str, ...]]
    aggregators: Tuple[str, ...]
    distribution_phrases: Tuple[str, ...]

    def group_for(self, name: str) -> Optional[str]:
        """Return the major group whose marker matches ``name``, if any."""
        normalized = f" {normalize(name)} "
        best: Optional[str] = None
        best_len = 0
        for group, markers in self.groups.items():
            for marker in markers:
                norm_marker = normalize(marker)
                if norm_marker and f" {norm_marker} " in normalized and len(norm_marker) > best_len:
                    best, best_len = group, len(norm_marker)
        return best

    def aggregator_for(self, name: str) -> Optional[str]:
        normalized = f" {normalize(name)} "
        for marker in self.aggregators:
            norm_marker = normalize(marker)
            if norm_marker and f" {norm_marker} " in normalized:
                return marker
        return None


@dataclass(frozen=True)
class Reference:
    """The complete reference bundle used by the detectors."""

    labels: LabelIndex
    majors: Majors
    markers: Markers


def _load_json(path: Path) -> Dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"reference data file missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"reference data file {path} is not valid JSON: {exc}") from exc


def _entry_from_dict(payload: Dict[str, object], *, default_country: str = "SE") -> LabelEntry:
    aliases = payload.get("aliases") or []
    return LabelEntry(
        name=str(payload.get("name", "")),
        aliases=tuple(str(a) for a in aliases if isinstance(a, str)),
        city=str(payload.get("city", "")),
        scene=str(payload.get("scene", "")),
        confidence=float(payload.get("confidence", 0.8)),
        independent=bool(payload.get("independent", True)),
        role=str(payload.get("role", "label")),
        note=str(payload.get("note", "")),
        country=str(payload.get("country", default_country)),
    )


def build_label_index(payload: Dict[str, object]) -> LabelIndex:
    index = LabelIndex()
    for raw in payload.get("labels", []) or []:
        if isinstance(raw, dict):
            entry = _entry_from_dict(raw)
            if entry.name:
                index._register(entry)
    for raw in payload.get("false_friends", []) or []:
        if isinstance(raw, dict):
            entry = _entry_from_dict(raw, default_country="")
            for name in entry.all_names():
                if label_key(name):
                    index.false_friends.setdefault(label_key(name), entry)
                if loose_key(name):
                    index.false_friends.setdefault(loose_key(name), entry)
    for raw in payload.get("swedish_distributors", []) or []:
        if isinstance(raw, dict):
            entry = _entry_from_dict(raw)
            for name in entry.all_names():
                if label_key(name):
                    index.distributors.setdefault(label_key(name), entry)
                if loose_key(name):
                    index.distributors.setdefault(loose_key(name), entry)
    return index


@lru_cache(maxsize=4)
def load_reference(data_dir: Optional[Path] = None) -> Reference:
    """Load (and memoise) the reference bundle."""
    directory = data_dir or DATA_DIR
    labels_payload = _load_json(directory / "swedish_labels.json")
    majors_payload = _load_json(directory / "majors.json")
    markers_payload = _load_json(directory / "markers.json")

    groups_raw = majors_payload.get("groups", {})
    groups: Dict[str, Tuple[str, ...]] = {}
    if isinstance(groups_raw, dict):
        for group, markers in groups_raw.items():
            if isinstance(markers, list):
                groups[group] = tuple(str(m) for m in markers)

    def _tuple(key: str, payload: Dict[str, object]) -> Tuple[str, ...]:
        value = payload.get(key, [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    majors = Majors(
        groups=groups,
        aggregators=_tuple("aggregators", majors_payload),
        distribution_phrases=_tuple("major_distribution_phrases", majors_payload),
    )
    markers = Markers(
        genre_prefixes=_tuple("genre_prefixes", markers_payload),
        sweden_specific_genres=_tuple("sweden_specific_genres", markers_payload),
        nordic_ambiguous_genres=_tuple("nordic_ambiguous_genres", markers_payload),
        swedish_cities=_tuple("swedish_cities", markers_payload),
        swedish_words=_tuple("swedish_distinctive_words", markers_payload),
        danish_norwegian_words=_tuple("danish_norwegian_words", markers_payload),
        finnish_words=_tuple("finnish_words", markers_payload),
        isrc_country_swedish=str(markers_payload.get("isrc_country_swedish", "SE")),
        isrc_country_nordic=_tuple("isrc_country_nordic", markers_payload) or ("NO", "DK", "FI", "IS"),
    )
    return Reference(labels=build_label_index(labels_payload), majors=majors, markers=markers)


def label_search_names(reference: Reference, limit: Optional[int] = None) -> List[str]:
    """Canonical label names worth querying Spotify's ``label:`` search filter with.

    Ordered by confidence so a capped run spends its budget on the labels we
    are most sure about.
    """
    entries: Sequence[LabelEntry] = sorted(
        (e for e in reference.labels.entries if e.independent),
        key=lambda e: (-e.confidence, e.name),
    )
    names = [entry.name for entry in entries]
    return names[:limit] if limit else names
