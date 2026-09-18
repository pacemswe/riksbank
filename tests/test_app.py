"""Tests for the reporting, storage, HTTP server and CLI layers."""

from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from swedish_radar import report
from swedish_radar.cli import main
from swedish_radar.config import ALL_CRITERIA, Settings
from swedish_radar.models import RunResult, RunStats
from swedish_radar.server import make_server
from swedish_radar.store import Store
from tests.catalogue import TODAY
from tests.test_pipeline import make_pipeline


def sample_result() -> RunResult:
    pipeline, *_ = make_pipeline()
    return pipeline.run(list(ALL_CRITERIA))


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = sample_result()

    def test_json_is_valid_and_complete(self):
        payload = json.loads(report.to_json(self.result, today=TODAY))
        self.assertEqual(len(self.result.matches), payload["count"])
        self.assertEqual(list(ALL_CRITERIA), payload["criteria"])
        self.assertIn("stats", payload)

    def test_csv_has_a_row_per_match(self):
        import csv

        rows = list(csv.reader(io.StringIO(report.to_csv(self.result, today=TODAY))))
        self.assertEqual(len(self.result.matches) + 1, len(rows))
        self.assertIn("spotify_url", rows[0])

    def test_csv_escapes_commas_in_titles(self):
        text = report.to_csv(self.result, today=TODAY)
        import csv

        for row in csv.reader(io.StringIO(text)):
            self.assertEqual(13, len(row), "every row must have the same column count")

    def test_markdown_lists_every_match_with_evidence(self):
        text = report.to_markdown(self.result, today=TODAY)
        for match in self.result.matches:
            self.assertIn(match.release.name, text)
        self.assertIn("Run statistics", text)

    def test_text_output_is_non_empty(self):
        self.assertIn("match(es)", report.to_text(self.result, today=TODAY))

    def test_empty_result_renders_in_every_format(self):
        empty = RunResult(matches=[], stats=RunStats(), criteria=["artist"])
        for fmt in ("json", "csv", "markdown", "text"):
            self.assertIsInstance(report.render(empty, fmt), str)

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(ValueError):
            report.render(self.result, "xml")


