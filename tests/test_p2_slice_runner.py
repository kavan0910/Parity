"""M4 generation loop (migrate/slice_runner.py, migrate/targetrepo.py): state machine, fix loop, budget caps,
resume, dry run, refusals, git commits and human-commit detection (docs/phase2-plan.md 5.1, 5.5)."""
import json
import subprocess

import pytest
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import slice_runner as sr
from migration_agent.migrate import targetrepo
from migration_agent.migrate.codegen import OfflineCodegenLLM
from migration_agent.migrate.exec.fake import FakeExecBackend, FakeOutcome
from migration_agent.migrate.ledger import spent
from migration_agent.migrate.models import CharacterizationCase, SliceState
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import load_state, save_state, transition

from p2_m4_helpers import ScriptLLM, m4_project, passing_suite, plan, ready_slice, reply, tree_snapshot


@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return m4_project(tmp_path, tmp_path_factory)


def run(p, sid, llm=None, backend=None, suite=passing_suite, **opts):
    return sr.generate(p, slice_id=sid, llm=llm or OfflineCodegenLLM(), backend=backend or FakeExecBackend(p),
                       suite_runner=suite, options=sr.GenerateOptions(**opts))


def git(p, *args):
    return subprocess.run(["git", "-C", str(Phase2Layout(p).target_code), *args], capture_output=True, text=True,
                          check=True).stdout


def entry_with_recorded_case(p):
    from migration_agent.migrate.casestore import save_case
    entry = next(s for s in plan(p).slices if s.kind == "entry")
    for d in entry.depends_on:                                      # pretend dependencies are approved
        save_state(p, SliceState(slice_id=d, status="approved"))
    save_case(p, CharacterizationCase.model_validate({"id": "CT-0001", "slice": entry.id, "kind": "http",
                                                     "request": {"method": "GET", "path": "/api/x"},
                                                     "expect": {"status": 200}}))
    return entry


def failing_build(job):
    return FakeOutcome(exit_code=1, stderr="error TS2304: Cannot find name 'x'") if job.phase == "build" \
        else FakeOutcome()


# ------------------------------------------------------------------ happy path
def test_offline_loop_reaches_awaiting_human(proj):
    sid = ready_slice(proj).id
    fb = FakeExecBackend(proj)
    rep = run(proj, sid, backend=fb)
    o = rep.outcomes[0]
    assert o.action == "ran" and o.status == "awaiting_human" and o.iterations == 1 and o.model_calls == 2
    st = load_state(proj, sid)
    assert [h.to_status for h in st.history] == ["tests_recorded", "generating", "built", "tests_passing",
                                                 "reviewed", "awaiting_human"]
    it = Phase2Layout(proj).slice_iterations(sid) / "1"
    for name in ("prompt_meta.json", "edits.json", "build.json", "tests.json", "review.json"):
        assert (it / name).exists(), name
    edits = json.loads((it / "edits.json").read_text())
    assert edits["status"] == "applied" and edits["changed"] == [f"migration/{sid}.md"]
    meta = json.loads((it / "prompt_meta.json").read_text())
    assert meta["attempts"][0]["prompt_version"] == "codegen.v1" and meta["attempts"][0]["total_tokens"] > 0
    # build in the mirror phase, unit tests offline, both on a workspace copy (never target_code/ itself)
    assert [(j.phase, j.network) for j in fb.jobs] == [("build", "mirror"), ("test", "none")]
    assert all(m.source.startswith("runs/") for j in fb.jobs for m in j.mounts)
    # git: one agent commit + tag per iteration
    log = git(proj, "log", "--format=%an <%ae>|%s")
    assert log.splitlines()[0].startswith("migration-agent <agent@localhost>|" + sid)
    assert f"slice/{sid}/iter-1" in git(proj, "tag")
    assert git(proj, "config", "core.hooksPath").strip() == "/dev/null"
    # audit: spans per step tagged with the slice, model calls attributed to the slice
    audit = [json.loads(x) for x in (proj.audit_dir / "audit.jsonl").read_text().splitlines()]
    steps = {e.get("step") for e in audit if e.get("action") == "generate_step" and e.get("slice") == sid}
    assert {"codegen", "build", "tests", "review"} <= steps
    assert sid in spent(proj).by_slice
    assert sum(1 for e in audit if e["action"] == "llm_call" and e.get("slice") == sid) == 2


