"""M3 recording: record twice + noise masks, expectations, triage, runtime findings, coverage + gaps, tests run."""
import json

import pytest
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import recorder, testgen
from migration_agent.migrate.casestore import find_case, load_cases, mark_untestable, save_case
from migration_agent.migrate.harness_runner import MASK
from migration_agent.migrate.ids import allocate
from migration_agent.migrate.models import CharacterizationCase
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import atomic_write_json

from p2_m3_helpers import RULE_ID, LegacyService, m3_project


@pytest.fixture
def proj(tmp_path, tmp_path_factory):
    p = m3_project(tmp_path, tmp_path_factory)
    testgen.generate_cases(p, offline=True)
    return p


@pytest.fixture
def legacy():
    with LegacyService() as svc:
        yield svc


def _case_for(p, node_id, tag=None, path=None):
    return next(c for c in load_cases(p) if c.covers[0] == node_id and (tag is None or tag in c.tags)
                and (path is None or c.request.path == path))


def test_record_twice_masks_noise_and_stores_expectations(proj, legacy):
    rep = recorder.record_cases(proj, legacy.base_url)
    assert len(rep.runs) == 2 and rep.runs[0] != rep.runs[1]
    assert not rep.by_status("harness_issue") and not rep.by_status("unstable")
    orders = _case_for(proj, "EP-0001")
    assert orders.normalize == ["$.generated_at", "$.orders[*].seen_at"]          # timestamps only, not ids
    assert orders.expect.status == 200 and orders.expect.recorded_runs == rep.runs
    assert orders.expect.body["generated_at"] == MASK and orders.expect.body["orders"][1]["id"] == 2
    assert orders.expect.headers == {"content-type": "application/json;charset=utf-8"}   # x-request-id not kept
    created = _case_for(proj, "EP-0002", "happy_path")
    assert created.normalize == ["$.created_at"] and created.expect.status == 201
    invalid = _case_for(proj, "EP-0002", "validation")
    assert (invalid.expect.status, invalid.expect.body) == (400, {"error": "invalid body"})
    assert _case_for(proj, "EP-0006", "error_path").expect.status == 404
    assert _case_for(proj, "EP-0007", "auth").expect.status == 401
    # recordings live under characterization/recordings/RUN-*/ (not runs/: those are exec jobs)
    lay = Phase2Layout(proj)
    for r in rep.runs:
        assert (lay.recordings_dir / r / "results.json").exists() and not lay.run_dir(r).exists()
    assert json.loads((lay.recordings_dir / rep.runs[0] / "report.json").read_text())["runs"] == rep.runs
    # zero unmasked noise: a fresh run against legacy passes everything that was recorded
    run = recorder.run_suite(proj, legacy.base_url)
    pending_spec = [c.id for c in load_cases(proj) if c.expect.source == "specified"]
    assert pending_spec and run.ok
    assert run.totals == {"pass": len(rep.by_status("recorded")) - len(pending_spec), "unrecorded": len(pending_spec)}
    assert (lay.characterization / "results" / run.run_id / "results.json").exists()


def test_second_base_url_masks_instance_differences(proj):
    with LegacyService(instance="a") as a, LegacyService(instance="b") as b:
        cid = _case_for(proj, "EP-0001").id
        rep = recorder.record_cases(proj, a.base_url, b.base_url, case_ids=[cid])
        assert rep.base_urls == [a.base_url, b.base_url]
        assert "$.instance" in find_case(proj, cid).normalize
        assert len(a.requests) == 1 and len(b.requests) == 1


def test_rule_contradiction_raises_runtime_finding(proj, legacy):
    rule_case = _case_for(proj, RULE_ID)
    rep = recorder.record_cases(proj, legacy.base_url)
    ff = recorder.load_runtime_findings(proj)
    (f,) = [x for x in ff.findings if x.kind == "contradicts_rule"]
    assert f.item_ids == [RULE_ID] and f.case_ids == [rule_case.id] and f.run_id == rep.runs[0]
    assert "predicts status 409" in f.detail and '"status": 200' in f.observed and f.status == "open"
    assert f.id in rep.findings
    assert find_case(proj, rule_case.id).expect.status == 200          # legacy is still the truth
    # re-recording updates the same finding and keeps a human status
    lay = Phase2Layout(proj)
    data = json.loads(lay.runtime_findings.read_text())
    data["findings"][0]["status"] = "accepted_recorded"
    lay.runtime_findings.write_text(json.dumps(data))
    recorder.record_cases(proj, legacy.base_url)
    again = recorder.load_runtime_findings(proj).findings
    assert [x.id for x in again] == [f.id] and again[0].status == "accepted_recorded"