class StoreTests(unittest.TestCase):
    def test_runs_round_trip(self):
        result = sample_result()
        with Store(Path(":memory:")) as store:
            run_id = store.save_run(result, today=TODAY)
            latest = store.latest_run()
            self.assertEqual(run_id, latest["run_id"])
            self.assertEqual(len(result.matches), latest["count"])
            self.assertEqual(1, len(store.list_runs()))
            self.assertEqual(latest["count"], store.get_run(run_id)["count"])

    def test_missing_run_returns_none(self):
        with Store(Path(":memory:")) as store:
            self.assertIsNone(store.get_run(999))
            self.assertIsNone(store.latest_run())

    def test_first_seen_tracks_releases_across_runs(self):
        result = sample_result()
        with Store(Path(":memory:")) as store:
            store.save_run(result, today=TODAY)
            store.save_run(result, today=TODAY)
            ids = [m.release.id for m in result.matches]
            seen = store.first_seen(ids)
            self.assertEqual(set(ids), set(seen))
            self.assertEqual({}, store.first_seen([]))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store_path = Path(":memory:")
        settings = Settings(store_path=cls.store_path, cache_enabled=False)
        cls.httpd, cls.service = make_server(settings, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.service.store.save_run(sample_result(), today=TODAY)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.service.store.close()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as handle:
            return handle.status, handle.read(), dict(handle.headers)

    def get_json(self, path):
        status, body, _ = self.get(path)
        return status, json.loads(body)

    def post_json(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as handle:
                return handle.status, json.loads(handle.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_index_is_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(200, status)
        self.assertIn(b"Swedish Radar", body)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_static_assets_are_served_with_correct_types(self):
        for path, expected in (("/static/styles.css", "text/css"), ("/static/app.js", "javascript")):
            _, _, headers = self.get(path)
            self.assertIn(expected, headers["Content-Type"])

    def test_directory_traversal_is_blocked(self):
        for attempt in ("/static/../config.py", "/static/..%2fconfig.py", "/static/../../etc/passwd"):
            try:
                status, _, _ = self.get(attempt)
            except urllib.error.HTTPError as exc:
                status = exc.code
            self.assertIn(status, (403, 404), attempt)

    def test_config_endpoint(self):
        status, payload = self.get_json("/api/config")
        self.assertEqual(200, status)
        self.assertEqual(list(ALL_CRITERIA), payload["criteria"])
        self.assertFalse(payload["has_credentials"])
        self.assertGreater(payload["curated_labels"], 0)

    def test_latest_run_is_returned(self):
        status, payload = self.get_json("/api/runs/latest")
        self.assertEqual(200, status)
        self.assertGreater(payload["count"], 0)
        self.assertIn("matched_criteria", payload["matches"][0])

    def test_labels_endpoint(self):
        _, payload = self.get_json("/api/labels")
        self.assertGreater(payload["count"], 0)
        self.assertIn("name", payload["labels"][0])

    def test_unknown_run_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/runs/99999")
        self.assertEqual(404, ctx.exception.code)

    def test_non_numeric_run_id_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/runs/not-a-number")
        self.assertEqual(400, ctx.exception.code)

    def test_discover_requires_a_criterion(self):
        status, payload = self.post_json("/api/discover", {"criteria": []})
        self.assertEqual(400, status)
        self.assertIn("criterion", payload["error"])

    def test_discover_rejects_non_numeric_tuning(self):
        status, payload = self.post_json("/api/discover", {"criteria": ["artist"], "days": "many"})
        self.assertEqual(400, status)

    def test_discover_without_credentials_fails_the_job_not_the_request(self):
        status, payload = self.post_json("/api/discover", {"criteria": ["artist"]})
        self.assertEqual(202, status)
        job_id = payload["id"]
        for _ in range(50):
            _, job = self.get_json(f"/api/jobs/{job_id}")
            if job["status"] in ("done", "error"):
                break
        self.assertEqual("error", job["status"])
        self.assertIn("SPOTIFY_CLIENT_ID", job["error"])

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nonsense")
        self.assertEqual(404, ctx.exception.code)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_doctor_reports_missing_credentials(self):
        code, out, _ = self.run_cli(["--env-file", "/nonexistent/.env", "doctor"])
        self.assertEqual(1, code)
        self.assertIn("MISSING", out)

    def test_labels_list(self):
        code, out, _ = self.run_cli(["labels", "list"])
        self.assertEqual(0, code)
        self.assertIn("Adrian Recordings", out)

    def test_labels_check_classifies_correctly(self):
        code, out, _ = self.run_cli(
            ["labels", "check", "Adrian Recordings", "Universal Music AB", "Svart Records", "DistroKid"]
        )
        self.assertEqual(0, code)
        self.assertIn("Swedish independent", out)
        self.assertIn("major label", out)
        self.assertIn("not Swedish", out)
        self.assertIn("DIY distributor", out)

    def test_unknown_criteria_are_rejected(self):
        code, _, err = self.run_cli(
            ["--env-file", "/nonexistent/.env", "discover", "--criteria", "producer"]
        )
        self.assertEqual(2, code)
        self.assertIn("unknown criteria", err)

    def test_discover_without_credentials_exits_cleanly(self):
        code, _, err = self.run_cli(["--env-file", "/nonexistent/.env", "discover", "-q"])
        self.assertEqual(2, code)
        self.assertIn("SPOTIFY_CLIENT_ID", err)

    def test_help_lists_every_command(self):
        with self.assertRaises(SystemExit):
            self.run_cli(["--help"])


if __name__ == "__main__":
    unittest.main()


class RoundTripTests(unittest.TestCase):
    """The JSON output must be readable back as a RunResult."""

    @classmethod
    def setUpClass(cls):
        cls.result = sample_result()
        cls.payload = json.loads(report.to_json(cls.result, today=TODAY))

    def test_matches_and_verdicts_survive(self):
        from swedish_radar.models import run_result_from_dict

        restored = run_result_from_dict(self.payload)
        self.assertEqual(len(self.result.matches), len(restored.matches))
        self.assertEqual(list(self.result.criteria), list(restored.criteria))
        for original, copy in zip(self.result.matches, restored.matches):
            self.assertEqual(original.release.id, copy.release.id)
            self.assertEqual(original.release.label, copy.release.label)
            self.assertEqual(original.release.release_date, copy.release.release_date)
            self.assertEqual(original.matched_criteria, copy.matched_criteria)
            self.assertAlmostEqual(original.score, copy.score, places=3)
            for name, verdict in original.verdicts.items():
                restored_verdict = copy.verdicts[name]
                self.assertEqual(verdict.matched, restored_verdict.matched)
                self.assertEqual(verdict.summary, restored_verdict.summary)
                self.assertEqual(len(verdict.signals), len(restored_verdict.signals))

    def test_stats_survive(self):
        from swedish_radar.models import run_result_from_dict

        restored = run_result_from_dict(self.payload)
        self.assertEqual(self.result.stats.albums_in_window, restored.stats.albums_in_window)
        self.assertEqual(self.result.stats.albums_seen, restored.stats.albums_seen)

    def test_restored_result_renders_in_every_format(self):
        from swedish_radar.models import run_result_from_dict

        restored = run_result_from_dict(self.payload)
        for fmt in ("json", "csv", "markdown", "text"):
            text = report.render(restored, fmt)
            self.assertIn(self.result.matches[0].release.name, text)

    def test_garbage_input_does_not_crash(self):
        from swedish_radar.models import run_result_from_dict

        for payload in ({}, {"matches": None}, {"matches": [{}]}, {"matches": ["oops"]}):
            restored = run_result_from_dict(payload)
            self.assertIsInstance(restored.matches, list)


class RenderCommandTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_render_from_a_json_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_text(report.to_json(sample_result(), today=TODAY), encoding="utf-8")
            code, out, _ = self.run_cli(["render", "--input", str(path), "--format", "markdown"])
            self.assertEqual(0, code)
            self.assertIn("# Swedish Radar", out)
            self.assertIn("Nattens Ljus", out)

    def test_render_writes_a_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "run.json"
            target = Path(tmp) / "nested" / "digest.md"
            source.write_text(report.to_json(sample_result(), today=TODAY), encoding="utf-8")
            code, _, _ = self.run_cli(
                ["render", "--input", str(source), "--format", "markdown", "--out", str(target)]
            )
            self.assertEqual(0, code)
            self.assertIn("Swedish Radar", target.read_text(encoding="utf-8"))

    def test_render_reports_a_missing_file(self):
        code, _, err = self.run_cli(["render", "--input", "/definitely/not/here.json"])
        self.assertEqual(1, code)
        self.assertIn("No such file", err)

    def test_render_reports_invalid_json(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            code, _, err = self.run_cli(["render", "--input", str(path)])
            self.assertEqual(1, code)
            self.assertIn("not valid JSON", err)

    def test_render_from_the_store(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "runs.sqlite3"
            with Store(store_path) as store:
                store.save_run(sample_result(), today=TODAY)
            code, out, _ = self.run_cli(
                ["render", "--store-path", str(store_path), "--format", "text"]
            )
            self.assertEqual(0, code)
            self.assertIn("match(es)", out)

    def test_render_reports_an_empty_store(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli(
                ["render", "--store-path", str(Path(tmp) / "empty.sqlite3")]
            )
            self.assertEqual(1, code)
            self.assertIn("discover --save", err)