def test_default_suite_is_skipped_without_new_service(proj):
    sid = entry_with_recorded_case(proj).id
    o = sr.generate(proj, slice_id=sid, llm=OfflineCodegenLLM(), backend=FakeExecBackend(proj)).outcomes[0]
    assert o.action == "stopped" and o.status == "built" and "skipped" in o.reason
    tests = json.loads((Phase2Layout(proj).slice_iterations(sid) / "1" / "tests.json").read_text())
    assert tests["result"] == "skipped" and tests["characterization"]["status"] == "skipped"
    assert tests["ok"] is False and "failed" not in tests                 # never reads as green


def test_backend_unavailable_stops_without_host_execution(proj):
    sid = ready_slice(proj).id
    fb = FakeExecBackend(proj, available=(False, "no docker"))
    o = run(proj, sid, backend=fb).outcomes[0]
    assert o.action == "stopped" and o.status == "generating" and "no docker" in o.reason and not fb.jobs


# ------------------------------------------------------------------ fix loop, retries, caps
def test_fix_loop_then_blocked_at_cap(proj):
    sid = ready_slice(proj).id
    llm = ScriptLLM(codegen=lambda prompt, n: reply(f"src/f{n}.ts", f"export const v = {n};\n"))
    o = run(proj, sid, llm=llm, backend=FakeExecBackend(proj, script=failing_build), max_iterations=3).outcomes[0]
    assert o.status == "blocked" and o.iterations == 3 and "--max-iterations 3" in o.reason
    assert llm.n("codegen") == 3
    fix_prompt = [c for c in llm.calls if c["task"] == "codegen"][1]
    assert "<<<FAILURES_BEGIN>>>" in fix_prompt["prompt"] and "TS2304" in fix_prompt["prompt"]
    meta = json.loads((Phase2Layout(proj).slice_iterations(sid) / "2" / "prompt_meta.json").read_text())
    assert meta["attempts"][0]["prompt_version"] == "codegen_fix.v1"
    assert len(git(proj, "tag", "--list", f"slice/{sid}/*").split()) == 3
    # a blocked slice is refused afterwards
    again = run(proj, sid, llm=llm).outcomes[0]
    assert again.action == "refused" and "blocked" in again.reason and llm.n("codegen") == 3


def test_mismatched_edit_is_retried_with_the_failure_message(proj):
    sid = ready_slice(proj).id
    replies = [reply("src/a.ts", op="edit", edits=[{"search": "does not exist", "replace": "x"}]),
               reply("src/a.ts", "export const a = 1;\n")]
    llm = ScriptLLM(codegen=replies)
    o = run(proj, sid, llm=llm).outcomes[0]
    assert o.status == "awaiting_human" and llm.n("codegen") == 2
    second = [c for c in llm.calls if c["task"] == "codegen"][1]["prompt"]
    assert "does not exist; use op 'create'" in second.split("<<<REJECTION_BEGIN>>>")[1]
    edits = json.loads((Phase2Layout(proj).slice_iterations(sid) / "1" / "edits.json").read_text())
    assert [a["outcome"] for a in edits["attempts"]] == ["rejected", "accepted"]


def test_secret_in_output_fails_and_is_never_stored(proj):
    sid = ready_slice(proj).id
    bad = reply("src/k.ts", "export const k = 'AKIAABCDEFGHIJKLMNOP';\n")
    llm = ScriptLLM(codegen=[bad])
    o = run(proj, sid, llm=llm, max_iterations=1).outcomes[0]
    assert o.status == "blocked" and llm.n("codegen") == 3                 # initial + 2 retries, then rejected
    it = Phase2Layout(proj).slice_iterations(sid) / "1"
    text = (it / "edits.json").read_text()
    assert "AKIAABCDEFGHIJKLMNOP" not in text and "looks like a secret" in text
    assert not (Phase2Layout(proj).target_code / "src" / "k.ts").exists()


def test_need_round_answers_from_code(proj):
    sid = ready_slice(proj).id
    f = next(x for x in (proj.code / "src").rglob("*.js")).relative_to(proj.code).as_posix()
    replies = [json.dumps({"files": [], "need": [f"{f}:1-5"]}), reply("src/n.ts", "export {};\n")]
    llm = ScriptLLM(codegen=replies)
    assert run(proj, sid, llm=llm).outcomes[0].status == "awaiting_human"
    second = [c for c in llm.calls if c["task"] == "codegen"][1]["prompt"]
    assert f"=== {f}:1-5 (lines 1-" in second