def test_consistent_5xx_is_recorded_with_legacy_failure_finding(proj, legacy):
    cid = allocate(proj, "CT", key="manual/boom")
    save_case(proj, CharacterizationCase(id=cid, covers=["EP-0001"], kind="http",
                                         request={"method": "GET", "path": "/api/boom"}))
    rep = recorder.record_cases(proj, legacy.base_url, case_ids=[cid])
    assert rep.by_status("recorded") == [cid]
    (f,) = recorder.load_runtime_findings(proj).findings
    assert (f.kind, f.case_ids, f.item_ids) == ("legacy_failure", [cid], ["EP-0001"])
    assert find_case(proj, cid).expect.status == 500


def test_errors_and_unstable_are_triaged_not_recorded(proj):
    cid = _case_for(proj, "EP-0001").id
    rep = recorder.record_cases(proj, "http://127.0.0.1:9", case_ids=[cid])
    (c,) = rep.cases
    assert c.status == "harness_issue" and "both runs" in c.reason and "transport" in c.reason
    assert find_case(proj, cid).expect.status is None
    with LegacyService(flaky_status=True) as flaky:
        rep = recorder.record_cases(proj, flaky.base_url, case_ids=[cid])
    assert rep.cases[0].status == "unstable" and "200 vs 503" in rep.cases[0].reason
    assert find_case(proj, cid).expect.status is None
    assert recorder.load_runtime_findings(proj).findings == []
    with pytest.raises(ValueError, match="invalid base URL"):
        recorder.record_cases(proj, "ftp://127.0.0.1/")
    with pytest.raises(ValueError, match="CT-9999"):
        recorder.record_cases(proj, "http://127.0.0.1:9", case_ids=["CT-9999"])


def test_fix_decision_keeps_spec_and_stores_legacy_observed(proj, legacy):
    bug = _case_for(proj, proj.bug_id)
    recorder.record_cases(proj, legacy.base_url)
    c = find_case(proj, bug.id)
    assert c.expect.source == "specified" and c.expect.status is None
    assert c.expect.legacy_observed["status"] == 200 and len(c.expect.recorded_runs) == 2
    assert bug.id in next(r for r in recorder.coverage_table(proj).rows
                          if r.node_id == proj.bug_id).pending_spec_case_ids
    testgen.approve_spec(proj, bug.id, "bob", status=200, body=c.expect.legacy_observed["body"])
    res = recorder.run_suite(proj, legacy.base_url, case_ids=[bug.id])
    assert res.results[0].outcome == "pass" and res.results[0].expectation_source == "specified"
    row = next(r for r in recorder.coverage_table(proj).rows if r.node_id == proj.bug_id)
    assert row.pending_spec_case_ids == [] and row.decision == "fix" and row.status == "recorded"


def test_run_detects_regression_and_cli_exit_code(proj, legacy):
    recorder.record_cases(proj, legacy.base_url)
    cid = _case_for(proj, "EP-0005", "happy_path").id
    with LegacyService(mode="regressed") as new:
        rep = recorder.run_suite(proj, new.base_url, case_ids=[cid])
        assert rep.results[0].outcome == "fail"
        assert [(d.field, d.expected, d.actual) for d in rep.results[0].diffs] == [("body.$[0].name", "pen", "PEN")]
        res = CliRunner().invoke(main, ["tests", "run", str(proj.root), "--base-url", new.base_url, "--case", cid])
    assert res.exit_code == 1 and "FAIL" in res.output and "body.$[0].name" in res.output
    res = CliRunner().invoke(main, ["tests", "run", str(proj.root), "--base-url", legacy.base_url, "--case", cid])
    assert res.exit_code == 0, res.output


