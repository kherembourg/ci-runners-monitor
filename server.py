#!/usr/bin/env python3
"""Read-only local dashboard for personal-ci runners."""
import argparse
import datetime as dt
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def seconds(start, end):
    try:
        a = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(end.replace("Z", "+00:00")) if end else dt.datetime.now(dt.timezone.utc)
        return max(0, int((b - a).total_seconds()))
    except (AttributeError, TypeError, ValueError):
        return None


class Dashboard:
    PAGE_SIZE = 100
    MAX_PAGES = 100

    def __init__(self, config, transport, cache_seconds=90, clock=time.monotonic):
        self.config, self.transport = config, transport
        self.cache_seconds, self.clock = cache_seconds, clock
        self.lock = threading.Lock()
        self.snapshot, self.expires = None, 0
        self.repo_snapshots = {repo: {} for repo in config.repositories}
        self.repo_fetched = {repo: None for repo in config.repositories}

    def _repo_jobs(self, repo):
        root = "/repos/{}/{}/actions".format(self.config.owner, repo)
        runs, seen = [], set()
        # Keep the ten most recent runs, plus every active run. Pagination has an
        # explicit safety ceiling; callers receive truncated coverage if reached.
        truncated = False
        for query in ("", "&status=queued", "&status=in_progress"):
            page = 1
            while page <= self.MAX_PAGES:
                size = 10 if not query else self.PAGE_SIZE
                result = self.transport("GET", root + "/runs?per_page={}&page={}{}".format(size, page, query))
                batch = result.get("workflow_runs", [])
                for run in batch:
                    if run.get("id") not in seen:
                        seen.add(run.get("id")); runs.append(run)
                if not query:
                    break
                if len(batch) < self.PAGE_SIZE:
                    if result.get("total_count", 0) > (page - 1) * self.PAGE_SIZE + len(batch):
                        truncated = True
                    break
                if page == self.MAX_PAGES:
                    truncated = True
                page += 1
        found = {}
        for run in runs:
            if run.get("status") not in ("queued", "in_progress", "completed"):
                continue
            page = 1
            while page <= self.MAX_PAGES:
                result = self.transport("GET", root + "/runs/{}/jobs?per_page={}&page={}".format(run.get("id"), self.PAGE_SIZE, page))
                batch = result.get("jobs", [])
                for job in batch:
                    lanes = [name for name, lane in self.config.lanes.items() if lane.label in (job.get("labels") or ())]
                    if len(lanes) != 1 or job.get("id") is None:
                        continue
                    started, completed = job.get("started_at"), job.get("completed_at")
                    duration = (seconds(started, completed) if job.get("status") == "completed" else
                                seconds(started, None) if job.get("status") == "in_progress" else None)
                    steps = [{"name": step.get("name", ""), "status": step.get("status", ""), "conclusion": step.get("conclusion")} for step in job.get("steps", [])]
                    found[str(job["id"])] = {"id": job["id"], "repo": repo, "name": job.get("name", ""),
                        "workflow": run.get("name", ""), "status": job.get("status", "unknown"), "conclusion": job.get("conclusion"),
                        "started_at": started, "completed_at": completed, "duration_seconds": duration,
                        "url": job.get("html_url") or run.get("html_url"), "lane": lanes[0], "phase": None, "steps": steps}
                if len(batch) < self.PAGE_SIZE:
                    if result.get("total_count", 0) > (page - 1) * self.PAGE_SIZE + len(batch):
                        truncated = True
                    break
                if page == self.MAX_PAGES:
                    truncated = True
                page += 1
        return found, {"truncated": truncated, "runs_per_repo": 10}

    def _github(self):
        results = {}
        with ThreadPoolExecutor(max_workers=max(1, min(5, len(self.config.repositories)))) as pool:
            futures = {repo: pool.submit(self._repo_jobs, repo) for repo in self.config.repositories}
            for repo, future in futures.items():
                try:
                    results[repo] = (future.result(), None)
                except Exception:
                    results[repo] = (None, "GitHub API unavailable; check the connection and Actions permissions")
        return results

    def data(self):
        with self.lock:
            now = self.clock()
            if self.snapshot is None or now >= self.expires:
                results = self._github()
                for repo, (result, error) in results.items():
                    if result is not None:
                        jobs, coverage = result
                        self.repo_snapshots[repo] = jobs
                        self.repo_fetched[repo] = iso_now()
                        self.repo_coverage = getattr(self, "repo_coverage", {})
                        self.repo_coverage[repo] = coverage
                self.repo_errors = {repo: error for repo, (_, error) in results.items() if error}
                self.snapshot = True
                self.expires = now + self.cache_seconds
            errors = getattr(self, "repo_errors", {})
            jobs = {}
            for per_repo in self.repo_snapshots.values():
                jobs.update(per_repo)
            fetched_times = [v for v in self.repo_fetched.values() if v]
            fetched = max(fetched_times) if fetched_times else None
            ledger_error = None
            try:
                ledger_path = Path(self.config.state_dir) / "instances.json"
                ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
                if not isinstance(ledger, dict): raise ValueError("invalid ledger")
            except (OSError, ValueError):
                ledger, ledger_error = {}, "Local state unreadable; VM occupancy unknown"
            slots, occupied = [], []
            for job_id, entry in ledger.items():
                if not isinstance(entry, dict): continue
                repo, lane = entry.get("repo"), entry.get("lane")
                if repo not in self.config.repositories: repo = "unknown"
                slot = {"id": str(job_id), "repo": repo, "lane": lane if lane in self.config.lanes else "unknown",
                        "phase": str(entry.get("phase", "unknown"))[:80], "vm": str(entry.get("vm", "unknown"))[:100]}
                slots.append(slot); occupied.append((str(job_id), slot))
            merged = {key: dict(value) for key, value in jobs.items()}
            for job_id, slot in occupied:
                if job_id in merged: merged[job_id]["phase"] = slot["phase"]
                else:
                    merged[job_id] = {"id": job_id, "repo": slot["repo"], "name": "Local reservation", "workflow": "", "status": "unknown",
                        "conclusion": None, "started_at": None, "completed_at": None, "duration_seconds": None, "url": None,
                        "lane": slot["lane"] if slot["lane"] != "unknown" else None, "phase": slot["phase"], "steps": []}
            usage = shutil.disk_usage(self.config.state_dir)
            coverage = getattr(self, "repo_coverage", {})
            truncated = [repo for repo in self.config.repositories if coverage.get(repo, {}).get("truncated", False)]
            stale = sorted(errors)
            github_ok = not stale
            return {"updated_at": iso_now(), "github": {"ok": github_ok, "error": "GitHub API unavailable; check the connection and Actions permissions" if stale else None,
                    "fetched_at": fetched, "repos": [{"repo": repo, "fetched_at": self.repo_fetched[repo], "stale": repo in stale,
                    "error": errors.get(repo)} for repo in self.config.repositories]},
                "fleet": {"capacity": self.config.max_vms, "occupied": len(ledger), "disk_free_gb": round(usage.free / 1024**3, 2),
                    "disk_total_gb": round(usage.total / 1024**3, 2), "slots": slots, "error": ledger_error},
                "jobs": list(merged.values()), "coverage": {"repos": len(self.config.repositories), "runs_per_repo": 10,
                    "truncated": bool(truncated), "truncated_repos": truncated}, "owner": self.config.owner}


