"""Unit tests for the three criteria detectors."""

from __future__ import annotations

import unittest

from swedish_radar.clients.musicbrainz import MBLabel
from swedish_radar.detect.artist import detect_swedish_artist
from swedish_radar.detect.common import analyse_genres, analyse_isrcs, analyse_language, combine
from swedish_radar.detect.label import LabelContext, detect_swedish_independent_label, label_candidates
from swedish_radar.detect.writer import TrackWriters, WriterContext, detect_swedish_writers
from swedish_radar.models import Artist, Release, Signal, Track, Writer
from swedish_radar.reference import load_reference

REFERENCE = load_reference()


def release(**kwargs) -> Release:
    tracks = kwargs.pop("tracks", [])
    rel = Release(id="rel", name=kwargs.pop("name", "Album"), **kwargs)
    rel.tracks = [
        Track(id=f"t{i}", name=name, isrc=isrc) for i, (name, isrc) in enumerate(tracks)
    ]
    return rel


def writer(name: str, country):
    return Writer(
        name=name,
        mbid=name,
        roles=["composer"],
        country=country,
        is_swedish=(country == "SE") if country else None,
    )


class CombineTests(unittest.TestCase):
    def test_weak_signals_reinforce_without_exceeding_one(self):
        score = combine([Signal("a", "", 0.4), Signal("b", "", 0.4), Signal("c", "", 0.4)])
        self.assertAlmostEqual(0.784, score, places=3)
        self.assertLessEqual(score, 1.0)

    def test_negative_signals_discount(self):
        self.assertAlmostEqual(0.45, combine([Signal("a", "", 0.9), Signal("n", "", -0.5)]), places=3)

    def test_no_signals_is_zero(self):
        self.assertEqual(0.0, combine([]))

    def test_single_weak_signal_stays_below_default_threshold(self):
        self.assertLess(combine([Signal("a", "", 0.3)]), 0.55)


class LanguageTests(unittest.TestCase):
    def test_swedish_words_are_detected(self):
        hint = analyse_language(["Jag älskar dej och hjärtat"], REFERENCE.markers)
        self.assertIn("alskar", hint.swedish_words)
        self.assertGreater(hint.score, 0.4)

    def test_danish_is_not_mistaken_for_swedish(self):
        hint = analyse_language(["Jeg elsker dig og hjertet"], REFERENCE.markers)
        self.assertEqual((), hint.swedish_words)
        self.assertEqual(0.0, hint.score)

    def test_norwegian_letters_suppress_the_hint(self):
        hint = analyse_language(["Kjærlighet på øya"], REFERENCE.markers)
        self.assertEqual(0.0, hint.score)

    def test_finnish_is_not_mistaken_for_swedish(self):
        hint = analyse_language(["Minä rakastan sinua ja sydän"], REFERENCE.markers)
        self.assertEqual(0.0, hint.score)

    def test_bare_umlauts_are_only_a_faint_hint(self):
        hint = analyse_language(["Ödeläggelse"], REFERENCE.markers)
        self.assertGreater(hint.score, 0.0)
        self.assertLess(hint.score, 0.3)


class GenreTests(unittest.TestCase):
    def test_explicit_swedish_genre_scores_highest(self):
        self.assertEqual(0.8, analyse_genres(["swedish indie pop"], REFERENCE.markers).score)

    def test_sweden_specific_genre_without_the_word(self):
        self.assertEqual(0.7, analyse_genres(["dansband"], REFERENCE.markers).score)

    def test_nordic_genres_are_only_weak(self):
        self.assertEqual(0.15, analyse_genres(["nordic folk"], REFERENCE.markers).score)

    def test_unrelated_genres_score_nothing(self):
        self.assertEqual(0.0, analyse_genres(["uk garage", "bossa nova"], REFERENCE.markers).score)


class IsrcTests(unittest.TestCase):
    def test_counts_by_country_prefix(self):
        hint = analyse_isrcs(["SEAAA2600001", "SEAAA2600002", "USABC2600003"], REFERENCE.markers)
        self.assertEqual(2, hint.swedish)
        self.assertEqual(1, hint.other)
        self.assertAlmostEqual(2 / 3, hint.swedish_share)

    def test_empty_is_safe(self):
        hint = analyse_isrcs([], REFERENCE.markers)
        self.assertEqual(0.0, hint.swedish_share)


