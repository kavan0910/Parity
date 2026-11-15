"""M7 cut-over: readiness blockers one by one, approval refused / allowed with hashes, plan.md content, CLI."""
import json

import pytest
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import cutover as co
from migration_agent.migrate.approvals import SliceDecision, SliceDecisionsFile
from migration_agent.migrate.configmap import ConfigMap, ConfigMapEntry, SecretRef
from migration_agent.migrate.equivalence import CaseOutcome, EquivalenceRunMeta, run_meta_path
from migration_agent.migrate.models import (
    CutoverApproval, EquivalenceDiff, EquivalenceReport, MigrationPlan, SliceApproval,
)
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import atomic_write_json, now_iso, transition

from p2_m5_helpers import commit, git, init_target, m5_project, make_case, walk_to_awaiting
from phase2_helpers import edit_contract


def _approve_state(p, sid):
    walk_to_awaiting(p, sid)
    transition(p, sid, "approved", actor="human:alice", reason="ok")
    atomic_write_json(Phase2Layout(p).slice_approval(sid), SliceApproval(slice_id=sid, approved_by="alice",
                                                                       approved_at=now_iso()))


def _report(p, sid, case_ids, *, diffs=(), regression=(), head="", created=None):
    lay = Phase2Layout(p)
    rep = EquivalenceReport(slice=sid, cases_total=len(case_ids),
                            cases_equivalent=len(case_ids) - len({d.case_id for d in diffs
                                                                  if d.classification in ("regression", "unknown")}),
                            diffs=list(diffs), created=created or now_iso())
    atomic_write_json(lay.slice_equivalence(sid), rep)
    atomic_write_json(run_meta_path(p, sid), EquivalenceRunMeta(
        slice=sid, created=rep.created, new_url="http://router:8080", target_head=head,
        regression_slices=list(regression), cases=[CaseOutcome(case_id=c, equivalent=True) for c in case_ids]))
    return rep


@pytest.fixture
def ready(tmp_path, tmp_path_factory, monkeypatch):
    """Two approved slices, clean per-slice reports, a full-suite run (slice 2 + regression slice 1) on HEAD.
    Coverage / config / schema gates are patched clean (each has its own test below)."""
    p, s1, s2 = m5_project(tmp_path, tmp_path_factory)
    c1 = make_case(p, s1, "/api/a", key="a")
    c2 = make_case(p, s2, "/api/b", key="b")
    init_target(p)
    head = commit(p, "app.py", "print('new')\n", "final")
    for s in (s1, s2):
        _approve_state(p, s)
    _report(p, s1, [c1.id], head=head, created="2030-01-01T00:00:00+00:00")
    intended = EquivalenceDiff(id="EQ-0001", case_id=c2.id, slice=s2, field="status", legacy=200, new=409,
                               classification="intended_divergence", decision_ref="COMP-0002")
    _report(p, s2, [c1.id, c2.id], diffs=[intended], regression=[s1], head=head,
            created="2030-01-02T00:00:00+00:00")
    monkeypatch.setattr(co, "_coverage_gaps", lambda project: [])
    monkeypatch.setattr(co, "_config_gaps", lambda project: [])
    monkeypatch.setattr(co, "_schema_blockers", lambda project: {})
    return dict(p=p, s1=s1, s2=s2, c1=c1, c2=c2, head=head)


def _blockers(p, name):
    rd = co.readiness(p)
    return [b for c in rd.checks if c.name == name for b in c.blockers]


def test_clean_project_is_ready(ready):
    rd = co.readiness(ready["p"])
    assert rd.ready, rd.blockers
    assert rd.full_suite.kind == "slice" and rd.full_suite.target_head == ready["head"]
    assert {c.name for c in rd.checks} >= {"contract", "plan", "slices", "equivalence", "full_suite", "target_code",
                                           "coverage", "config", "schema", "findings", "budget"}


def test_blocker_unapproved_slice(ready):
    p, s2 = ready["p"], ready["s2"]
    transition(p, s2, "generating", actor="human:bob", reason="reopen")
    assert any(s2 in b and "not approved" in b for b in _blockers(p, "slices"))


def test_blocker_missing_approval_file(ready):
    p, s1 = ready["p"], ready["s1"]
    Phase2Layout(p).slice_approval(s1).unlink()
    assert any("approval.json" in b for b in _blockers(p, "slices"))