def test_blocking_review_finding_feeds_fix_loop(proj):
    sid = ready_slice(proj).id
    replies = [reply("src/x.py", "data = eval(user_input)\n"), reply("src/x.py", "data = int(user_input)\n",
                                                                     op="replace")]
    llm = ScriptLLM(codegen=replies)
    o = run(proj, sid, llm=llm).outcomes[0]
    assert o.status == "awaiting_human" and o.iterations == 2
    fix = [c for c in llm.calls if c["task"] == "codegen"][1]["prompt"]
    assert "Blocking review findings" in fix and "dynamic_exec" in fix
    rv = json.loads((Phase2Layout(proj).slice_iterations(sid) / "1" / "review.json").read_text())
    assert rv["findings"][0]["rule"] == "dynamic_exec"
    review_prompt = [c for c in llm.calls if c["task"] == "code_review"][0]["prompt"]
    assert "<<<LEGACY_BEGIN>>>" not in review_prompt and "eval(user_input)" in review_prompt


def test_per_slice_budget_cap_stops_a_looping_slice(proj):
    sid = ready_slice(proj).id
    llm = ScriptLLM(codegen=lambda prompt, n: reply(f"src/f{n}.ts", "x\n"), cost=0.4)
    o = run(proj, sid, llm=llm, backend=FakeExecBackend(proj, script=failing_build), max_iterations=10,
            max_usd_per_slice=1.0).outcomes[0]
    assert o.status == "blocked" and "per-slice budget" in o.reason
    assert llm.n("codegen") == 3 and spent(proj).by_slice[sid] == pytest.approx(1.2)


def test_run_budget_stops_the_run_but_keeps_state(proj):
    sid = ready_slice(proj).id
    llm = ScriptLLM(codegen=lambda prompt, n: reply(f"src/f{n}.ts", "x\n"), cost=0.5)
    rep = run(proj, sid, llm=llm, backend=FakeExecBackend(proj, script=failing_build), max_iterations=10,
              max_usd=1.0, max_usd_per_slice=100.0)
    assert rep.stopped and "--max-usd" in rep.stopped and rep.outcomes[0].action == "stopped"
    assert load_state(proj, sid).status == "generating" and llm.n("codegen") == 2


def test_default_slice_cap_is_three_times_the_estimate(proj):
    sl = ready_slice(proj)
    o = run(proj, sl.id).outcomes[0]
    assert o.cap_usd == pytest.approx(3 * sl.estimate_usd)


# ------------------------------------------------------------------ resume
def test_resume_after_crash_mid_slice_repeats_no_model_call(proj):
    sid = ready_slice(proj).id
    crashed = {"done": False}

    def crash_on_tests(job):
        if job.phase == "test" and not crashed["done"]:
            crashed["done"] = True
            raise KeyboardInterrupt("killed")
        return FakeOutcome()
    llm1 = ScriptLLM(codegen=[reply("src/a.ts", "export const a = 1;\n")])
    with pytest.raises(KeyboardInterrupt):
        run(proj, sid, llm=llm1, backend=FakeExecBackend(proj, script=crash_on_tests))
    assert load_state(proj, sid).status == "built" and llm1.n("codegen") == 1
    llm2 = ScriptLLM(codegen=[reply("src/a.ts", "export const a = 1;\n")])
    o = run(proj, sid, llm=llm2, backend=FakeExecBackend(proj, script=crash_on_tests)).outcomes[0]
    assert o.status == "awaiting_human" and llm2.n("codegen") == 0 and llm2.n("code_review") == 1


def test_resume_after_crash_right_after_a_model_call_loses_one_call(proj, monkeypatch):
    sid = ready_slice(proj).id
    orig = sr.SliceRunner._write
    state = {"armed": True}

    def flaky_write(self, s, k, name, obj):
        if name == "edits.json" and state["armed"]:
            state["armed"] = False
            raise KeyboardInterrupt("killed between the model reply and edits.json")
        return orig(self, s, k, name, obj)
    monkeypatch.setattr(sr.SliceRunner, "_write", flaky_write)
    llm = ScriptLLM(codegen=[reply("src/a.ts", "export const a = 1;\n")])
    with pytest.raises(KeyboardInterrupt):
        run(proj, sid, llm=llm)
    o = run(proj, sid, llm=llm).outcomes[0]
    assert o.status == "awaiting_human" and llm.n("codegen") == 2      # exactly one call lost


def test_resume_rolls_back_half_applied_edits(proj, monkeypatch):
    sid = ready_slice(proj).id
    orig = targetrepo.commit_iteration
    state = {"armed": True}

    def crash_commit(*a, **k):
        if state["armed"]:
            state["armed"] = False
            raise KeyboardInterrupt("killed before commit")
        return orig(*a, **k)
    monkeypatch.setattr(targetrepo, "commit_iteration", crash_commit)
    llm = ScriptLLM(codegen=[reply("src/a.ts", "export const a = 1;\n")])
    with pytest.raises(KeyboardInterrupt):
        run(proj, sid, llm=llm)
    assert targetrepo.dirty_paths(proj) == ["src/a.ts"]
    o = run(proj, sid, llm=llm).outcomes[0]
    assert o.status == "awaiting_human" and llm.n("codegen") == 1 and targetrepo.dirty_paths(proj) == []


