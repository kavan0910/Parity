"""Shared builders for the M5 tests (equivalence, approvals, review UI): an approved project with a two-slice plan,
hand-written characterization cases, two in-process stdlib services (legacy, new) and a target_code git repo."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from parity_migrate.migrate.casestore import save_case
from parity_migrate.migrate.harness_runner import RunnerConfig, apply_masks, observe
from parity_migrate.migrate.ids import allocate
from parity_migrate.migrate.models import (
    CharacterizationCase, Expectation, HttpRequest, MigrationPlan, Slice,
)
from parity_migrate.migrate.paths import Phase2Layout
from parity_migrate.migrate.state import atomic_write_json, transition

from phase2_helpers import approved_project

AGENT = ("parity", "agent@localhost")
HUMAN = ("Bob Human", "bob@example.com")
WALK = ("tests_recorded", "generating", "built", "tests_passing", "reviewed", "awaiting_human")


class Svc:
    """mode: legacy | new | regressed. Noise: `ts` on /api/items. Seeded regression (regressed): /api/price total
    11 instead of 10. Quirk: legacy /api/quirk answers 200 cancelled; new answers 409 (the approved fix).
    /api/unrec differs between legacy and new on `v` and `w` (unrecorded case -> unknown)."""

    def __init__(self, mode: str = "legacy", extra: dict | None = None):
        self.mode, self.extra = mode, extra or {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._httpd.daemon_threads = True
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    def respond(self, path: str):
        legacy = self.mode == "legacy"
        if path in self.extra:
            return self.extra[path][0 if legacy else 1]
        if path == "/api/items":
            return 200, {"items": [{"id": 1, "name": "pen"}], "ts": time.time_ns()}
        if path == "/api/price":
            return 200, {"total": 11 if self.mode == "regressed" else 10}
        if path == "/api/quirk":
            return (200, {"cancelled": True}) if legacy else (409, {"error": "shipped"})
        if path == "/api/unrec":
            return 200, ({"v": 1, "w": "a"} if legacy else {"v": 2, "w": "b"})
        return 404, {"error": "not found"}

    def _handler(self):
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                status, payload = svc.respond(self.path.split("?", 1)[0])
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H


def plan_two_slices(p, blockers1: list[str] | None = None) -> tuple[str, str]:
    s1 = allocate(p, "SLICE", key="entry:one")
    s2 = allocate(p, "SLICE", key="entry:two")
    plan = MigrationPlan(version=1, slices=[
        Slice(id=s1, name="one", kind="entry", node_ids=["EP-0001"], order=0, blockers=blockers1 or []),
        Slice(id=s2, name="two", kind="entry", node_ids=["EP-0002"], order=1, depends_on=[s1])])
    lay = Phase2Layout(p)
    lay.ensure(lay.plan_dir)
    atomic_write_json(lay.plan_json, plan)
    return s1, s2


def make_case(p, slice_id: str, path: str, *, key: str, normalize=(), decision="preserve", spec=None,
              approved_by="alice", decision_ref="") -> CharacterizationCase:
    cid = allocate(p, "CT", key=key)
    expect = Expectation()
    if spec is not None:
        expect = Expectation(source="specified", status=spec[0], body=spec[1], approved_by=approved_by,
                             headers={"content-type": "application/json"})
    c = CharacterizationCase(id=cid, slice=slice_id, covers=["EP-0001"], kind="http",
                             request=HttpRequest(method="GET", path=path), expect=expect,
                             normalize=list(normalize), decision=decision, decision_ref=decision_ref)
    save_case(p, c)
    return c


def record(p, case: CharacterizationCase, legacy_url: str) -> CharacterizationCase:
    """Store the legacy observation as the recorded expectation (masks applied), like `tests record`."""
    obs = observe(case, RunnerConfig(base_url=legacy_url)).observed
    if case.expect.source == "specified":
        new = case.model_copy(update={"expect": case.expect.model_copy(update={
            "legacy_observed": {"status": obs.status, "body": obs.body}})})
    else:
        new = case.model_copy(update={"expect": Expectation(status=obs.status, headers=obs.headers,
                                                            body=apply_masks(obs.body, case.normalize))})
    save_case(p, new)
    return new


def standard_cases(p, s1: str, legacy_url: str) -> dict:
    """items (noise), price (regression target), quirk (approved fix -> intended divergence)."""
    items = record(p, make_case(p, s1, "/api/items", key="items", normalize=["$.ts"]), legacy_url)
    price = record(p, make_case(p, s1, "/api/price", key="price"), legacy_url)
    quirk = record(p, make_case(p, s1, "/api/quirk", key="quirk", decision="fix", spec=(409, {"error": "shipped"}),
                                decision_ref="COMP-0002"), legacy_url)
    return {"items": items, "price": price, "quirk": quirk}


def walk_to_awaiting(p, slice_id: str) -> None:
    for s in WALK:
        transition(p, slice_id, s, actor="agent", reason="test")


def write_iteration(p, slice_id: str, n: int = 1, build=None, tests=None) -> Path:
    d = Phase2Layout(p).slice_iterations(slice_id) / str(n)
    atomic_write_json(d / "build.json", build if build is not None else {"ok": True})
    atomic_write_json(d / "tests.json", tests if tests is not None else {"ok": True, "failed": []})
    return d


def _git_env(author) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME=author[0],
               GIT_COMMITTER_EMAIL=author[1], GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def git(p, *args, author=AGENT) -> str:
    tc = Phase2Layout(p).target_code
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args], cwd=tc,
                       env=_git_env(author), capture_output=True, text=True, check=True)
    return r.stdout


def init_target(p) -> str:
    tc = Phase2Layout(p).target_code
    tc.mkdir(parents=True, exist_ok=True)
    git(p, "init", "-q")
    return commit(p, "app.py", "print('hello')\n", "base", author=AGENT)


def commit(p, rel: str, text: str, msg: str, author=AGENT) -> str:
    f = Phase2Layout(p).target_code / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(p, "add", rel, author=author)
    git(p, "commit", "-q", "-m", msg, author=author)
    return git(p, "rev-parse", "HEAD").strip()


def m5_project(tmp_path, tmp_path_factory, blockers1=None):
    p = approved_project(tmp_path, tmp_path_factory)
    s1, s2 = plan_two_slices(p, blockers1)
    return p, s1, s2