def make_handler(dashboard, static_dir):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in ("127.0.0.1:{}".format(self.server.server_port), "localhost:{}".format(self.server.server_port)):
                self.send_error(403); return
            path = urlparse(self.path).path
            if path == "/api/dashboard": body, kind = json.dumps(dashboard.data()).encode(), "application/json; charset=utf-8"
            elif path in ("/", "/index.html"):
                index = Path(static_dir) / "index.html"
                if not index.is_file(): self.send_error(404); return
                body, kind = index.read_bytes(), "text/html; charset=utf-8"
            else: self.send_error(404); return
            self.send_response(200); self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer"); self.send_header("Cache-Control", "no-store"); self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        def log_message(self, *args): pass
    return Handler


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True); parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # The controller config and personal_ci package live in the same checkout.
    sys.path.insert(0, str(Path(args.config).expanduser().resolve().parent))
    from personal_ci.config import Config
    from personal_ci.github import AppTokenProvider, HttpTransport, read_token
    config = Config.load(args.config)
    credential = (AppTokenProvider(config.owner, config.repositories, config.github_app.app_id, config.github_app.private_key_file)
                  if config.github_app else read_token(config.token_file))
    dashboard = Dashboard(config, HttpTransport(credential))
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(dashboard, Path(__file__).parent / "static")).serve_forever()


if __name__ == "__main__": main()