class LabelDetectorTests(unittest.TestCase):
    def test_curated_label_matches(self):
        verdict = detect_swedish_independent_label(
            release(label="Adrian Recordings"), [Artist(id="a", name="X")], REFERENCE
        )
        self.assertTrue(verdict.matched)
        self.assertEqual("Adrian Recordings", verdict.data["matched_label"]["name"])

    def test_alias_and_case_insensitive_matching(self):
        for name in ("höga nord rekords", "Hoga Nord Rekords", "HÖGA NORD"):
            verdict = detect_swedish_independent_label(
                release(label=name), [Artist(id="a", name="X")], REFERENCE
            )
            self.assertTrue(verdict.matched, name)

    def test_major_label_never_matches(self):
        for name in ("Universal Music AB", "Sony Music Entertainment", "Warner Music Sweden"):
            verdict = detect_swedish_independent_label(
                release(label=name), [Artist(id="a", name="X")], REFERENCE
            )
            self.assertFalse(verdict.matched, name)

    def test_indie_licensed_to_a_major_still_matches_but_is_discounted(self):
        plain = detect_swedish_independent_label(
            release(label="Rama Lama Records"), [Artist(id="a", name="X")], REFERENCE
        )
        licensed = detect_swedish_independent_label(
            release(label="Rama Lama Records under exclusive license to Sony Music"),
            [Artist(id="a", name="X")],
            REFERENCE,
        )
        self.assertTrue(plain.matched)
        self.assertTrue(licensed.matched)
        self.assertLess(licensed.confidence, plain.confidence)

    def test_foreign_label_with_a_nordic_name_is_rejected(self):
        for name in ("Svart Records", "Fysisk Format", "Fika Recordings", "Posh Isolation"):
            verdict = detect_swedish_independent_label(
                release(label=name), [Artist(id="a", name="X")], REFERENCE
            )
            self.assertFalse(verdict.matched, name)

    def test_swedish_but_major_owned_label_is_rejected(self):
        verdict = detect_swedish_independent_label(
            release(label="BIS Records"), [Artist(id="a", name="X")], REFERENCE
        )
        self.assertFalse(verdict.matched)

    def test_label_from_copyright_line(self):
        verdict = detect_swedish_independent_label(
            release(label="", copyrights=["℗ 2026 Höga Nord Rekords"]),
            [Artist(id="a", name="X")],
            REFERENCE,
        )
        self.assertTrue(verdict.matched)

    def test_heuristics_alone_never_claim_a_label(self):
        verdict = detect_swedish_independent_label(
            release(
                label="Okänt Skivbolag AB",
                tracks=[("A", "SEAAA2600001"), ("B", "SEAAA2600002")],
            ),
            [Artist(id="a", name="X")],
            REFERENCE,
        )
        self.assertFalse(verdict.matched, "a Swedish-looking name is not proof of a label")
        self.assertGreater(verdict.confidence, 0.0, "but it should still register as evidence")

    def test_musicbrainz_can_confirm_an_uncurated_label(self):
        context = LabelContext(mb_labels=[MBLabel(mbid="m", name="Okänt Skivbolag", country="SE")])
        verdict = detect_swedish_independent_label(
            release(label="Okänt Skivbolag"), [Artist(id="a", name="X")], REFERENCE, context=context
        )
        self.assertTrue(verdict.matched)

    def test_self_release_requires_a_swedish_artist(self):
        rel = release(label="Klara Ström", tracks=[("A", "SEAAA2600001")])
        rel.artist_names = ["Klara Ström"]
        artists = [Artist(id="a", name="Klara Ström")]
        self.assertFalse(
            detect_swedish_independent_label(rel, artists, REFERENCE, artist_is_swedish=False).matched
        )
        self.assertTrue(
            detect_swedish_independent_label(rel, artists, REFERENCE, artist_is_swedish=True).matched
        )

    def test_diy_aggregator_is_not_a_label(self):
        verdict = detect_swedish_independent_label(
            release(label="DistroKid", tracks=[("A", "SEAAA2600001")]),
            [Artist(id="a", name="X")],
            REFERENCE,
        )
        self.assertFalse(verdict.matched)
        self.assertEqual("distrokid", verdict.data["aggregator"])

    def test_missing_label_is_reported_not_guessed(self):
        verdict = detect_swedish_independent_label(
            release(label=""), [Artist(id="a", name="X")], REFERENCE
        )
        self.assertFalse(verdict.matched)
        self.assertEqual(0.0, verdict.confidence)

    def test_label_candidates_split_compound_credits(self):
        rel = release(label="Rama Lama Records / Playground Music Scandinavia")
        self.assertEqual(
            ["Rama Lama Records", "Playground Music Scandinavia"], label_candidates(rel)
        )