def test_blocker_slice_regression(ready):
    p, s1, c1 = ready["p"], ready["s1"], ready["c1"]
    bad = EquivalenceDiff(id="EQ-0009", case_id=c1.id, slice=s1, field="body.$.total", legacy=10, new=11,
                          classification="regression")
    _report(p, s1, [c1.id], diffs=[bad], head=ready["head"], created="2030-01-01T00:00:00+00:00")
    assert any("EQ-0009" in b for b in _blockers(p, "equivalence"))


def test_blocker_missing_full_suite_is_tolerated_with_message(ready):
    p, s2 = ready["p"], ready["s2"]
    run_meta_path(p, s2).unlink()
    bl = _blockers(p, "full_suite")
    assert bl and "composite report" in bl[0] and "missing" in bl[0]


def test_blocker_full_suite_on_old_tree(ready):
    p = ready["p"]
    commit(p, "later.py", "x = 1\n", "after the full-suite run")
    assert any("re-run the full suite" in b for b in _blockers(p, "full_suite"))


def test_blocker_full_suite_missing_case(ready):
    p, s1, s2, c1, c2 = (ready[k] for k in ("p", "s1", "s2", "c1", "c2"))
    _report(p, s2, [c2.id], regression=[s1], head=ready["head"], created="2030-01-02T00:00:00+00:00")
    assert any(c1.id in b and "did not run" in b for b in _blockers(p, "full_suite"))


def test_composite_report_preferred_when_newer(ready):
    p, c1, c2 = ready["p"], ready["c1"], ready["c2"]
    bad = EquivalenceDiff(id="EQ-0010", case_id=c2.id, field="status", legacy=200, new=500, classification="unknown")
    atomic_write_json(co.composite_report_path(p), EquivalenceReport(cases_total=2, cases_equivalent=1, diffs=[bad]))
    atomic_write_json(co.composite_run_path(p), {"created": "2031-01-01T00:00:00+00:00", "target_head": ready["head"],
                                                 "cases": [{"case_id": c1.id}, {"case_id": c2.id}]})
    rd = co.readiness(p)
    assert rd.full_suite.kind == "composite"
    assert any("EQ-0010" in b for b in rd.blockers)


def test_blocker_dirty_target_code(ready):
    p = ready["p"]
    (Phase2Layout(p).target_code / "app.py").write_text("print('edited')\n")
    assert any("uncommitted" in b for b in _blockers(p, "target_code"))


def test_blocker_coverage_gaps(ready, monkeypatch):
    monkeypatch.setattr(co, "_coverage_gaps", lambda project: ["EP-0003", "RULE-0001"])
    assert any("EP-0003" in b for b in _blockers(ready["p"], "coverage"))


def test_coverage_gate_uses_recorder_for_real(ready, monkeypatch):
    from migration_agent.migrate import recorder
    monkeypatch.setattr(co, "_coverage_gaps", lambda project: recorder.coverage_gaps(project))
    assert _blockers(ready["p"], "coverage")       # the express-crud fixture has uncovered in-scope nodes


def test_blocker_config_gaps(ready, monkeypatch):
    monkeypatch.setattr(co, "_config_gaps", lambda project: [{"id": "app.yml:db.url", "reason": "no target key"}])
    assert _blockers(ready["p"], "config") == ["app.yml:db.url: no target key"]


def test_config_gate_without_map_blocks(ready, monkeypatch):
    from migration_agent.migrate import configmap
    monkeypatch.setattr(co, "_config_gaps", lambda project: configmap.config_gaps(project))
    assert any("config map not generated" in b for b in _blockers(ready["p"], "config"))


def test_blocker_schema(ready, monkeypatch):
    monkeypatch.setattr(co, "_schema_blockers", lambda project: {ready["s1"]: ["calls stored procedure sp_x"]})
    assert any("sp_x" in b for b in _blockers(ready["p"], "schema"))


