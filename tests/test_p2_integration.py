"""Phase 2 integration seams: in-network case execution (harness.run_cases_in_network) used by the recorder,
`tests run`, equivalence (slice and composite) and the generation loop; fix-request lifecycle; recorder slice
state; iteration-file contract between slice_runner and approvals; end-to-end CLI flow."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import equivalence as eq
from migration_agent.migrate import harness as H
from migration_agent.migrate import slice_runner as sr
from migration_agent.migrate.approvals import approval_blockers, iterations, load_approval
from migration_agent.migrate.exec.fake import FakeExecBackend
from migration_agent.migrate.exec.policy import scan_job
from migration_agent.migrate.harness_runner import netrunner, wire
from migration_agent.migrate.models import CharacterizationCase, Expectation, HttpRequest
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import load_state, transition

from p2_exec_helpers import IMG_ID, FakeDocker, flag_values
from p2_integration_helpers import fake_up, runner_fake
from p2_m4_helpers import TARGET_YAML, m4_project, passing_suite, ready_slice
from p2_m5_helpers import Svc, m5_project, make_case, standard_cases, walk_to_awaiting
from phase2_helpers import approved_project


def _cli(*args):
    return CliRunner().invoke(main, [str(a) for a in args])


def _case(cid: str, path: str, **expect) -> CharacterizationCase:
    return CharacterizationCase(id=cid, kind="http", request=HttpRequest(method="GET", path=path),
                                expect=Expectation(**expect) if expect else Expectation(),
                                normalize=["$.ts"] if path == "/api/items" else [])


@pytest.fixture
def m5(tmp_path, tmp_path_factory):
    return m5_project(tmp_path, tmp_path_factory)


# ------------------------------------------------------------------ run_cases_in_network
def test_run_cases_in_network_argv_mounts_network_and_results(m5, monkeypatch):
    p, *_ = m5
    monkeypatch.setattr(H, "replay_seeds", lambda *a, **k: {"requests": 0, "ok": True})   # seeds: test_p2_seed.py
    with Svc("legacy") as legacy:
        fd = runner_fake(monkeypatch, {"http://legacy:3000": legacy.base_url})
        H.import_source(p, from_original=True)
        assert H.build(p, "legacy")["ok"]
        up = H.up(p, "legacy", sleep=lambda s: None)["up"]
        assert H.service_urls(p)["legacy"] == "http://legacy:3000"
        cases = [_case("CT-0101", "/api/price", status=200, body={"total": 10},
                       headers={"content-type": "application/json"}),
                 _case("CT-0102", "/api/price", status=200, body={"total": 99},
                       headers={"content-type": "application/json"}),
                 _case("CT-0103", "/api/items", status=200, body={"items": [{"id": 1, "name": "pen"}],
                                                                  "ts": "<masked>"},
                       headers={"content-type": "application/json"})]
        res = H.run_cases_in_network(p, "legacy", cases)
        obs = H.run_cases_in_network(p, "legacy", cases[:1], compare=False)
    assert [(r.case_id, r.outcome) for r in res] == [("CT-0101", "pass"), ("CT-0102", "fail"), ("CT-0103", "pass")]
    assert [d.field for d in res[1].diffs] == ["body.$.total"]
    assert obs[0].outcome == "unrecorded" and obs[0].observed.status == 200
    assert set(obs[0].observed.headers) == {"content-type"}            # allowlisted on the host
    # the docker argv of the runner job: internal network of the legacy stack, pinned image, read-only mounts
    a = fd.runner_runs[0]
    assert flag_values(a, "--network") == [up["network"]] and up["network"].startswith("ma-int-")
    assert a[a.index("--pull") + 1] == "never" and "--read-only" in a and "ma.phase=test" in flag_values(a, "--label")
    assert a[a.index("--entrypoint") + 1] == "python" and a[a.index("--entrypoint") + 2] == IMG_ID
    assert a[a.index("--base-url") + 1] == "http://legacy:3000" and a[a.index("--out") + 1] == "/reports/results.json"
    mounts = {m.split("target=")[1].split(",")[0]: m for m in flag_values(a, "--mount")}
    assert mounts["/runner"].endswith(",readonly") and mounts["/cases"].endswith(",readonly")
    assert not mounts["/reports"].endswith(",readonly")
    run_dir = fd.mount_target_host(a, "/runner").parent.parent
    assert run_dir.parent == (p.root / "runs").resolve() and fd.mount_target_host(a, "/runner").name == "runner"
    assert not any("legacy_src" in m or "target_code" in m for m in mounts.values())
    staged = json.loads((fd.mount_target_host(a, "/cases") / "cases.json").read_text())
    assert [set(c) for c in staged] == [{"id", "kind", "request"}] * 3      # expectations stay on the host
    # job recorded like any exec run, passes the static policy, results copied to runs/RUN-NNNN/reports/
    meta = json.loads((run_dir / "meta.json").read_text())
    from migration_agent.migrate.models import ExecJob
    job = ExecJob.model_validate(meta["job"])
    assert job.phase == "test" and job.network == "none" and job.image == H.RUNNER_IMAGE
    assert scan_job(job, p.root) == []
    assert meta["result"]["reports"] == [f"runs/{run_dir.name}/reports/results.json"]
    assert json.loads((run_dir / "reports" / "results.json").read_text())["results"][0]["case_id"] == "CT-0101"
    audit = (p.root / "audit" / "audit.jsonl").read_text()
    assert '"harness_cases"' in audit


def test_run_cases_in_network_refusals_and_bad_output(m5, monkeypatch):
    p, *_ = m5
    case = _case("CT-0101", "/api/price", status=200)
    fd = runner_fake(monkeypatch)
    with pytest.raises(H.HarnessError, match="not up"):
        H.run_cases_in_network(p, "new", [case])
    with pytest.raises(H.HarnessError, match="unknown harness role"):
        H.run_cases_in_network(p, "db", [case])
    fake_up(p, fd, "legacy", "legacy", 3000)
    assert H.run_cases_in_network(p, "legacy", []) == []
    with pytest.raises(H.HarnessError, match="cannot attach"):
        H.run_cases_in_network(p, "legacy", [case], backend=FakeExecBackend(p))
    # runner image not present locally: never pulled, the run is an error
    nofd = FakeDocker()
    nofd.nets.update(fd.nets)
    with pytest.raises(H.HarnessError, match="did not run.*not present locally"):
        H.run_cases_in_network(p, "legacy", [case], backend=nofd.backend(p))

    # untrusted output: garbage file -> HarnessError; a malformed observation -> protocol error for that case
    def write(content):
        def on_run(a):
            if "/runner/netrunner.py" in a:
                (fd.mount_target_host(a, "/reports") / "results.json").write_text(content)
        fd.on_run = on_run
    write("not json")
    with pytest.raises(H.HarnessError, match="no usable results.json"):
        H.run_cases_in_network(p, "legacy", [case])
    write(json.dumps({"results": [{"case_id": "CT-0101", "outcome": "unrecorded",
                                   "observed": {"status": "200; rm -rf /", "headers": {}}}]}))
    r = H.run_cases_in_network(p, "legacy", [case, _case("CT-0102", "/x")])
    assert [(x.outcome, x.error_class) for x in r] == [("error", "protocol"), ("error", "protocol")]


def test_staged_runner_code_is_stdlib_only():
    std = set(sys.stdlib_module_names) | {"__future__"}
    for mod in (wire, netrunner):
        tree = ast.parse(Path(mod.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                       # `from . import wire` (package fallback only)
                    continue
                names = [node.module.split(".")[0]]
            else:
                continue
            assert all(n in std or n == "wire" for n in names), (mod.__name__, names)
    assert set(H.RUNNER_FILES) == {"wire.py", "netrunner.py"}
    assert "FROM " + H.RUNNER_IMAGE in (Path(H.__file__).parent / "router" / "Dockerfile").read_text()


# ------------------------------------------------------------------ recorder / tests run
def test_tests_record_harness_cli_records_twice_and_moves_slice_state(m5, monkeypatch):
    p, s1, s2 = m5
    items = make_case(p, s1, "/api/items", key="items")
    make_case(p, s1, "/api/price", key="price")
    with Svc("legacy") as legacy:
        fd = runner_fake(monkeypatch, {"http://legacy:3000": legacy.base_url})
        res = _cli("tests", "record", p.root, "--harness")
        assert res.exit_code != 0 and "not up" in res.output
        fake_up(p, fd, "legacy", "legacy", 3000)
        res = _cli("tests", "record", p.root, "--harness")
        assert res.exit_code == 0, res.output
        assert "recorded 2/2" in res.output and f"slices now tests_recorded: {s1}" in res.output
        assert len(fd.runner_runs) == 2 and all(flag_values(a, "--network")[0].startswith("ma-int-")
                                               for a in fd.runner_runs)
        from migration_agent.migrate.casestore import load_cases
        rec = {c.id: c for c in load_cases(p)}
        assert "$.ts" in rec[items.id].normalize and rec[items.id].expect.source == "recorded"
        assert load_state(p, s1).status == "tests_recorded" and load_state(p, s2).status == "planned"
        rep = json.loads(next((Phase2Layout(p).recordings_dir).glob("RUN-*/report.json")).read_text())
        assert rep["base_urls"] == ["http://legacy:3000"] * 2 and rep["slices_recorded"] == [s1]
        # tolerated: re-recording a slice that is already further along changes nothing
        transition(p, s1, "generating", reason="test")
        res = _cli("tests", "record", p.root, "--harness")
        assert res.exit_code == 0 and "slices now" not in res.output and load_state(p, s1).status == "generating"
        # the in-process path keeps working
        res = _cli("tests", "record", p.root, "--base-url", legacy.base_url)
        assert res.exit_code == 0, res.output
    res = _cli("tests", "record", p.root)
    assert res.exit_code != 0 and "exactly one of --base-url URL or --harness" in res.output


def test_record_marks_slice_only_when_all_cases_recorded(m5, monkeypatch):
    p, s1, _ = m5
    make_case(p, s1, "/api/price", key="price")
    make_case(p, s1, "/api/nope", key="nope")                       # 404 is recorded too
    with Svc("legacy") as legacy:
        from migration_agent.migrate.recorder import record_cases
        rep = record_cases(p, legacy.base_url, case_ids=[make_case(p, s1, "/api/items", key="items").id])
        assert rep.slices_recorded == [] and load_state(p, s1).status == "planned"
        rep = record_cases(p, legacy.base_url)
        assert rep.slices_recorded == [s1] and load_state(p, s1).status == "tests_recorded"


def test_tests_run_harness_roles(m5, monkeypatch):
    p, s1, _ = m5
    with Svc("legacy") as legacy, Svc("regressed") as bad:
        cases = standard_cases(p, s1, legacy.base_url)
        fd = runner_fake(monkeypatch, {"http://legacy:3000": legacy.base_url, "http://new:8000": bad.base_url})
        fake_up(p, fd, "legacy", "legacy", 3000)
        fake_up(p, fd, "new", "new", 8000)
        res = _cli("tests", "run", p.root, "--harness", "--role", "legacy", "--case", cases["items"].id,
                   "--case", cases["price"].id)
        assert res.exit_code == 0, res.output
        res = _cli("tests", "run", p.root, "--harness", "--role", "legacy")    # legacy misses the approved fix
        assert res.exit_code == 1 and f"FAIL {cases['quirk'].id}: status: 409 != 200" in res.output
        res = _cli("tests", "run", p.root, "--harness", "--role", "new")
        assert res.exit_code == 1 and "FAIL" in res.output and "body.$.total" in res.output
        out = json.loads(_cli("tests", "run", p.root, "--harness", "--role", "new", "--format", "json").output)
        assert out["base_url"] == "http://new:8000"
        res = _cli("tests", "run", p.root, "--role", "new")
        assert res.exit_code != 0 and "--role needs --harness" in res.output
        res = _cli("tests", "run", p.root, "--harness", "--role", "composite")
        assert res.exit_code != 0 and "composite harness is not up" in res.output
        res = _cli("tests", "run", p.root, "--base-url", legacy.base_url, "--case", cases["price"].id)
        assert res.exit_code == 0, res.output


# ------------------------------------------------------------------ equivalence
def test_equivalence_runs_in_network_when_urls_are_omitted(m5, monkeypatch):
    p, s1, _ = m5
    with Svc("legacy") as legacy, Svc("regressed") as bad:
        cases = standard_cases(p, s1, legacy.base_url)
        fd = runner_fake(monkeypatch, {"http://legacy:3000": legacy.base_url, "http://new:8000": bad.base_url})
        res = _cli("equivalence", p.root, "--slice", s1, "--offline")
        assert res.exit_code != 0 and "missing: legacy, new" in res.output
        fake_up(p, fd, "legacy", "legacy", 3000)
        fake_up(p, fd, "new", "new", 8000)
        res = _cli("equivalence", p.root, "--slice", s1, "--offline")
        assert res.exit_code == 1 and "1 regression(s)" in res.output, res.output
        assert len(fd.runner_runs) == 2                                 # one runner job per role
        meta = eq.load_run_meta(p, s1)
        assert (meta.legacy_mode, meta.new_mode) == ("harness", "harness")
        assert (meta.legacy_url, meta.new_url) == ("http://legacy:3000", "http://new:8000")
        rep = eq.load_report(p, s1)
        assert [(d.case_id, d.field) for d in rep.unexplained] == [(cases["price"].id, "body.$.total")]
        # mixed: explicit legacy URL, new in-network
        rep = eq.run_equivalence(p, s1, legacy.base_url, None)
        assert eq.load_run_meta(p, s1).legacy_mode == "url" and len(rep.unexplained) == 1


def test_equivalence_composite_after_flip(m5, monkeypatch):
    p, s1, s2 = m5
    with Svc("legacy") as legacy, Svc("new") as new, Svc("regressed") as bad:
        cases = standard_cases(p, s1, legacy.base_url)
        q = cases["quirk"]                                              # like `tests record`: with headers
        from migration_agent.migrate.casestore import save_case
        save_case(p, q.model_copy(update={"expect": q.expect.model_copy(update={"legacy_observed": {
            **q.expect.legacy_observed, "headers": {"content-type": "application/json"}}})}))
        make_case(p, s2, "/api/price", key="price2")                    # s2 is not approved: not run
        fd = runner_fake(monkeypatch, {"http://router:8080": new.base_url})
        res = _cli("equivalence", "composite", p.root)
        assert res.exit_code != 0 and "composite harness is not up" in res.output
        walk_to_awaiting(p, s1)
        transition(p, s1, "approved", actor="human:alice", reason="ok")
        fake_up(p, fd, "composite", "router", 8080)
        H.flips_path(p).write_text(json.dumps({"flips": {s1: {"by": "alice"}}}))
        res = _cli("equivalence", "composite", p.root)
        assert res.exit_code == 0, res.output
        comp = eq.load_composite(p)
        assert comp.flipped == [s1] and comp.slices == [s1] and comp.totals == {"pass": 3} and comp.ok
        quirk = next(c for c in comp.cases if c.case_id == cases["quirk"].id)
        assert (quirk.upstream, quirk.expected_from) == ("new", "expect")      # approved fix spec (409)
        # unflipped: the router answers with legacy; the fix case is compared with the recorded legacy output
        H.flips_path(p).write_text(json.dumps({"flips": {}}))
        fd.urls["http://router:8080"] = legacy.base_url
        rep = eq.run_composite(p)
        assert rep.ok and {c.upstream for c in rep.cases} == {"legacy"}
        assert next(c for c in rep.cases if c.case_id == cases["quirk"].id).expected_from == "legacy_observed"
        # a regression behind the router fails the composite suite
        H.flips_path(p).write_text(json.dumps({"flips": {s1: {"by": "alice"}}}))
        fd.urls["http://router:8080"] = bad.base_url
        res = _cli("equivalence", "composite", p.root)
        assert res.exit_code == 1 and f"FAIL {cases['price'].id}" in res.output
    lay = Phase2Layout(p)
    hist = [json.loads(x) for x in (lay.equivalence_dir / "composite_history.jsonl").read_text().splitlines()]
    assert [h["ok"] for h in hist] == [True, True, False]
    assert "## Composite (strangler router)" in lay.equivalence_report_md.read_text()


def test_harness_flip_cli_runs_the_composite_suite_when_up(m5, monkeypatch):
    p, s1, _ = m5
    monkeypatch.setattr(H, "flip", lambda project, sid, **kw: {"routes": [1], "forced": False, "warnings": []})
    with Svc("legacy") as legacy, Svc("regressed") as bad:
        standard_cases(p, s1, legacy.base_url)
        walk_to_awaiting(p, s1)
        transition(p, s1, "approved", actor="human:alice", reason="ok")
        fd = runner_fake(monkeypatch, {"http://router:8080": bad.base_url})
        res = _cli("harness", "flip", p.root, s1)
        assert res.exit_code == 0 and "composite harness not up" in res.output, res.output
        fake_up(p, fd, "composite", "router", 8080)
        res = _cli("harness", "flip", p.root, s1, "--no-verify")
        assert res.exit_code == 0 and not fd.runner_runs
        res = _cli("harness", "flip", p.root, s1)
        assert res.exit_code == 1 and "composite: 3 case(s)" in res.output and "FAIL" in res.output
        assert len(fd.runner_runs) == 1 and eq.load_composite(p) is not None


# ------------------------------------------------------------------ slice runner suite
def test_harness_suite_builds_starts_and_runs_cases_in_network(m5, monkeypatch):
    p, s1, _ = m5
    calls = []
    monkeypatch.setattr(H, "load_spec", lambda project, adopt=False: object())
    monkeypatch.setattr(H, "resolve_service", lambda project, spec, role: None)
    monkeypatch.setattr(H, "service_urls", lambda project: {"new": None})
    monkeypatch.setattr(H, "build", lambda project, role, backend=None: calls.append(("build", role)) or {"ok": True})
    monkeypatch.setattr(H, "up", lambda project, role, backend=None: calls.append(("up", role)) or {})
    monkeypatch.setattr(H, "down", lambda project, role, backend=None: calls.append(("down", role)) or {})
    from migration_agent.migrate.harness_runner import CaseResult

    def fake_run(project, role, cases, *, backend=None, compare=True, **kw):
        calls.append(("run", role, compare))
        return [CaseResult(case_id=c.id, kind=c.kind, outcome="pass") for c in cases]
    monkeypatch.setattr(H, "run_cases_in_network", fake_run)
    backend = type("B", (), {"start_service": lambda *a, **k: "c", "name": "docker"})()
    case = _case("CT-0101", "/api/price", status=200)
    res = sr.harness_suite(p, None, [case], backend=backend)
    assert res.status == "passed" and res.totals == {"pass": 1}
    assert calls == [("build", "new"), ("up", "new"), ("run", "new", True), ("down", "new")]

    def boom(*a, **k):
        raise H.HarnessError("runner job RUN-0009 did not run")
    monkeypatch.setattr(H, "run_cases_in_network", boom)
    res = sr.harness_suite(p, None, [case], backend=backend)
    assert res.status == "skipped" and "RUN-0009" in res.reason
    assert calls[-1] == ("down", "new")                       # stopped again even when the runner failed


# ------------------------------------------------------------------ iteration-file contract
def test_iteration_files_written_by_slice_runner_are_what_approvals_reads(tmp_path, tmp_path_factory):
    from migration_agent.migrate.approvals import _green, latest_iteration
    from migration_agent.migrate.codegen import OfflineCodegenLLM
    from migration_agent.migrate.exec.fake import FakeOutcome
    p = m4_project(tmp_path, tmp_path_factory)
    sid = ready_slice(p).id

    def gen(suite, backend=None, **opts):
        return sr.generate(p, slice_id=sid, llm=OfflineCodegenLLM(), backend=backend or FakeExecBackend(p),
                           suite_runner=suite, options=sr.GenerateOptions(**opts)).outcomes[0]
    o = gen(passing_suite)
    assert o.status == "awaiting_human"
    it = latest_iteration(p, sid)
    build, tests = (json.loads((it / f"{n}.json").read_text()) for n in ("build", "tests"))
    assert isinstance(build["ok"], bool) and "exit_code" in build
    assert {"ok", "status", "result", "failed"} <= set(tests) and tests["failed"] == []
    assert iterations(p, sid) == [{"iteration": 1, "files": sorted(x.name for x in it.glob("*.json")),
                                   "build": True, "tests": True}]
    assert not [b for b in approval_blockers(p, sid) if "iteration" in b]

    # failing characterization suite: `failed` lists the CT ids; approvals allows only intended divergences
    failing = lambda project, slice_, cases, *, backend: sr.SuiteResult(  # noqa: E731
        status="failed", failures=[{"case_id": "CT-0042", "outcome": "fail", "error": "", "diffs": []}])
    transition(p, sid, "generating", reason="test")
    o = gen(failing, max_iterations=1)
    assert o.status == "blocked"
    tests = json.loads((latest_iteration(p, sid) / "tests.json").read_text())
    assert tests["failed"] == ["CT-0042"] and tests["ok"] is False
    assert not _green(tests, set()) and _green(tests, {"CT-0042"})
    # failing build: build.json is red for approvals too
    p2 = m4_project(tmp_path / "b", tmp_path_factory)
    sid2 = ready_slice(p2).id
    o = sr.generate(p2, slice_id=sid2, llm=OfflineCodegenLLM(), suite_runner=passing_suite,
                    backend=FakeExecBackend(p2, script=[FakeOutcome(exit_code=2, stderr="boom")]),
                    options=sr.GenerateOptions(max_iterations=1)).outcomes[0]
    assert o.status == "blocked" and iterations(p2, sid2)[-1]["build"] is False
    assert any("build of iteration" in b for b in approval_blockers(p2, sid2))


# ------------------------------------------------------------------ end to end (CLI)
def _e2e_project(tmp_path, tmp_path_factory, monkeypatch):
    p = approved_project(tmp_path, tmp_path_factory)
    res = _cli("plan", p.root, "--offline")                         # target-agnostic plan (no fit report)
    assert res.exit_code == 0, res.output
    lay = Phase2Layout(p)
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text(TARGET_YAML)
    monkeypatch.setattr(sr, "harness_suite", passing_suite)          # injected passing characterization suite
    import migration_agent.migrate.exec as ex
    monkeypatch.setattr(ex, "get_backend", lambda project=None: FakeExecBackend(project))
    sid = ready_slice(p).id
    case = make_case(p, sid, "/api/price", key="e2e-price")
    return p, sid, case


def test_e2e_plan_generate_equivalence_approve_via_cli(tmp_path, tmp_path_factory, monkeypatch):
    p, sid, _case_ = _e2e_project(tmp_path, tmp_path_factory, monkeypatch)
    with Svc("legacy") as legacy, Svc("new") as new:
        res = _cli("tests", "record", p.root, "--base-url", legacy.base_url)
        assert res.exit_code == 0 and f"slices now tests_recorded: {sid}" in res.output, res.output
        res = _cli("generate", p.root, "--slice", sid, "--offline")
        assert res.exit_code == 0 and f"{sid}: ran -> awaiting_human" in res.output, res.output
        res = _cli("approve-slice", p.root, sid, "--by", "alice")
        assert res.exit_code != 0 and "no equivalence report" in res.output
        res = _cli("equivalence", p.root, "--slice", sid, "--legacy-url", legacy.base_url,
                   "--new-url", new.base_url, "--offline")
        assert res.exit_code == 0 and "100.00%" in res.output, res.output
    res = _cli("approve-slice", p.root, sid, "--by", "alice")
    assert res.exit_code == 0, res.output
    assert load_state(p, sid).status == "approved"
    appr = load_approval(p, sid)
    assert appr is not None and appr.approved_by == "alice" and appr.commit_sha
    assert Phase2Layout(p).slice_approval(sid).is_file()
    assert any(k.endswith("tests.json") for k in appr.report_hashes)


def test_fix_request_drives_next_iteration_then_done_and_approval(tmp_path, tmp_path_factory, monkeypatch):
    p, sid, case = _e2e_project(tmp_path, tmp_path_factory, monkeypatch)
    lay = Phase2Layout(p)
    with Svc("legacy") as legacy, Svc("regressed") as bad, Svc("new") as good:
        assert _cli("tests", "record", p.root, "--base-url", legacy.base_url).exit_code == 0
        assert _cli("generate", p.root, "--slice", sid, "--offline").exit_code == 0
        res = _cli("equivalence", p.root, "--slice", sid, "--legacy-url", legacy.base_url, "--new-url", bad.base_url,
                   "--offline")
        assert res.exit_code == 1
        fr = eq.load_fix_requests(p, sid).requests
        assert [(r.case_id, r.field, r.status) for r in fr] == [(case.id, "body.$.total", "pending")]
        eq_id = fr[0].eq_id
        res = _cli("approve-slice", p.root, sid, "--by", "alice")
        assert res.exit_code != 0 and "1 pending fix request(s): " + eq_id in res.output
        # the next generate run reopens the slice for a fix iteration that sees the request as a failure
        res = _cli("generate", p.root, "--slice", sid, "--offline")
        assert res.exit_code == 0 and "iterations 2" in res.output, res.output
        pack = json.loads((lay.slice_context(sid) / "iter-2.json").read_text())
        failures = json.dumps(pack["sections"].get("failures", ""))
        assert eq_id in failures and "body.$.total" in failures
        meta = json.loads((lay.slice_iterations(sid) / "2" / "prompt_meta.json").read_text())
        assert meta["attempts"][0]["template"].startswith("codegen_fix")
        fr = eq.load_fix_requests(p, sid).requests
        assert [(r.eq_id, r.status) for r in fr] == [(eq_id, "done")] and "iteration 2" in fr[0].note
        assert (json.loads((lay.slice_dir(sid) / "loop.json").read_text())["fix_requests"]["2"] == [eq_id])
        # nothing pending: a further generate run has nothing to do
        assert "awaiting human review" in _cli("generate", p.root, "--slice", sid, "--offline").output
        # HEAD moved: approval needs a fresh equivalence run, which is now clean
        assert any("re-run equivalence" in b or "regression" in b for b in approval_blockers(p, sid))
        res = _cli("equivalence", p.root, "--slice", sid, "--legacy-url", legacy.base_url, "--new-url", good.base_url,
                   "--offline")
        assert res.exit_code == 0, res.output
    assert [r.status for r in eq.load_fix_requests(p, sid).requests] == ["done"]
    res = _cli("approve-slice", p.root, sid, "--by", "alice")
    assert res.exit_code == 0, res.output
    assert load_state(p, sid).status == "approved"


def test_fix_requests_do_not_reopen_approved_slices(tmp_path, tmp_path_factory, monkeypatch):
    p, sid, case = _e2e_project(tmp_path, tmp_path_factory, monkeypatch)
    for s in ("tests_recorded", "generating", "built", "tests_passing", "reviewed", "awaiting_human", "approved"):
        transition(p, sid, s, reason="test")
    eq.add_fix_request(p, sid, eq_id="EQ-0100", case_id=case.id, field="status")
    res = _cli("generate", p.root, "--slice", sid, "--offline")
    assert "already approved (1 pending fix request(s)" in res.output
    assert load_state(p, sid).status == "approved"


# ------------------------------------------------------------------ live (real daemon only)
def _daemon_ok() -> bool:
    import shutil
    import subprocess
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _daemon_ok(), reason="docker daemon not available (docker info failed)")
def test_live_runner_job_reaches_a_service_on_the_internal_network_only(m5):
    from migration_agent.migrate.exec.docker import DockerBackend
    from migration_agent.migrate.models import ExecJob, Limits
    p, *_ = m5
    b = DockerBackend(p)
    if b.docker(["image", "inspect", H.RUNNER_IMAGE]).returncode != 0:
        pytest.skip(f"{H.RUNNER_IMAGE} not present locally (pulls are disabled)")
    net = b.ensure_internal_network("legacy")
    svc = None
    try:
        job = ExecJob(run_id="RUN-0900", image=H.RUNNER_IMAGE, phase="test", workdir="/tmp",
                      command=["python", "-m", "http.server", "8000", "--directory", "/tmp"],
                      limits=Limits(cpus=0.5, memory_mb=128, pids=64, timeout_s=300, disk_mb=16))
        svc = b.start_service(job, network=net, aliases=["legacy"])
        st = H.load_state(p)
        st["legacy"] = {"ok": True, "up": {"network": net, "containers": [svc], "alias": "legacy", "port": 8000}}
        H.save_state(p, st)
        import time
        res = []
        for _ in range(10):                                      # give the server a moment to bind
            res = H.run_cases_in_network(p, "legacy", [_case("CT-0001", "/")], backend=b, compare=False)
            if res and res[0].outcome == "unrecorded":
                break
            time.sleep(1)
        assert res[0].outcome == "unrecorded" and res[0].observed.status == 200
        # the same runner cannot leave the internal network
        st["legacy"]["up"]["alias"] = "example.com"
        st["legacy"]["up"]["port"] = 80
        H.save_state(p, st)
        out = H.run_cases_in_network(p, "legacy", [_case("CT-0002", "/")], backend=b, compare=False)
        assert out[0].outcome == "error"
    finally:
        if svc:
            b.remove_containers([svc])
        b.remove_network(net)
