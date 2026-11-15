"""M5 approvals: every approval blocker refuses, clean slice approves (approval.json, state, git tag), human commit
after equivalence forces a re-run, accept-observed, comments store (redaction, ids, file:line), CLI."""
import json

import pytest
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import approvals as ap
from migration_agent.migrate import equivalence as eq
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import load_state

from p2_m5_helpers import (
    HUMAN, Svc, commit, init_target, m5_project, make_case, standard_cases, walk_to_awaiting, write_iteration,
)


@pytest.fixture
def env(tmp_path, tmp_path_factory):
    """A clean, approvable SLICE 1: awaiting_human, green iteration, equivalence clean on the current HEAD."""
    p, s1, s2 = m5_project(tmp_path, tmp_path_factory)
    with Svc("legacy") as legacy, Svc("new") as new, Svc("regressed") as bad:
        cases = standard_cases(p, s1, legacy.base_url)
        init_target(p)
        commit(p, "price.py", "TOTAL = 10\n", "slice one")
        walk_to_awaiting(p, s1)
        write_iteration(p, s1)
        eq.run_equivalence(p, s1, legacy.base_url, new.base_url)
        ctx = dict(p=p, s1=s1, s2=s2, legacy=legacy, new=new, bad=bad, cases=cases)
        ctx["rerun"] = lambda url=None: eq.run_equivalence(p, s1, legacy.base_url, url or new.base_url)
        yield ctx


def _blockers(c):
    return ap.approval_blockers(c["p"], c["s1"])


def test_clean_slice_approves_and_tags(env):
    p, s1 = env["p"], env["s1"]
    assert _blockers(env) == []
    a = ap.approve_slice(p, s1, "alice", note="looks good")
    head = ap.git_head(p)
    assert a.commit_sha == head and a.approved_by == "alice"
    lay = Phase2Layout(p)
    assert f"slices/{s1}/equivalence.json" in a.report_hashes and f"slices/{s1}/iterations/1/build.json" in a.report_hashes
    assert ap.load_approval(p, s1) == a
    assert load_state(p, s1).status == "approved"
    assert ap.tag_sha(p, ap.approved_tag(s1)) == head
    assert json.loads(lay.slice_approval(s1).read_text())["commit_sha"] == head
    with pytest.raises(ap.ApprovalError):                    # already approved: state gate
        ap.approve_slice(p, s1, "alice")


@pytest.mark.parametrize("blocker", ["state", "build", "tests", "regression", "unknown", "no_report", "fix_request",
                                     "decision", "unapproved_spec", "plan_blocker"])
def test_each_blocker_refuses(env, blocker):
    p, s1 = env["p"], env["s1"]
    expect = {"state": "slice state", "build": "build of iteration", "tests": "test suite", "regression": "regression",
              "unknown": "regression/unknown", "no_report": "no equivalence report", "fix_request": "pending fix",
              "decision": "open legacy_bug", "unapproved_spec": "unapproved specified", "plan_blocker": "RISK-0001"}
    if blocker == "state":
        from migration_agent.migrate.state import transition
        transition(p, s1, "generating", reason="again")
    elif blocker == "build":
        write_iteration(p, s1, 2, build={"exit_code": 1}, tests={"ok": True})
    elif blocker == "tests":
        write_iteration(p, s1, 2, tests={"failed": [env["cases"]["price"].id]})
    elif blocker == "regression":
        env["rerun"](env["bad"].base_url)
    elif blocker == "unknown":
        make_case(p, s1, "/api/unrec", key="unrec")
        env["rerun"]()
    elif blocker == "no_report":
        Phase2Layout(p).slice_equivalence(s1).unlink()
    elif blocker == "fix_request":
        eq.add_fix_request(p, s1, eq_id="EQ-0001", reason="new_bug")
    elif blocker == "decision":
        ap.upsert_open_decision(p, s1, "legacy_bug", "EQ-0001", "legacy wrong?")
    elif blocker == "unapproved_spec":
        q = env["cases"]["quirk"]
        from migration_agent.migrate.casestore import save_case
        save_case(p, q.model_copy(update={"expect": q.expect.model_copy(update={"approved_by": ""})}))
    elif blocker == "plan_blocker":
        from migration_agent.migrate.casestore import load_plan
        from migration_agent.migrate.state import atomic_write_json
        plan = load_plan(p)
        plan.slices[0].blockers = ["RISK-0001"]
        atomic_write_json(Phase2Layout(p).plan_json, plan)
    bl = _blockers(env)
    assert bl and any(expect[blocker] in b for b in bl), bl
    with pytest.raises(ap.ApprovalError) as ei:
        ap.approve_slice(p, s1, "alice")
    assert ei.value.blockers == bl
    assert load_state(p, s1).status != "approved" and not Phase2Layout(p).slice_approval(s1).exists()
    assert not ap.tag_sha(p, ap.approved_tag(s1))


