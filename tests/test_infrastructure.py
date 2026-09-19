"""Tests for the text, HTTP, cache, client and reference layers."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from swedish_radar import textutil as tx
from swedish_radar.cache import Cache
from swedish_radar.clients.musicbrainz import MusicBrainzClient
from swedish_radar.clients.spotify import SpotifyClient, label_query, quote_search_value
from swedish_radar.config import Settings, load_settings
from swedish_radar.errors import AuthError, ConfigError, HttpError, RateLimited, TransportError
from swedish_radar.http import FakeTransport, JsonClient, RateLimiter, Response, build_url, chunked
from swedish_radar.models import Release, Track, parse_release_date
from swedish_radar.reference import load_reference
from tests.catalogue import build
from tests.fakes import RoutingTransport


class TextUtilTests(unittest.TestCase):
    def test_accent_folding_covers_nordic_letters(self):
        self.assertEqual("aao", tx.strip_accents("åäö"))
        self.assertEqual("oae", tx.strip_accents("øæ"))

    def test_label_key_drops_company_suffixes(self):
        self.assertEqual("adrian recordings", tx.label_key("Adrian Recordings AB"))
        self.assertEqual("adrian recordings", tx.label_key("  adrian, recordings  "))

    def test_loose_key_drops_industry_words(self):
        self.assertEqual("adrian", tx.loose_key("Adrian Recordings"))
        self.assertEqual("adrian", tx.loose_key("Adrian Records AB"))

    def test_loose_key_of_a_generic_name_is_empty(self):
        # Must be empty so it cannot fuzzy-match every other generic name.
        self.assertEqual("", tx.loose_key("Music AB"))
        self.assertEqual("", tx.loose_key("The Records"))

    def test_split_label_string_handles_separators_and_phrases(self):
        self.assertEqual(
            ["Rama Lama Records", "Sony Music"],
            tx.split_label_string("Rama Lama Records / Sony Music"),
        )
        self.assertEqual(
            ["1631 Recordings", "Decca"],
            tx.split_label_string("1631 Recordings under exclusive license to Decca"),
        )
        self.assertEqual(["YEAR0001"], tx.split_label_string("YEAR0001"))
        self.assertEqual([], tx.split_label_string(""))

    def test_ampersand_is_normalised(self):
        self.assertEqual(
            tx.label_key("Welfare Sounds & Records"), tx.label_key("Welfare Sounds and Records")
        )

    def test_swedish_company_form_detection(self):
        self.assertTrue(tx.is_swedish_company_form("Bolaget Media AB"))
        self.assertFalse(tx.is_swedish_company_form("Bolaget Media Inc"))


class ModelTests(unittest.TestCase):
    def test_partial_release_dates_widen_to_the_period_start(self):
        self.assertEqual("2026-01-01", parse_release_date("2026", "year").isoformat())
        self.assertEqual("2026-09-01", parse_release_date("2026-09", "month").isoformat())
        self.assertIsNone(parse_release_date("not-a-date", "day"))

    def test_isrc_is_upper_cased(self):
        track = Track.from_spotify({"id": "t", "name": "x", "external_ids": {"isrc": "sebgk2600001"}})
        self.assertEqual("SEBGK2600001", track.isrc)

    def test_release_survives_missing_fields(self):
        release = Release.from_spotify({"id": "a"})
        self.assertEqual("a", release.id)
        self.assertEqual([], release.tracks)
        self.assertIsNone(release.release_date)


class HttpTests(unittest.TestCase):
    def test_retries_then_succeeds_on_429(self):
        calls = {"n": 0}
        sleeps = []

        class Flaky:
            def request(self, method, url, *, headers=None, body=None, timeout=20.0):
                calls["n"] += 1
                if calls["n"] < 3:
                    return Response(429, {"Retry-After": "2"}, b"{}", url)
                return Response(200, {}, b'{"ok": true}', url)

        client = JsonClient(Flaky(), user_agent="ua", sleep=sleeps.append)
        self.assertEqual({"ok": True}, client.get_json("https://x/y"))
        self.assertEqual(3, calls["n"])
        self.assertEqual([2.0, 2.0], sleeps, "Retry-After must be honoured")

    def test_gives_up_after_max_retries(self):
        class AlwaysLimited:
            def request(self, method, url, *, headers=None, body=None, timeout=20.0):
                return Response(429, {"Retry-After": "1"}, b"nope", url)

        client = JsonClient(AlwaysLimited(), user_agent="ua", max_retries=2, sleep=lambda _s: None)
        with self.assertRaises(RateLimited):
            client.get_json("https://x/y")

    def test_404_can_be_allowed(self):
        transport = FakeTransport()
        transport.add_json("GET", "https://x/missing", {"error": "nope"}, status=404)
        client = JsonClient(transport, user_agent="ua")
        self.assertIsNone(client.get_json("https://x/missing", allow_404=True))

    def test_auth_errors_are_not_retried(self):
        calls = {"n": 0}

        class Unauthorised:
            def request(self, method, url, *, headers=None, body=None, timeout=20.0):
                calls["n"] += 1
                return Response(401, {}, b"bad token", url)

        client = JsonClient(Unauthorised(), user_agent="ua", sleep=lambda _s: None)
        with self.assertRaises(AuthError):
            client.get_json("https://x/y")
        self.assertEqual(1, calls["n"])

    def test_server_errors_back_off_exponentially(self):
        sleeps = []

        class Broken:
            def request(self, method, url, *, headers=None, body=None, timeout=20.0):
                return Response(503, {}, b"", url)

        client = JsonClient(Broken(), user_agent="ua", max_retries=3, sleep=sleeps.append)
        with self.assertRaises(HttpError):
            client.get_json("https://x/y")
        self.assertEqual([1.0, 2.0, 4.0], sleeps)

    def test_rate_limiter_spaces_requests(self):
        # Sleeping advances the clock, as it does in reality.
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        limiter = RateLimiter(1.0, sleep=sleep)
        for _ in range(3):
            limiter.acquire(now=lambda: clock[0])
        self.assertEqual([1.0, 1.0], sleeps, "one second between consecutive requests")
        self.assertEqual(2.0, clock[0])

    def test_rate_limiter_does_not_sleep_when_enough_time_has_passed(self):
        clock = [0.0]
        sleeps = []
        limiter = RateLimiter(1.0, sleep=sleeps.append)
        limiter.acquire(now=lambda: clock[0])
        clock[0] = 5.0
        limiter.acquire(now=lambda: clock[0])
        self.assertEqual([], sleeps)

    def test_unlimited_rate_limiter_never_sleeps(self):
        sleeps = []
        RateLimiter(0, sleep=sleeps.append).acquire()
        self.assertEqual([], sleeps)

    def test_build_url_encodes_lists_and_skips_none(self):
        url = build_url("https://api/v1", "albums", {"ids": ["a", "b"], "market": "SE", "x": None})
        self.assertEqual("https://api/v1/albums?ids=a%2Cb&market=SE", url)

    def test_chunked_splits_evenly(self):
        self.assertEqual([[1, 2], [3, 4], [5]], [list(c) for c in chunked([1, 2, 3, 4, 5], 2)])

    def test_fake_transport_rejects_unknown_routes(self):
        with self.assertRaises(TransportError):
            FakeTransport().request("GET", "https://nowhere/")


class CacheTests(unittest.TestCase):
    def test_values_expire(self):
        clock = [1000.0]
        cache = Cache(Path(":memory:"), clock=lambda: clock[0])
        cache.set("k", {"v": 1}, ttl=10)
        self.assertEqual({"v": 1}, cache.get("k"))
        clock[0] = 1011.0
        self.assertIsNone(cache.get("k"))

    def test_disabled_cache_is_a_no_op(self):
        cache = Cache(Path(":memory:"), enabled=False)
        cache.set("k", 1, ttl=100)
        self.assertIsNone(cache.get("k"))

    def test_hit_and_miss_counters(self):
        cache = Cache(Path(":memory:"))
        cache.set("k", 1, ttl=100)
        cache.get("k")
        cache.get("absent")
        self.assertEqual(1, cache.hits)
        self.assertEqual(1, cache.misses)

    def test_purge_expired_removes_only_stale_rows(self):
        clock = [0.0]
        cache = Cache(Path(":memory:"), clock=lambda: clock[0])
        cache.set("old", 1, ttl=1)
        cache.set("new", 2, ttl=1000)
        clock[0] = 10.0
        self.assertEqual(1, cache.purge_expired())
        self.assertEqual(2, cache.get("new"))


class SpotifyClientTests(unittest.TestCase):
    def setUp(self):
        spotify_fake, mb_fake = build()
        self.fake = spotify_fake
        self.transport = RoutingTransport(spotify_fake, mb_fake)
        self.settings = Settings(spotify_client_id="id", spotify_client_secret="secret")
        self.client = SpotifyClient(self.settings, transport=self.transport)

    def test_token_is_reused_until_expiry(self):
        self.client.token()
        self.client.token()
        self.assertEqual(1, self.fake.token_calls)

    def test_missing_credentials_raise_a_helpful_error(self):
        client = SpotifyClient(Settings(), transport=self.transport)
        with self.assertRaises(ConfigError) as ctx:
            client.token()
        self.assertIn("SPOTIFY_CLIENT_ID", str(ctx.exception))

    def test_albums_are_batched_at_twenty(self):
        ids = [f"id{i}" for i in range(45)]
        self.client.albums(ids)
        self.assertEqual(3, self.fake.calls.count("/v1/albums"))

    def test_artists_are_batched_at_fifty(self):
        self.client.artists([f"id{i}" for i in range(120)])
        self.assertEqual(3, self.fake.calls.count("/v1/artists"))

    def test_duplicate_ids_are_requested_once(self):
        self.client.albums(["alb_a", "alb_a", "alb_a"])
        self.assertEqual(1, self.fake.calls.count("/v1/albums"))

    def test_caching_prevents_repeat_requests(self):
        cache = Cache(Path(":memory:"))
        client = SpotifyClient(self.settings, transport=self.transport, cache=cache)
        client.albums(["alb_a"])
        before = self.fake.calls.count("/v1/albums")
        client.albums(["alb_a"])
        self.assertEqual(before, self.fake.calls.count("/v1/albums"))

    def test_label_query_quoting(self):
        self.assertEqual('label:"Woah Dad!" tag:new', label_query("Woah Dad!"))
        self.assertEqual("Say hi", quote_search_value('Say "hi"'))


class MusicBrainzClientTests(unittest.TestCase):
    def setUp(self):
        spotify_fake, mb_fake = build()
        self.mb_fake = mb_fake
        transport = RoutingTransport(spotify_fake, mb_fake)
        settings = Settings(musicbrainz_contact="test@example.com")
        self.client = MusicBrainzClient(settings, transport=transport, sleep=lambda _s: None)

    def test_resolves_artist_through_the_spotify_url(self):
        self.assertEqual("mb_se", self.client.artist_mbid_for_spotify("art_se"))
        artist = self.client.artist("mb_se")
        self.assertEqual("SE", artist.country)
        self.assertTrue(artist.is_swedish)

    def test_unknown_spotify_artist_returns_none(self):
        self.assertIsNone(self.client.artist_mbid_for_spotify("nope"))

    def test_isrc_to_writers(self):
        works = self.client.works_for_isrc("GBARL2600001")
        self.assertEqual(["work_1"], works)
        work = self.client.work("work_1")
        self.assertEqual({"Max Exempel", "Shellbeck"}, {w.name for w in work.writers})
        self.assertEqual({"composer"}, next(w for w in work.writers if w.name == "Max Exempel").roles)

    def test_arrangers_are_not_counted_as_writers(self):
        self.mb_fake.works["work_arr"] = {
            "id": "work_arr",
            "title": "Arranged",
            "relations": [
                {"type": "arranger", "target-type": "artist", "artist": {"id": "x", "name": "Arr"}},
                {"type": "composer", "target-type": "artist", "artist": {"id": "y", "name": "Comp"}},
            ],
        }
        work = self.client.work("work_arr")
        self.assertEqual(["Comp"], [w.name for w in work.writers])

    def test_label_lookup_by_barcode(self):
        labels = self.client.labels_for_barcode("7320470260007")
        self.assertEqual(["mb_lab_1"], [l.mbid for l in labels])
        self.assertTrue(self.client.label("mb_lab_1").is_swedish)

    def test_disabled_client_makes_no_requests(self):
        settings = Settings(musicbrainz_enabled=False)
        client = MusicBrainzClient(settings, transport=RoutingTransport(*build()), sleep=lambda _s: None)
        self.assertIsNone(client.artist("mb_se"))
        self.assertEqual(0, client.request_count)

    def test_name_search_rejects_ambiguous_hits(self):
        self.mb_fake.artist_name_search['artist:"Duplicated"'] = [
            {"id": "a", "name": "Duplicated", "score": 100, "country": "SE"},
            {"id": "b", "name": "Duplicated", "score": 100, "country": "US"},
        ]
        self.assertIsNone(self.client.search_artist("Duplicated"))

    def test_name_search_accepts_a_single_exact_hit(self):
        self.mb_fake.artist_name_search['artist:"Unika"'] = [
            {"id": "a", "name": "Unika", "score": 100, "country": "SE"},
            {"id": "b", "name": "Unika Band", "score": 95, "country": "US"},
        ]
        hit = self.client.search_artist("Unika")
        self.assertEqual("SE", hit.country)

    def test_errors_are_collected_not_raised(self):
        class Broken:
            def request(self, method, url, *, headers=None, body=None, timeout=20.0):
                raise TransportError("network down")

        client = MusicBrainzClient(Settings(), transport=Broken(), sleep=lambda _s: None)
        self.assertIsNone(client.artist("mb_se"))
        self.assertEqual(1, len(client.errors))


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.reference = load_reference()

    def test_every_curated_label_round_trips_through_lookup(self):
        for entry in self.reference.labels.entries:
            for name in entry.all_names():
                found, how = self.reference.labels.lookup(name)
                self.assertIsNotNone(found, f"{name!r} does not resolve")
                self.assertEqual("exact", how, f"{name!r} only matched loosely")

    def test_no_curated_label_is_also_a_major_marker(self):
        for entry in self.reference.labels.entries:
            if not entry.independent:
                continue
            self.assertIsNone(
                self.reference.majors.group_for(entry.name),
                f"{entry.name} is on both the indie list and a major's marker list",
            )

    def test_no_curated_label_is_also_a_false_friend(self):
        for entry in self.reference.labels.entries:
            if entry.independent:
                self.assertIsNone(
                    self.reference.labels.lookup_false_friend(entry.name),
                    f"{entry.name} is on both the indie list and the false-friend list",
                )

    def test_major_markers_match_real_label_strings(self):
        cases = {
            "Universal Music AB": "Universal Music Group",
            "Sony Music Entertainment Sweden AB": "Sony Music Entertainment",
            "Warner Music Sweden": "Warner Music Group",
            "Columbia Records": "Sony Music Entertainment",
        }
        for text, group in cases.items():
            self.assertEqual(group, self.reference.majors.group_for(text), text)

    def test_unrelated_names_match_no_major(self):
        for name in ("Adrian Recordings", "Höga Nord Rekords", "Studio Barnhus"):
            self.assertIsNone(self.reference.majors.group_for(name), name)

    def test_data_files_are_valid_json_with_the_expected_shape(self):
        from swedish_radar.config import DATA_DIR

        labels = json.loads((DATA_DIR / "swedish_labels.json").read_text(encoding="utf-8"))
        self.assertTrue(labels["labels"])
        for entry in labels["labels"]:
            self.assertIn("name", entry)
            self.assertIsInstance(entry.get("confidence", 0.8), (int, float))
            self.assertGreaterEqual(entry.get("confidence", 0.8), 0.0)
            self.assertLessEqual(entry.get("confidence", 0.8), 1.0)


class SettingsTests(unittest.TestCase):
    def test_dotenv_is_read_and_overridden_by_the_environment(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('SPOTIFY_CLIENT_ID="from-file"\nRADAR_DAYS=30\n', encoding="utf-8")
            os.environ.pop("SPOTIFY_CLIENT_ID", None)
            settings = load_settings(path)
            self.assertEqual("from-file", settings.spotify_client_id)
            self.assertEqual(30, settings.days)

            os.environ["SPOTIFY_CLIENT_ID"] = "from-env"
            try:
                self.assertEqual("from-env", load_settings(path).spotify_client_id)
            finally:
                os.environ.pop("SPOTIFY_CLIENT_ID", None)

    def test_invalid_values_are_rejected(self):
        import os

        os.environ["RADAR_DAYS"] = "zero"
        try:
            with self.assertRaises(ConfigError):
                load_settings(Path("/nonexistent/.env"))
        finally:
            os.environ.pop("RADAR_DAYS", None)

    def test_overrides_ignore_none(self):
        settings = Settings(days=14)
        self.assertEqual(14, settings.with_overrides(days=None).days)
        self.assertEqual(7, settings.with_overrides(days=7).days)


if __name__ == "__main__":
    unittest.main()