# ------------------------------------------------------------------ dry run, refusals
def test_dry_run_writes_nothing(proj):
    before = tree_snapshot(proj.root)
    rows = sr.dry_run(proj, all_slices=True)
    assert len(rows) == len(plan(proj).slices) and all(r.action == "dry_run" and r.estimate_usd > 0 for r in rows)
    assert any("waits for" in r.reason for r in rows)
    res = CliRunner().invoke(main, ["generate", str(proj.root), "--all", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "nothing written" in res.output and ready_slice(proj).id in res.output
    assert tree_snapshot(proj.root) == before


def test_refusals(proj):
    pl = plan(proj)
    sl = ready_slice(proj)
    # unmet dependency
    dep_sl = next(s for s in pl.slices if s.depends_on)
    o = run(proj, dep_sl.id).outcomes[0]
    assert o.action == "refused" and "not approved" in o.reason and load_state(proj, dep_sl.id).status == "planned"
    # blocked
    transition(proj, sl.id, "blocked", reason="waiting for a decision")
    o = run(proj, sl.id).outcomes[0]
    assert o.action == "refused" and "waiting for a decision" in o.reason
    transition(proj, sl.id, "planned")
    # plan blockers, until accepted in decisions.json
    lay = Phase2Layout(proj)
    data = json.loads(lay.plan_json.read_text())
    for s in data["slices"]:
        if s["id"] == sl.id:
            s["blockers"] = ["F-0007"]
    lay.plan_json.write_text(json.dumps(data))
    o = run(proj, sl.id).outcomes[0]
    assert o.action == "refused" and "F-0007" in o.reason
    lay.slice_decisions(sl.id).write_text(json.dumps({"decisions": [               # migrate/approvals.py format
        {"kind": "accept_observed", "ref": "F-0007", "status": "open"}]}))
    assert run(proj, sl.id).outcomes[0].action == "refused"
    lay.slice_decisions(sl.id).write_text(json.dumps({"decisions": [
        {"kind": "accept_observed", "ref": "F-0007", "status": "resolved", "choice": "accept_observed"}]}))
    assert run(proj, sl.id).outcomes[0].status == "awaiting_human"


def test_entry_slice_needs_recorded_cases(proj):
    pl = plan(proj)
    entry = next(s for s in pl.slices if s.kind == "entry")
    for d in entry.depends_on:                                      # pretend dependencies are approved
        save_state(proj, SliceState(slice_id=d, status="approved"))
    o = run(proj, entry.id).outcomes[0]
    assert o.action == "refused" and "no characterization cases" in o.reason
    from migration_agent.migrate.casestore import save_case
    case = CharacterizationCase.model_validate({"id": "CT-0001", "slice": entry.id, "kind": "http",
                                                "request": {"method": "GET", "path": "/api/x"}})
    save_case(proj, case)
    o = run(proj, entry.id).outcomes[0]
    assert o.action == "refused" and "not recorded" in o.reason
    save_case(proj, case.model_copy(update={"expect": case.expect.model_copy(update={"status": 200})}))
    o = run(proj, entry.id).outcomes[0]
    assert o.status == "awaiting_human"
    assert load_state(proj, entry.id).history[0].reason == "1 recorded characterization case(s)"


def test_selection_errors(proj):
    with pytest.raises(sr.GenerateError):
        sr.generate(proj, llm=OfflineCodegenLLM(), backend=FakeExecBackend(proj))
    with pytest.raises(sr.GenerateError):
        sr.generate(proj, slice_id="SLICE-0999", llm=OfflineCodegenLLM(), backend=FakeExecBackend(proj))


def test_all_runs_ready_slices_in_order(proj):
    rep = sr.generate(proj, all_slices=True, llm=OfflineCodegenLLM(), backend=FakeExecBackend(proj),
                      suite_runner=passing_suite)
    ran = [o for o in rep.outcomes if o.action == "ran"]
    refused = [o for o in rep.outcomes if o.action == "refused"]
    assert ran and refused and all(o.status == "awaiting_human" for o in ran)
    assert len(rep.outcomes) == len(plan(proj).slices)


# ------------------------------------------------------------------ git: human commits, dirty tree, comments
def test_human_commits_and_dirty_tree(proj):
    sid = ready_slice(proj).id
    run(proj, sid)
    assert sr.human_commits(proj, sid) == []
    root = Phase2Layout(proj).target_code
    (root / "migration" / f"{sid}.md").write_text("human edit\n")
    (root / "unrelated.txt").write_text("x\n")
    # dirty tree: never overwritten, the run is refused
    transition(proj, sid, "generating", reason="test reopen")
    with pytest.raises(sr.GenerateError, match="uncommitted changes"):
        run(proj, sid)
    env = {"GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.com", "GIT_COMMITTER_NAME": "Alice",
           "GIT_COMMITTER_EMAIL": "alice@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(proj.root)}
    for args in (["add", f"migration/{sid}.md"], ["commit", "-q", "-m", "fix wording"], ["add", "unrelated.txt"],
                 ["commit", "-q", "-m", "other file"]):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args], env=env, check=True)
    hc = sr.human_commits(proj, sid)
    assert [c["subject"] for c in hc] == ["fix wording"] and hc[0]["email"] == "alice@example.com"
    assert hc[0]["files"] == [f"migration/{sid}.md"]


def test_from_comments_reopens_with_comments(proj):
    sid = ready_slice(proj).id
    run(proj, sid)
    lay = Phase2Layout(proj)
    sl = ready_slice(proj)
    lay.comments.write_text(json.dumps({"comments": [
        {"id": "C-1", "item_id": sl.node_ids[0], "file": f"migration/{sid}.md", "line": 1,
         "text": "please mention the port", "processed": False},
        {"id": "C-2", "item_id": "EP-9999", "text": "other slice", "processed": False}]}))
    llm = OfflineCodegenLLM()
    o = run(proj, sid, llm=llm).outcomes[0]
    assert o.action == "nothing_to_do" and "awaiting human review" in o.reason
    llm = ScriptLLM(codegen=[reply(f"migration/{sid}.md", "port 3000\n", op="replace")])
    o = run(proj, sid, llm=llm, from_comments=True).outcomes[0]
    assert o.status == "awaiting_human" and o.iterations == 2
    prompt = [c for c in llm.calls if c["task"] == "codegen"][0]["prompt"]
    assert "please mention the port" in prompt and "other slice" not in prompt
    cm = {c["id"]: c["processed"] for c in json.loads(lay.comments.read_text())["comments"]}
    assert cm == {"C-1": True, "C-2": False}


def test_in_place_seeds_from_legacy_src(tmp_path, tmp_path_factory):
    y = ("kind: version_upgrade\nstrategy: in_place\nsource: {language: javascript, runtime: node@16}\n"
         "target: {language: javascript, runtime: node@20, build: npm}\n")
    p = m4_project(tmp_path, tmp_path_factory, target_yaml=y)
    lay = Phase2Layout(p)
    (lay.legacy_src / "src").mkdir(parents=True)
    (lay.legacy_src / "src" / "server.js").write_text("// original unredacted\n")
    (lay.legacy_src / ".env").write_text("SECRET=1\n")
    rep = run(p, ready_slice(p).id)
    assert (lay.target_code / "src" / "server.js").read_text() == "// original unredacted\n"
    assert not (lay.target_code / ".env").exists()
    assert "seed target_code from legacy_src/" in git(p, "log", "--format=%s")
    # jscodeshift stub is reported (skipped), never run on the host
    assert [m["name"] for m in rep.codemods] == ["jscodeshift"] and rep.codemods[0]["status"] == "skipped"


def test_in_place_without_legacy_src_seeds_from_code_with_warning(tmp_path, tmp_path_factory):
    y = ("kind: version_upgrade\nstrategy: in_place\nsource: {language: javascript}\n"
         "target: {language: javascript, build: npm}\n")
    p = m4_project(tmp_path, tmp_path_factory, target_yaml=y)
    rep = run(p, ready_slice(p).id)
    assert any("REDACTED code/" in w for w in rep.warnings)
    assert (Phase2Layout(p).target_code / "src").is_dir()


def test_cli_generate_offline(proj, monkeypatch):
    import migration_agent.migrate.exec as ex
    monkeypatch.setattr(ex, "get_backend", lambda project=None: FakeExecBackend(project))
    sid = entry_with_recorded_case(proj).id
    res = CliRunner().invoke(main, ["generate", str(proj.root), "--slice", sid, "--offline"])
    assert res.exit_code == 1, res.output                       # stops at built: no new service in the harness
    assert f"{sid}: stopped -> built" in res.output and "skipped" in res.output
    res = CliRunner().invoke(main, ["generate", str(proj.root), "--offline"])
    assert res.exit_code != 0 and "exactly one of" in res.output