def test_failed_tests_only_on_intended_divergence_are_allowed(env):
    write_iteration(env["p"], env["s1"], 2, tests={"failed": [env["cases"]["quirk"].id]})
    assert _blockers(env) == []


def test_deferred_finding_needs_accept_observed(env):
    from migration_agent.migrate.casestore import load_plan
    from migration_agent.migrate.state import atomic_write_json
    p, s1 = env["p"], env["s1"]
    plan = load_plan(p)
    plan.slices[0].blockers = ["F-0003"]
    atomic_write_json(Phase2Layout(p).plan_json, plan)
    assert any("F-0003" in b for b in _blockers(env))
    with pytest.raises(ap.ApprovalError):
        ap.accept_observed(p, s1, "F-0003", "alice", "")                  # note required
    with pytest.raises(ap.ApprovalError):
        ap.accept_observed(p, s1, "F-0009", "alice", "not a blocker here")
    r = CliRunner().invoke(main, ["slice", "accept-observed", str(p.root), s1, "F-0003", "--by", "alice",
                                  "--note", "legacy behaviour is what clients expect"])
    assert r.exit_code == 0, r.output
    assert _blockers(env) == []
    a = ap.approve_slice(p, s1, "alice")
    assert a.accepted_blockers == ["F-0003"]
    assert f"slices/{s1}/decisions.json" in a.report_hashes


def test_human_commit_after_equivalence_forces_rerun(env):
    p, s1 = env["p"], env["s1"]
    sha = commit(p, "price.py", "TOTAL = 10  # tidy\n", "human tweak", author=HUMAN)
    hc = ap.human_commits(p)
    assert [c["sha"] for c in hc] == [sha] and hc[0]["author"] == "Bob Human"
    bl = _blockers(env)
    assert any("human commit" in b and sha[:10] in b for b in bl), bl
    stats = ap.human_edit_stats(p)
    assert stats["human_commits"] == 1 and stats["human_lines"] == 2 and stats["agent_commits"] == 2
    env["rerun"]()                                                    # re-verification on the new HEAD
    assert eq.load_run_meta(p, s1).target_head == sha
    assert _blockers(env) == []
    assert ap.approve_slice(p, s1, "alice").commit_sha == sha


def test_agent_commit_after_equivalence_also_stale(env):
    commit(env["p"], "more.py", "x = 1\n", "agent iteration 2")
    assert any("HEAD changed" in b for b in _blockers(env))


def test_reject_slice(env):
    p, s1 = env["p"], env["s1"]
    with pytest.raises(ap.ApprovalError):
        ap.reject_slice(p, s1, "alice", "")
    r = ap.reject_slice(p, s1, "alice", "wrong error codes")
    assert r["status"] == "rejected" and load_state(p, s1).reason == "wrong error codes"
    with pytest.raises(ap.ApprovalError):
        ap.reject_slice(p, s1, "alice", "again")                         # not awaiting_human any more


