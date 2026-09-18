"""End-to-end pipeline tests against the in-process fakes."""

from __future__ import annotations

import unittest
from pathlib import Path

from swedish_radar.cache import Cache
from swedish_radar.clients.musicbrainz import MusicBrainzClient
from swedish_radar.clients.spotify import SpotifyClient
from swedish_radar.config import ALL_CRITERIA, Settings
from swedish_radar.pipeline import Pipeline, build_sources
from swedish_radar.reference import load_reference
from tests.catalogue import TODAY, build
from tests.fakes import RoutingTransport


def make_pipeline(**overrides):
    spotify_fake, mb_fake = build()
    transport = RoutingTransport(spotify_fake, mb_fake)
    settings = Settings(
        spotify_client_id="id",
        spotify_client_secret="secret",
        cache_path=Path(":memory:"),
        cache_enabled=False,
        days=14,
        **overrides,
    )
    cache = Cache(Path(":memory:"), enabled=False)
    spotify = SpotifyClient(settings, transport=transport, cache=cache)
    musicbrainz = MusicBrainzClient(settings, transport=transport, cache=cache, sleep=lambda _s: None)
    pipeline = Pipeline(
        settings,
        spotify=spotify,
        musicbrainz=musicbrainz,
        reference=load_reference(),
        cache=cache,
        today=TODAY,
    )
    return pipeline, spotify_fake, mb_fake, transport


class PipelineTests(unittest.TestCase):
    def test_full_run_matches_expected_releases(self):
        pipeline, *_ = make_pipeline()
        result = pipeline.run(list(ALL_CRITERIA))
        by_id = {m.release.id: m for m in result.matches}

        self.assertIn("alb_a", by_id, "Swedish artist on a Swedish indie should match")
        self.assertEqual(
            {"artist", "label"}, set(by_id["alb_a"].matched_criteria)
        )

        self.assertNotIn("alb_b", by_id, "US artist on a major must not match")
        self.assertNotIn("alb_c", by_id, "Norwegian artist on a Norwegian label must not match")

        self.assertIn("alb_d", by_id, "Swedish-written release should match on writers")
        self.assertEqual(["writer"], by_id["alb_d"].matched_criteria)

        self.assertIn("alb_e", by_id, "Self-releasing Swedish artist should match")
        self.assertIn("artist", by_id["alb_e"].matched_criteria)

        self.assertNotIn("alb_f", by_id, "Release outside the date window must be filtered out")

        self.assertIn("alb_g", by_id, "MusicBrainz-resolved Swedish label should match")
        self.assertIn("label", by_id["alb_g"].matched_criteria)

    def test_window_filter_counts(self):
        pipeline, *_ = make_pipeline()
        result = pipeline.run(["artist"])
        self.assertEqual(6, result.stats.albums_in_window)
        self.assertGreater(result.stats.albums_seen, result.stats.albums_in_window)

    def test_single_criterion_only_reports_that_criterion(self):
        pipeline, *_ = make_pipeline()
        result = pipeline.run(["label"])
        for match in result.matches:
            self.assertEqual(["label"], list(match.verdicts))
        ids = {m.release.id for m in result.matches}
        self.assertIn("alb_a", ids)
        self.assertNotIn("alb_d", ids, "writer-only match must not surface under --criteria label")

    def test_writer_verdict_records_evidence(self):
        pipeline, *_ = make_pipeline()
        result = pipeline.run(["writer"])
        match = next(m for m in result.matches if m.release.id == "alb_d")
        verdict = match.verdicts["writer"]
        self.assertTrue(verdict.matched)
        self.assertEqual(["Max Exempel", "Shellbeck"], verdict.data["swedish_writers"])
        self.assertEqual(2, verdict.data["tracks_qualifying"])
        self.assertTrue(any(s.source == "musicbrainz" for s in verdict.signals))

    def test_evidence_is_attached_to_every_match(self):
        pipeline, *_ = make_pipeline()
        result = pipeline.run(list(ALL_CRITERIA))
        self.assertTrue(result.matches)
        for match in result.matches:
            for name in match.matched_criteria:
                verdict = match.verdicts[name]
                self.assertTrue(verdict.signals, f"{match.release.id}/{name} matched with no evidence")
                self.assertTrue(verdict.summary)

    def test_batches_album_and_artist_lookups(self):
        pipeline, spotify_fake, _, _ = make_pipeline()
        pipeline.run(["artist"])
        # 6 in-window albums must be fetched with a single batched /albums call.
        self.assertEqual(1, spotify_fake.calls.count("/v1/albums"))
        self.assertEqual(1, spotify_fake.calls.count("/v1/artists"))
        self.assertEqual(1, spotify_fake.calls.count("/v1/tracks"))

    def test_token_is_requested_once(self):
        pipeline, spotify_fake, _, _ = make_pipeline()
        pipeline.run(["artist"])
        self.assertEqual(1, spotify_fake.token_calls)

    def test_musicbrainz_budget_is_respected(self):
        pipeline, _, mb_fake, _ = make_pipeline(musicbrainz_budget=4)
        pipeline.run(list(ALL_CRITERIA))
        self.assertLessEqual(len(mb_fake.calls), 6)

    def test_ranking_prefers_multi_criterion_matches(self):
        pipeline, *_ = make_pipeline()
        result = pipeline.run(list(ALL_CRITERIA))
        scores = [m.score for m in result.matches]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual("alb_a", result.matches[0].release.id)

    def test_serialisation_round_trips(self):
        import json

        pipeline, *_ = make_pipeline()
        result = pipeline.run(list(ALL_CRITERIA))
        payload = json.loads(json.dumps(result.to_dict(TODAY)))
        self.assertEqual(len(result.matches), payload["count"])
        first = payload["matches"][0]
        self.assertIn("verdicts", first)
        self.assertIn("spotify_url", first)


class SourceTests(unittest.TestCase):
    def test_label_sources_only_for_label_criterion(self):
        settings = Settings(markets=["SE"])
        reference = load_reference()
        artist_only = build_sources(settings, ["artist"], reference, today=TODAY)
        with_labels = build_sources(settings, ["label"], reference, today=TODAY)
        self.assertFalse(any(s.kind == "label-search" for s in artist_only))
        self.assertTrue(any(s.kind == "label-search" for s in with_labels))

    def test_every_market_gets_a_new_releases_source(self):
        settings = Settings(markets=["SE", "NO"])
        sources = build_sources(settings, ["artist"], load_reference(), today=TODAY)
        markets = {s.market for s in sources if s.kind == "new-releases"}
        self.assertEqual({"SE", "NO"}, markets)


if __name__ == "__main__":
    unittest.main()
