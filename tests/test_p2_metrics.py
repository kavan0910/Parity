"""M7 metrics (plan section 10) on synthetic project folders: audit records, iteration files, equivalence reports,
recordings, masks, comments, runtime findings and target_code git attribution."""
import json

import pytest
from click.testing import CliRunner

from migration_agent.audit import AuditLog
from migration_agent.cli import main
from migration_agent.migrate.metrics import compute_metrics, render_table
from migration_agent.migrate.models import (
    EquivalenceDiff, EquivalenceReport, RuntimeFinding, RuntimeFindingsFile,
)
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import atomic_write_json

from p2_m5_helpers import HUMAN, commit, git, init_target, m5_project, make_case
from phase2_helpers import approved_project


@pytest.fixture
def synth(tmp_path, tmp_path_factory):
    p, s1, s2 = m5_project(tmp_path, tmp_path_factory)
    lay = Phase2Layout(p)
    a1, a2, q1 = (make_case(p, s1, f"/api/{k}", key=k) for k in ("a1", "a2", "q1"))
    b1 = make_case(p, s2, "/api/b1", key="b1")
    log = AuditLog(p.audit_dir / "audit.jsonl")
    log.record("codegen", "llm_call", cost_usd=0.5, tokens_in=100, tokens_out=50, cached=False, slice=s1)
    log.record("codegen", "llm_call", cost_usd=0.0, tokens_in=100, tokens_out=50, cached=True, slice=s1)
    log.record("codegen", "llm_call", cost_usd=0.25, tokens_in=10, tokens_out=5, cached=False, slice=s2)
    log.record("analyzer", "llm_call", cost_usd=1.0, tokens_in=1000, tokens_out=500, cached=False)   # Phase 1
    for step, ms in (("codegen", 2000), ("review", 1000), ("build", 3000), ("tests", 4000)):
        log.record("slice_runner", "generate_step", duration_ms=ms, slice=s1, step=step, iteration=2)
    log.record("exec", "exec_run", duration_ms=1500, labels={"slice": s2, "stage": "build"})
    log.record("x", "bad", cost_usd="not a number")                                                 # tolerated

    it = lay.slice_iterations(s1)
    atomic_write_json(it / "1" / "build.json", {"ok": False})
    atomic_write_json(it / "2" / "build.json", {"ok": True})
    atomic_write_json(it / "2" / "tests.json", {"result": "passed", "ok": True, "characterization": {
        "status": "failed", "totals": {"pass": 3, "fail": 1}, "failures": [{"case_id": q1.id}]}})
    atomic_write_json(it / "2" / "review.json", {"findings": [{"kind": "x", "accepted": True},
                                                              {"kind": "y", "status": "rejected"}, {"kind": "z"}]})
    it2 = lay.slice_iterations(s2)
    atomic_write_json(it2 / "1" / "build.json", {"ok": True})
    atomic_write_json(it2 / "1" / "tests.json", {"result": "failed", "ok": False})

    atomic_write_json(lay.slice_equivalence(s1), EquivalenceReport(slice=s1, cases_total=4, cases_equivalent=4, diffs=[
        EquivalenceDiff(id="EQ-0001", case_id=q1.id, field="status", classification="intended_divergence"),
        EquivalenceDiff(id="EQ-0002", case_id=a1.id, field="body.$.ts", classification="noise")]))
    atomic_write_json(lay.slice_equivalence(s2), EquivalenceReport(slice=s2, cases_total=2, cases_equivalent=1, diffs=[
        EquivalenceDiff(id="EQ-0003", case_id=b1.id, field="status", classification="unknown")]))
    atomic_write_json(lay.recordings_dir / "RUN-0001" / "report.json",
                      {"cases": [{"case_id": a1.id, "status": "recorded", "masks_added": ["$.ts", "$.id"]}]})
    atomic_write_json(lay.equivalence_dir / "masks.json", {"masks": [
        {"eq_id": "EQ-0003", "case_id": b1.id, "mask": "$.v", "status": "approved"},
        {"eq_id": "EQ-0004", "case_id": b1.id, "mask": "$.w", "status": "proposed"}]})
    atomic_write_json(lay.comments, {"comments": [{"id": "CM-0001", "item_id": s1}, {"id": "CM-0002",
                                                                                      "item_id": "EP-0002"},
                                                  {"id": "CM-0003", "item_id": a2.id}]})
    atomic_write_json(lay.runtime_findings, RuntimeFindingsFile(findings=[
        RuntimeFinding(id="EQ-0010", kind="legacy_failure", title="5xx"),
        RuntimeFinding(id="EQ-0011", kind="contradicts_rule", title="rule", status="accepted_recorded")]))

    init_target(p)                                                    # agent: 1 line
    sha = commit(p, "a.py", "x = 1\ny = 2\n", "agent work")           # agent: 2 lines
    git(p, "tag", f"slice/{s1}/approved", sha)
    commit(p, "h.py", "z = 3\n", "human fix", author=HUMAN)           # human: 1 line
    return dict(p=p, s1=s1, s2=s2)