def test_cli_approve_refused_then_ok(env):
    p, s1 = env["p"], env["s1"]
    eq.add_fix_request(p, s1, eq_id="EQ-0001", reason="new_bug")
    r = CliRunner().invoke(main, ["approve-slice", str(p.root), s1, "--by", "alice"])
    assert r.exit_code == 1 and "pending fix request" in r.output
    env["rerun"]()                                                    # EQ-0001 not live -> resolved
    r = CliRunner().invoke(main, ["approve-slice", str(p.root), s1, "--by", "alice", "--note", "ok"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(main, ["reject-slice", str(p.root), s1, "--by", "a", "--reason", "x"])
    assert r.exit_code == 1


def test_slice_diff_from_base_and_previous_approved_tag(env):
    p, s1, s2 = env["p"], env["s1"], env["s2"]
    d = ap.slice_diff(p, s1)
    assert d["base_label"] == "base (root commit)" and "price.py" in d["stat"] and "+TOTAL = 10" in d["diff"]
    ap.approve_slice(p, s1, "alice")
    commit(p, "two.py", "TWO = 2\n", "slice two")
    d2 = ap.slice_diff(p, s2)
    assert d2["base_label"] == ap.approved_tag(s1) and "two.py" in d2["diff"] and "price.py" not in d2["diff"]


def test_git_hooks_never_run(env):
    p = env["p"]
    hooks = Phase2Layout(p).target_code / ".git" / "hooks"
    marker = Phase2Layout(p).root / "HOOK_RAN"
    for h in ("reference-transaction", "post-checkout", "pre-commit"):
        (hooks / h).write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        (hooks / h).chmod(0o755)
    ap.approve_slice(p, env["s1"], "alice")                           # runs `git tag`
    ap.slice_diff(p, env["s2"])
    assert not marker.exists()


def test_comments_store(env):
    p, s1 = env["p"], env["s1"]
    c = ap.add_comment(p, s1, "use Decimal here, key AKIAABCDEFGHIJKLMNOP", author="bob", file="price.py", line=1)
    assert c.id == "CM-0001" and "AKIAABCDEFGHIJKLMNOP" not in c.text
    c2 = ap.add_comment(p, env["cases"]["price"].id, "case is fine")
    assert c2.id == "CM-0002" and c2.file == "" and c2.line is None
    data = json.loads(Phase2Layout(p).comments.read_text())
    assert set(data["comments"][0]) == {"id", "item_id", "file", "line", "text", "author", "created", "processed"}
    assert data["comments"][0]["processed"] is False
    ap.add_comment(p, "EP-0001", "graph id ok")
    for bad in [dict(item_id="SLICE-0999", text="x"), dict(item_id="NOPE", text="x"), dict(item_id=s1, text="  "),
                dict(item_id=s1, text="x", file="../etc/passwd"), dict(item_id=s1, text="x", file="missing.py"),
                dict(item_id=s1, text="x", file=".git/config"), dict(item_id=s1, text="x", line=3)]:
        with pytest.raises(ap.ApprovalError):
            ap.add_comment(p, bad.pop("item_id"), bad.pop("text"), **bad)
    r = CliRunner().invoke(main, ["slice", "comment", str(p.root), s1, "--text", "cli", "--file", "app.py",
                                  "--line", "1"])
    assert r.exit_code == 0 and "CM-0004" in r.output


def test_no_git_repo_is_tolerated(tmp_path, tmp_path_factory):
    p, s1, _ = m5_project(tmp_path, tmp_path_factory)
    with Svc("legacy") as legacy, Svc("new") as new:
        standard_cases(p, s1, legacy.base_url)
        walk_to_awaiting(p, s1)
        write_iteration(p, s1)
        eq.run_equivalence(p, s1, legacy.base_url, new.base_url)
    a = ap.approve_slice(p, s1, "alice")
    assert a.commit_sha == "" and ap.slice_diff(p, s1)["error"]
    assert ap.human_commits(p) == [] and not ap.is_git_repo(p)