def test_blocker_deferred_finding_until_accepted(ready):
    p, s1 = ready["p"], ready["s1"]
    lay = Phase2Layout(p)
    plan = MigrationPlan.model_validate_json(lay.plan_json.read_text())
    plan.slices[0].blockers = ["F-0042"]
    atomic_write_json(lay.plan_json, plan)
    assert any("F-0042" in b for b in _blockers(p, "findings"))
    atomic_write_json(lay.slice_decisions(s1), SliceDecisionsFile(slice_id=s1, decisions=[
        SliceDecision(kind="accept_observed", ref="F-0042", status="resolved", choice="accept_observed", by="alice")]))
    assert _blockers(p, "findings") == []
    assert "F-0042 migrated as observed" in co.render_plan(p)


def test_blocker_open_decision(ready):
    p, s2 = ready["p"], ready["s2"]
    atomic_write_json(Phase2Layout(p).slice_decisions(s2), SliceDecisionsFile(slice_id=s2, decisions=[
        SliceDecision(kind="legacy_bug", ref="EQ-0001", question="preserve or fix?")]))
    assert any("open legacy_bug" in b for b in _blockers(p, "findings"))


def test_blocker_budget(ready):
    from migration_agent.audit import AuditLog
    p = ready["p"]
    lay = Phase2Layout(p)
    lay.target_yaml.parent.mkdir(parents=True, exist_ok=True)
    lay.target_yaml.write_text("kind: framework_move\nsource: {language: javascript}\ntarget: {language: typescript}\n"
                               "budget_usd: 0.5\n")
    AuditLog(p.audit_dir / "audit.jsonl").record("codegen", "llm_call", cost_usd=0.75, slice=ready["s1"])
    assert any("exceeds the project budget" in b for b in _blockers(p, "budget"))


def test_blocker_contract(ready):
    p = ready["p"]
    edit_contract(p, graph_sha256="0" * 64)
    assert _blockers(p, "contract")


def test_approve_refused_while_not_ready(ready, monkeypatch):
    p = ready["p"]
    monkeypatch.setattr(co, "_config_gaps", lambda project: [{"id": "", "reason": "map missing"}])
    with pytest.raises(co.CutoverError) as ei:
        co.approve(p, "alice")
    assert any("map missing" in b for b in ei.value.blockers)
    assert not Phase2Layout(p).cutover_approval.exists()
    assert "NOT READY" in Phase2Layout(p).cutover_plan.read_text()


def test_approve_writes_hashes_and_verifies(ready):
    p = ready["p"]
    lay = Phase2Layout(p)
    with pytest.raises(co.CutoverError):
        co.approve(p, "  ")
    a = co.approve(p, "alice", note="go")
    assert isinstance(a, CutoverApproval)
    disk = CutoverApproval.model_validate_json(lay.cutover_approval.read_text())
    assert disk.tree_commit == ready["head"] and disk.slice_ids == [ready["s1"], ready["s2"]]
    from migration_agent.migrate.state import sha256_file
    assert disk.contract_sha256 == sha256_file(lay.contract_file)
    assert disk.plan_sha256 == sha256_file(lay.cutover_plan)
    hashes = json.loads((lay.cutover_dir / "hashes.json").read_text())
    assert co.combined_digest(hashes["equivalence"]) == disk.equivalence_report_sha256
    assert any(k.endswith("equivalence.json") for k in hashes["equivalence"])
    # re-rendering an unchanged project keeps the approval valid (plan.md is deterministic)
    co.write_plan(p)
    assert co.verify_approval(p)["valid"]
    # changing the tree makes it stale
    commit(p, "hotfix.py", "y = 2\n", "hotfix")
    v = co.verify_approval(p)
    assert v["approved"] and not v["valid"] and "tree changed since approval" in v["mismatches"]


def test_tree_hash_without_git(ready):
    p = ready["p"]
    import shutil
    shutil.rmtree(Phase2Layout(p).target_code / ".git")
    tc = co.tree_commit(p)
    assert tc.startswith("tree-sha256:") and len(tc) == len("tree-sha256:") + 64
    rd = co.readiness(p)
    assert any("not a git repository" in w for w in rd.warnings)