def test_coverage_table_gaps_and_untestable(tmp_path, tmp_path_factory):
    p = m3_project(tmp_path, tmp_path_factory)
    testgen.generate_cases(p, offline=True, node_ids=["EP-0001", "EP-0002"])
    rep = recorder.coverage_table(p)
    rows = {r.node_id: r for r in rep.rows}
    assert set(rows) == {f"EP-000{i}" for i in range(1, 9)} | {RULE_ID, p.quirk_id, p.bug_id}
    assert rows["EP-0001"].status == "cases" and rows["EP-0001"].case_ids
    assert rows[p.quirk_id].quirk and rows[p.bug_id].decision == "fix"
    gaps = recorder.coverage_gaps(p)
    assert "EP-0001" not in gaps and RULE_ID in gaps and p.quirk_id in gaps and len(gaps) == 9
    assert "EP-0001" in recorder.coverage_gaps(p, require_recorded=True)
    mark_untestable(p, "EP-0008", "needs a real payment provider", "alice")
    assert "EP-0008" not in recorder.coverage_gaps(p)
    rows = {r.node_id: r for r in recorder.coverage_table(p).rows}
    assert (rows["EP-0008"].status, rows["EP-0008"].untestable) == ("untestable", "needs a real payment provider")
    with pytest.raises(ValueError, match="not an in-scope"):
        mark_untestable(p, "COMP-0010", "x", "alice")
    with pytest.raises(ValueError, match="reason"):
        mark_untestable(p, "EP-0007", "  ", "alice")
    r = CliRunner()
    res = r.invoke(main, ["tests", "mark-untestable", str(p.root), "EP-0007", "--reason", "admin only", "--by", "bob"])
    assert res.exit_code == 0, res.output
    res = r.invoke(main, ["tests", "coverage", str(p.root), "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["totals"]["untestable"] == 2 and "EP-0007" not in data["gaps"]
    assert json.loads(Phase2Layout(p).test_coverage.read_text())["gaps"] == data["gaps"]
    res = r.invoke(main, ["tests", "coverage", str(p.root), "--fail-on-gaps"])
    assert res.exit_code == 1 and "gaps: " in res.output and "untestable: admin only" in res.output
    testgen.generate_cases(p, offline=True)
    assert recorder.coverage_gaps(p) == []


def test_coverage_with_deferred_blockers_and_plan_slices(tmp_path, tmp_path_factory):
    p = m3_project(tmp_path, tmp_path_factory, fix_bug=False, deferred=True)
    lay = Phase2Layout(p)
    testgen.generate_cases(p, offline=True)                   # before planning: cases/UNPLANNED/
    unplanned = _case_for(p, "EP-0001")
    assert unplanned.slice is None
    plan = {"version": 1, "slices": [{"id": "SLICE-0001", "name": "orders", "kind": "entry", "order": 0,
                                      "node_ids": ["EP-0001", "EP-0002", "EP-0003", RULE_ID]}],
            "excluded_nodes": {"EP-0008": "dead_code"}}
    atomic_write_json(lay.plan_json, plan)
    testgen.generate_cases(p, offline=True)
    orders = _case_for(p, "EP-0001")
    assert orders.id == unplanned.id and orders.slice == "SLICE-0001"
    assert lay.case_file("SLICE-0001", orders.id).exists()
    assert not (lay.cases_dir / "UNPLANNED" / f"{orders.id}.json").exists()      # moved, not copied
    rows = {r.node_id: r for r in recorder.coverage_table(p).rows}
    assert "EP-0008" not in rows and rows["EP-0001"].slice == "SLICE-0001"
    assert rows[p.bug_id].blockers == ["F-0001"]


def test_cli_record_output(proj, legacy):
    res = CliRunner().invoke(main, ["tests", "record", str(proj.root), "--base-url", legacy.base_url])
    assert res.exit_code == 0, res.output
    assert "masked $.generated_at, $.orders[*].seen_at" in res.output and "runtime finding EQ-" in res.output
    res = CliRunner().invoke(main, ["tests", "record", str(proj.root), "--base-url", "http://user:pw@127.0.0.1:1"])
    assert res.exit_code != 0 and "credentials" in res.output
