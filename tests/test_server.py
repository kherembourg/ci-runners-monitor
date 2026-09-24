import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(owner="owner", repositories=("repo",), max_vms=2,
            state_dir=Path(self.temp.name), lanes={"linux": SimpleNamespace(label="linux")})
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, method, path):
        self.calls.append(path)
        if "/jobs?" in path:
            return {"jobs": [{"id": 7, "name": "build", "status": "completed", "conclusion": "failure",
                "started_at": "2024-01-01T00:00:00Z", "completed_at": "2024-01-01T00:00:10Z",
                "labels": ["linux"], "html_url": "https://github.test/job/7",
                "steps": [{"name": "test", "status": "completed", "conclusion": "failure"}]}]}
        return {"workflow_runs": [{"id": 4, "name": "CI", "status": "completed", "html_url": "https://github.test/run/4"}]}

    def test_jobs_steps_ledger_capacity_and_cache(self):
        Path(self.temp.name, "instances.json").write_text(json.dumps({"7": {"repo": "repo", "lane": "linux", "vm": "safe-vm", "phase": "running"},
            "8": {"repo": "repo", "lane": "linux", "vm": "local", "phase": "needs_attention"}}))
        dashboard = server.Dashboard(self.config, self.transport)
        data = dashboard.data()
        job = next(j for j in data["jobs"] if j["id"] == 7)
        self.assertEqual(job["conclusion"], "failure")
        self.assertEqual(job["duration_seconds"], 10)
        self.assertEqual(job["steps"][0]["conclusion"], "failure")
        self.assertEqual(data["fleet"]["occupied"], 2)
        self.assertEqual(data["fleet"]["capacity"], 2)
        count = len(self.calls)
        dashboard.data()
        self.assertEqual(len(self.calls), count)

    def test_stale_failure_keeps_last_good_and_local_placeholder(self):
        calls = [0]
        def flaky(method, path):
            if calls[0] < 6:
                calls[0] += 1
                return self.transport(method, path)
            raise RuntimeError("offline")
        dashboard = server.Dashboard(self.config, flaky, cache_seconds=0)
        first = dashboard.data()
        second = dashboard.data()
        self.assertTrue(first["github"]["ok"])
        self.assertFalse(second["github"]["ok"])
        self.assertIsNotNone(second["github"]["fetched_at"])
        third = dashboard.data()
        self.assertFalse(third["github"]["ok"])
        self.assertEqual(third["github"]["error"], second["github"]["error"])
        self.assertEqual(len(second["jobs"]), 1)

    def test_queued_job_has_no_execution_duration_and_active_run_is_requested(self):
        def queued(method, path):
            if "/jobs?" in path:
                return {"jobs": [{"id": 9, "status": "queued", "started_at": "2024-01-01T00:00:00Z",
                                  "labels": ["linux"]}]}
            return {"workflow_runs": [{"id": 35919729358, "name": "active", "status": "queued"}]}
        dashboard = server.Dashboard(self.config, queued)
        data = dashboard.data()
        self.assertEqual(data["jobs"][0]["status"], "queued")
        self.assertIsNone(data["jobs"][0]["duration_seconds"])

    def test_active_runs_paginate_beyond_first_three_and_report_safety_truncation(self):
        self.config.repositories = ("repo",)
        calls = []
        active = [{"id": i, "name": "CI", "status": "queued"} for i in range(1, 106)]
        def paged(method, path):
            calls.append(path)
            if "/jobs?" in path:
                return {"jobs": []}
            if "status=queued" in path:
                page = int(path.rsplit("page=", 1)[1].split("&", 1)[0])
                start = (page - 1) * 100
                return {"workflow_runs": active[start:start + 100]}
            return {"workflow_runs": []}
        result = server.Dashboard(self.config, paged).data()
        active_pages = [p for p in calls if "status=queued" in p]
        self.assertIn("per_page=100&page=2", " ".join(active_pages))
        self.assertFalse(result["coverage"]["truncated"])

    def test_recent_window_fetches_only_ten_runs(self):
        calls = []
        def many(method, path):
            calls.append(path)
            if "/jobs?" in path:
                return {"jobs": []}
            if "status=" in path:
                return {"workflow_runs": []}
            self.assertIn("per_page=10&page=1", path)
            return {"workflow_runs": [{"id": i, "status": "completed"} for i in range(1, 11)]}
        server.Dashboard(self.config, many).data()
        self.assertEqual(len([p for p in calls if "/jobs?" in p]), 10)
        self.assertEqual(len([p for p in calls if "/runs?" in p and "status=" not in p]), 1)

    def test_page_safety_limit_is_exposed(self):
        dashboard = server.Dashboard(self.config, lambda method, path: {"workflow_runs": [{"id": 4, "status": "queued"}] * 100} if "/runs?" in path else {"jobs": []})
        dashboard.MAX_PAGES = 1
        result = dashboard.data()
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(result["coverage"]["truncated_repos"], ["repo"])

    def test_api_result_window_short_page_reports_missing_runs(self):
        def limited(method, path):
            if "/jobs?" in path or "status=in_progress" in path:
                return {"jobs": []} if "/jobs?" in path else {"workflow_runs": []}
            if "status=queued" in path:
                if "page=1&" in path:
                    return {"workflow_runs": [{"id": i, "status": "queued"} for i in range(100)], "total_count": 150}
                return {"workflow_runs": [], "total_count": 150}
            return {"workflow_runs": []}
        result = server.Dashboard(self.config, limited).data()
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(result["coverage"]["truncated_repos"], ["repo"])

    def test_repo_failure_keeps_only_failed_repo_stale_and_registry_phase_is_not_cached(self):
        self.config.repositories = ("one", "two")
        failed = [False]
        def transport(method, path):
            one = "/repos/owner/one/" in path
            if one and failed[0]:
                raise RuntimeError("Bearer DO_NOT_LEAK")
            result = self.transport(method, path)
            if "/jobs?" in path:
                result = {"jobs": [{**result["jobs"][0], "id": 7 if one else 8,
                                     "name": "updated" if failed[0] else "build"}]}
            return result
        dashboard = server.Dashboard(self.config, transport, cache_seconds=0)
        Path(self.temp.name, "instances.json").write_text(json.dumps({"7": {"repo": "one", "lane": "linux", "phase": "first"}}))
        first = dashboard.data()
        self.assertEqual(next(j for j in first["jobs"] if j["id"] == 7)["phase"], "first")
        Path(self.temp.name, "instances.json").write_text("{}")
        failed[0] = True
        second = dashboard.data()
        self.assertFalse(second["github"]["ok"])
        stale = next(r for r in second["github"]["repos"] if r["repo"] == "one")
        fresh = next(r for r in second["github"]["repos"] if r["repo"] == "two")
        self.assertTrue(stale["stale"])
        self.assertFalse(fresh["stale"])
        self.assertEqual(stale["fetched_at"], first["github"]["repos"][0]["fetched_at"])
        self.assertNotEqual(fresh["fetched_at"], first["github"]["repos"][1]["fetched_at"])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(second))
        self.assertIsNone(next(j for j in second["jobs"] if j["id"] == 7)["phase"])
        self.assertEqual(next(j for j in second["jobs"] if j["id"] == 8)["name"], "updated")

    def test_transport_exception_never_exposes_credentials(self):
        def secret_error(*_args):
            raise RuntimeError("Authorization: Bearer TOP_SECRET")
        result = server.Dashboard(self.config, secret_error).data()
        self.assertFalse(result["github"]["ok"])
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        self.assertIn("GitHub API unavailable", result["github"]["error"])

    def test_corrupt_local_ledger_is_not_reported_as_free_capacity(self):
        Path(self.temp.name, "instances.json").write_text("not-json")
        result = server.Dashboard(self.config, self.transport).data()
        self.assertIn("Local state unreadable", result["fleet"]["error"])

    def test_rejects_dns_rebinding_host(self):
        app = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(
            SimpleNamespace(data=lambda: {"ok": True}), self.temp.name))
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:{}/api/dashboard".format(app.server_port)
            self.assertEqual(json.load(urllib.request.urlopen(url)), {"ok": True})
            request = urllib.request.Request(url, headers={"Host": "attacker.example"})
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(request)
            self.assertEqual(result.exception.code, 403)
        finally:
            app.shutdown()
            app.server_close()
            thread.join()

    def test_public_files_do_not_embed_user_home_paths(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("server.py", "static/index.html", "README.md", "launchd/monitor.example.plist"):
            with self.subTest(file=name):
                self.assertNotRegex((root / name).read_text(), r"/(?:Users|home)/[^/]+/")

    def test_failure_without_snapshot_is_safe(self):
        dashboard = server.Dashboard(self.config, lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
        data = dashboard.data()
        self.assertFalse(data["github"]["ok"])
        self.assertEqual(data["jobs"], [])


if __name__ == "__main__":
    unittest.main()