def test_plan_md_content(ready):
    p, s1, s2 = ready["p"], ready["s1"], ready["s2"]
    lay = Phase2Layout(p)
    cmap = ConfigMap(convention="node", entries=[
        ConfigMapEntry(id="src/config/index.js:apiToken", source_file="src/config/index.js", source_key="apiToken",
                       is_secret=True, status="mapped", target_key="API_TOKEN", env_name="API_TOKEN",
                       reference="${API_TOKEN}"),
        ConfigMapEntry(id="src/config/index.js:port", source_file="src/config/index.js", source_key="port",
                       status="renamed", target_key="server.port", env_name="PORT"),
        ConfigMapEntry(id="src/config/index.js:legacyFlag", source_file="src/config/index.js",
                       source_key="legacyFlag", status="dropped", reason="unused since 2019")],
        secrets=[SecretRef(env_name="API_TOKEN", target_key="API_TOKEN", reference="${API_TOKEN}", vault="vault")])
    atomic_write_json(lay.config_map_json, cmap)
    lay.ensure(lay.data_dir)
    (lay.data_dir / "schema_changes.yaml").write_text(
        "schema_version: 1\nchanges:\n  - {id: SC-001, kind: add_column, table: products, column: archived, "
        "type: BOOLEAN, default: 'FALSE', nullable: false, reason: soft delete}\n")
    md = co.write_plan(p).read_text()
    assert md.index(f"Step 1: {s1}") < md.index(f"Step 2: {s2}")
    assert "Rollback" in md and "retire legacy" in md
    assert "`API_TOKEN`" in md and "server.port" in md and "unused since 2019" in md
    assert "SC-001" in md and "ADD COLUMN" in md
    assert "## 4. Monitoring" in md and "5xx rate" in md
    assert "intended divergence EQ-0001" in md
    assert "READY" in md
    routes = co._routes(p, MigrationPlan.model_validate_json(lay.plan_json.read_text()))["routes"]
    for r in routes:
        assert f"harness flip PROJECT {r['slice']}" in md and f"harness unflip PROJECT {r['slice']}" in md
    assert md == co.render_plan(p)          # deterministic
    assert "run 'migration-agent config schema-check PROJECT'" in md


def test_cli_cutover_not_ready_then_approve(ready, monkeypatch):
    p = ready["p"]
    r = CliRunner().invoke(main, ["cutover", str(p.root), "--approve"])
    assert r.exit_code != 0 and "--by" in r.output
    monkeypatch.setattr(co, "_config_gaps", lambda project: [{"id": "k", "reason": "open"}])
    r = CliRunner().invoke(main, ["cutover", str(p.root)])
    assert r.exit_code == 1 and "NOT READY" in r.output and "k: open" in r.output
    r = CliRunner().invoke(main, ["cutover", str(p.root), "--approve", "--by", "alice"])
    assert r.exit_code != 0 and "cannot be approved" in r.output
    monkeypatch.setattr(co, "_config_gaps", lambda project: [])
    r = CliRunner().invoke(main, ["cutover", str(p.root), "--format", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["ready"] is True
    r = CliRunner().invoke(main, ["cutover", str(p.root), "--approve", "--by", "alice"])
    assert r.exit_code == 0, r.output
    assert "approved by alice" in r.output
    r = CliRunner().invoke(main, ["cutover", str(p.root)])
    assert "approval: valid" in r.output
    assert git(p, "status", "--porcelain").strip() == ""     # cut-over never touches target_code/


def test_full_suite_reads_composite_report_written_by_equivalence(tmp_path):
    """Seam test: cutover must understand equivalence.CompositeReport (the real `equivalence composite` output)."""
    from migration_agent.migrate import cutover
    from migration_agent.migrate.equivalence import CompositeCase, CompositeReport
    from migration_agent.migrate.models import MigrationPlan
    from migration_agent.project import Project
    p = Project.init(tmp_path / "p", source="s")
    path = cutover.composite_report_path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    rep = CompositeReport(created="2026-11-13T00:00:00+00:00", base_url="http://router:8080", target_head="abc",
                          cases=[CompositeCase(case_id="CT-0001", outcome="pass"),
                                 CompositeCase(case_id="CT-0002", outcome="fail")])
    path.write_text(rep.model_dump_json())
    plan = MigrationPlan.model_validate({"version": 1, "slices": []}) if hasattr(MigrationPlan, "model_validate") else None
    cands = cutover._full_suite_candidates(p, plan)
    comp = [c for c in cands if c.kind == "composite"]
    assert comp and comp[0].cases_total == 2 and comp[0].unexplained == ["CT-0002"]