class ArtistDetectorTests(unittest.TestCase):
    def test_musicbrainz_country_is_decisive(self):
        artist = Artist(id="a", name="Kite", country="SE", mb_resolved_by="spotify-url")
        verdict = detect_swedish_artist(release(), [artist], REFERENCE)
        self.assertTrue(verdict.matched)
        self.assertEqual(["Kite"], verdict.data["verified_swedish"])

    def test_foreign_country_beats_supporting_signals(self):
        artist = Artist(id="a", name="Someone", country="US", mb_resolved_by="spotify-url")
        verdict = detect_swedish_artist(
            release(tracks=[("A", "SEAAA2600001")]),
            [artist],
            REFERENCE,
            label_is_swedish_indie=True,
        )
        self.assertFalse(verdict.matched)

    def test_name_search_resolution_is_trusted_less(self):
        strong = detect_swedish_artist(
            release(), [Artist(id="a", name="X", country="SE", mb_resolved_by="spotify-url")], REFERENCE
        )
        weak = detect_swedish_artist(
            release(), [Artist(id="a", name="X", country="SE", mb_resolved_by="name-search")], REFERENCE
        )
        self.assertLess(weak.confidence, strong.confidence)

    def test_collaboration_matches_on_the_swedish_member(self):
        artists = [
            Artist(id="a", name="Foreign", country="US", mb_resolved_by="spotify-url"),
            Artist(id="b", name="Svensk", country="SE", mb_resolved_by="spotify-url"),
        ]
        verdict = detect_swedish_artist(release(), artists, REFERENCE)
        self.assertTrue(verdict.matched)
        self.assertEqual(["Svensk"], verdict.data["verified_swedish"])

    def test_no_evidence_means_no_match(self):
        verdict = detect_swedish_artist(release(), [Artist(id="a", name="Generic")], REFERENCE)
        self.assertFalse(verdict.matched)
        self.assertEqual(0.0, verdict.confidence)

    def test_no_artists_is_handled(self):
        verdict = detect_swedish_artist(release(), [], REFERENCE)
        self.assertFalse(verdict.matched)
        self.assertEqual([], verdict.data["artists"])


class WriterDetectorTests(unittest.TestCase):
    def test_all_swedish_writers_match(self):
        context = WriterContext(
            tracks=[
                TrackWriters("t0", "A", "SE1", [writer("Max", "SE"), writer("Shellback", "SE")]),
                TrackWriters("t1", "B", "SE2", [writer("Max", "SE")]),
            ],
            attempted=2,
        )
        verdict = detect_swedish_writers(release(), context, REFERENCE)
        self.assertTrue(verdict.matched)
        self.assertEqual(["Max", "Shellback"], verdict.data["swedish_writers"])

    def test_minority_swedish_writers_do_not_match(self):
        context = WriterContext(
            tracks=[TrackWriters("t0", "A", "SE1", [writer("A", "SE"), writer("B", "US"), writer("C", "GB")])],
            attempted=1,
        )
        self.assertFalse(detect_swedish_writers(release(), context, REFERENCE).matched)

    def test_unknown_countries_are_never_assumed_swedish(self):
        context = WriterContext(
            tracks=[TrackWriters("t0", "A", "SE1", [writer("A", None), writer("B", None)])],
            attempted=1,
        )
        verdict = detect_swedish_writers(release(), context, REFERENCE)
        self.assertFalse(verdict.matched)
        self.assertEqual(0, verdict.data["tracks_resolved"])

    def test_missing_credits_are_reported_as_missing(self):
        verdict = detect_swedish_writers(release(), WriterContext(attempted=3), REFERENCE)
        self.assertFalse(verdict.matched)
        self.assertIn("no songwriter credits", verdict.summary)

    def test_majority_threshold_is_configurable(self):
        context = WriterContext(
            tracks=[TrackWriters("t0", "A", "SE1", [writer("A", "SE"), writer("B", "US")])],
            attempted=1,
        )
        lenient = detect_swedish_writers(release(), context, REFERENCE, majority=0.5)
        strict = detect_swedish_writers(release(), context, REFERENCE, majority=0.9)
        self.assertTrue(lenient.matched)
        self.assertFalse(strict.matched)

    def test_low_coverage_lowers_confidence(self):
        full = WriterContext(
            tracks=[TrackWriters("t0", "A", "SE1", [writer("A", "SE")])], attempted=1
        )
        partial = WriterContext(
            tracks=[TrackWriters("t0", "A", "SE1", [writer("A", "SE")])], attempted=10
        )
        self.assertGreater(
            detect_swedish_writers(release(), full, REFERENCE).confidence,
            detect_swedish_writers(release(), partial, REFERENCE).confidence,
        )


if __name__ == "__main__":
    unittest.main()