def _row(m, sid):
    return next(r for r in m["slices"] if r["slice_id"] == sid)


def test_metrics_on_synthetic_project(synth):
    p, s1, s2 = synth["p"], synth["s1"], synth["s2"]
    m = compute_metrics(p)
    t, a = m["totals"], m["aggregate"]
    assert t["cost_usd"] == 1.75 and t["slice_cost_usd"] == 0.75 and t["calls"] == 4
    assert t["tokens_in"] == 1210 and t["tokens_out"] == 605 and t["cache_hit_rate"] == 0.25
    assert m["legacy_lines"] > 0 and t["cost_per_1k_legacy_lines_usd"] == round(0.75 * 1000 / m["legacy_lines"], 6)

    r1, r2 = _row(m, s1), _row(m, s2)
    assert r1["cost_usd"] == 0.5 and r1["cache_hit_rate"] == 0.5 and r1["cost_per_node_usd"] == 0.5
    assert r1["iterations"] == 2 and r1["iterations_to_green"] == 2 and r1["build_first_try"] is False
    assert r2["iterations_to_green"] is None and r2["build_first_try"] is True
    assert r1["characterization"] == {"passed": 3, "total": 3, "rate": 1.0}     # intended divergence excluded
    assert r1["equivalence"]["pct"] == 100.0 and r1["equivalence"]["unknown_diff_rate"] == 0.0
    assert r2["equivalence"]["pct"] == 50.0 and r2["equivalence"]["unknown_diff_rate"] == 1.0
    assert r1["mask_additions"] == 2 and r2["mask_additions"] == 1
    assert r1["comments"] == 2 and r2["comments"] == 1
    assert r1["reviewer"] == {"findings": 3, "marked": 2, "accepted": 1, "precision": 0.5}
    assert r2["reviewer"]["precision"] is None
    assert r1["wall_clock_s"] == {"model": 3.0, "exec": 7.0, "total": 10.0}
    assert r2["wall_clock_s"]["exec"] == 1.5 and r2["wall_clock_s"]["model"] == 0.0
    assert r1["human_edits"] == {"human_commits": 0, "agent_commits": 2, "human_lines": 0, "agent_lines": 3}
    assert r1["human_edit_rate"] == 0.0 and r2["human_edit_rate"] is None

    assert a["build_first_try_pct"] == 50.0 and a["iterations_to_green_avg"] == 2.0
    assert a["equivalence_pct"] == round(100 * 5 / 6, 2) and a["unknown_diff_rate"] == round(1 / 3, 4)
    assert a["mask_additions"] == 3 and a["comments"] == 3
    assert a["human_edit_rate"] == round(1 / 3, 4)
    assert m["human_edits"]["human_lines"] == 1 and m["human_edits"]["agent_lines"] == 3
    assert a["reviewer_precision"] == 0.5
    assert m["runtime_findings"] == {"total": 2, "confirmed": 1, "open": 1,
                                     "by_kind": {"legacy_failure": 1, "contradicts_rule": 1}}
    json.dumps(m)                                                     # JSON-ready


def test_metrics_without_data_are_na(tmp_path, tmp_path_factory):
    p = approved_project(tmp_path, tmp_path_factory)
    m = compute_metrics(p)
    assert m["slices"] == [] and m["aggregate"]["equivalence_pct"] is None
    assert m["aggregate"]["reviewer_precision"] is None and m["aggregate"]["human_edit_rate"] is None
    assert m["aggregate"]["build_first_try_pct"] is None and m["runtime_findings"]["total"] == 0
    out = render_table(m)
    assert "n/a" in out and "no slices" in out


def test_metrics_cli_table_and_json(synth):
    p = synth["p"]
    r = CliRunner().invoke(main, ["metrics", str(p.root), "--format", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["aggregate"]["build_first_try_pct"] == 50.0
    r = CliRunner().invoke(main, ["metrics", str(p.root)])
    assert r.exit_code == 0 and synth["s1"] in r.output and "reviewer precision 50.0%" in r.output


def test_service_metrics_facade(synth):
    from migration_agent import service
    assert service.metrics(synth["p"].root)["totals"]["calls"] == 4
