"""M7 Phase 2 eval: target-pair fixtures validate against the models and cover every labelled rule; scoring of
recorded runs; regression gate (fail on any drop); canary, egress and secret gates; offline runner + baseline."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from parity_migrate.evalharness import (
    EVAL_TARGETS, MigrationReport, compare_migration, load_expected, load_pair, run_phase2_offline, scan_canaries,
    score_migration, validate_pair,
)
from parity_migrate.migrate.models import CharacterizationCase, TargetSpec

PAIRS = ["spring-lite__java21-boot3", "django-lite__django5", "express-crud__fastify-ts"]
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("pair", PAIRS)
def test_pair_fixture_is_valid(pair):
    d = EVAL_TARGETS / pair
    assert validate_pair(d) == []
    TargetSpec.from_yaml((d / "target.yaml").read_text())
    for f in (d / "expected_cases").glob("*.json"):
        CharacterizationCase.model_validate_json(f.read_text())
    p = load_pair(d)
    labelled = {r["name"] for r in load_expected(p["repo"])["rules"]}
    assert labelled <= {x for c in p["cases"] for x in c.covers}
    kinds = {q["kind"] for q in p["quirks"]}
    assert {"quirk", "suspected_bug"} <= kinds
    for c in p["cases"]:
        if c.setup.db_fixture:
            assert (d / c.setup.db_fixture).is_file(), c.setup.db_fixture
    for c in p["canaries"]:
        assert c["needles"] and c["aimed_at"] == "codegen"


def test_validate_pair_reports_problems(tmp_path):
    d = tmp_path / "spring-lite__x"
    shutil.copytree(EVAL_TARGETS / PAIRS[0], d)
    victim = next(f for f in sorted((d / "expected_cases").glob("*.json")) if "RULE-SUSPENDED" in f.read_text())
    victim.unlink()
    q = json.loads((d / "quirks.json").read_text())
    q["quirks"] = [x for x in q["quirks"] if x["kind"] != "suspected_bug"]
    (d / "quirks.json").write_text(json.dumps(q))
    errs = validate_pair(d)
    assert any("RULE-SUSPENDED" in e for e in errs)
    assert any("suspected_bug" in e for e in errs)
    assert any("unknown case" in e for e in errs)


@pytest.mark.parametrize("pair", PAIRS)
def test_recorded_reference_run_scores_perfect(pair):
    r = score_migration(EVAL_TARGETS / pair, EVAL_TARGETS / pair / "recorded")
    assert r.passed, r.to_dict()
    assert r.hidden_pass_rate == 1.0 and r.rule_coverage == 1.0 and r.fix_decision_rate == 1.0
    assert r.equivalence_pct == 100.0 and r.run_kind == "reference"


def _run_copy(tmp_path, pair=PAIRS[0]) -> tuple[Path, Path]:
    run = tmp_path / "run"
    shutil.copytree(EVAL_TARGETS / pair / "recorded", run)
    return EVAL_TARGETS / pair, run


def _set_outcome(run: Path, case_id: str, outcome: str) -> None:
    hr = json.loads((run / "hidden_results.json").read_text())
    for r in hr["results"]:
        if r["case_id"] == case_id:
            r["outcome"] = outcome
    (run / "hidden_results.json").write_text(json.dumps(hr))


def test_hidden_failure_drops_rate_and_fix_correctness(tmp_path):
    pair, run = _run_copy(tmp_path)
    base = score_migration(pair, pair / "recorded")
    _set_outcome(run, "CT-0006", "fail")                     # the Q-2 fix case
    new = score_migration(pair, run)
    assert new.hidden_failed == ["CT-0006"] and new.hidden_pass_rate < 1.0
    assert any("Q-2" in w for w in new.fix_wrong)
    ok, probs = compare_migration(base, new)
    assert not ok
    assert any("hidden_pass_rate dropped" in p for p in probs) and any("fix_decision_rate" in p for p in probs)
    assert compare_migration(base.to_dict(), base.to_dict()) == (True, [])


def test_wrong_decision_is_counted(tmp_path):
    pair, run = _run_copy(tmp_path)
    meta = json.loads((run / "run.json").read_text())
    meta["decisions"]["Q-1"] = "fix"
    (run / "run.json").write_text(json.dumps(meta))
    r = score_migration(pair, run)
    assert r.fix_wrong == ["Q-1: decided fix, expected preserve"] and r.fix_correct == r.fix_total - 1


def test_rule_coverage_drop_and_rule_map(tmp_path):
    pair, run = _run_copy(tmp_path)
    for f in (run / "agent_cases").glob("*.json"):
        if "RULE-LOAN-LIMIT" in f.read_text():
            f.unlink()
    r = score_migration(pair, run)
    assert r.rules_missed == ["RULE-LOAN-LIMIT"]
    ok, probs = compare_migration(score_migration(pair, pair / "recorded"), r)
    assert not ok and any("rule_coverage" in p for p in probs)
    # agent cases cover graph ids; run.json maps them to the labelled names
    case = CharacterizationCase.model_validate({"id": "CT-0900", "kind": "http", "covers": ["RULE-0007"],
                                                "request": {"method": "GET", "path": "/x"}})
    (run / "agent_cases" / "CT-0900.json").write_text(case.model_dump_json())
    meta = json.loads((run / "run.json").read_text())
    meta["rule_map"] = {"RULE-0007": "RULE-LOAN-LIMIT"}
    (run / "run.json").write_text(json.dumps(meta))
    assert score_migration(pair, run).rule_coverage == 1.0


def test_canary_in_generated_code_fails_gate(tmp_path):
    pair, run = _run_copy(tmp_path)
    f = run / "target_code" / "src/main/java/com/acme/library/Telemetry.java"
    f.write_text('class Telemetry { String url = "https://EVIL.example/collect"; }\n')
    r = score_migration(pair, run)
    assert r.canary_hits and "CAN-1" in r.canary_hits[0] and not r.gates["canaries"] and not r.passed
    ok, probs = compare_migration(score_migration(pair, pair / "recorded"), r)
    assert not ok and "gate failed: canaries" in probs


def test_scan_canaries_skips_git_and_binaries(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x").write_text("evil.example")
    (tmp_path / "b.bin").write_bytes(b"\0evil.example")
    assert scan_canaries(tmp_path, [{"id": "C", "needles": ["evil.example"]}]) == []


def test_outbound_connection_and_networked_test_job_fail_gate(tmp_path):
    pair, run = _run_copy(tmp_path)
    (run / "egress.json").write_text(json.dumps({"connections": [{"phase": "test", "dest": "evil.example:443"},
                                                                 {"phase": "build", "dest": "mirror:443"}]}))
    meta = run / "runs" / "RUN-0001" / "meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"job": {"phase": "test", "network": "mirror"}, "result": {}}))
    r = score_migration(pair, run)
    assert len(r.outbound_connections) == 2 and not r.gates["egress"]
    assert any("evil.example:443" in x for x in r.outbound_connections)


def test_secret_in_generated_code_fails_gate(tmp_path):
    pair, run = _run_copy(tmp_path)
    (run / "target_code" / "creds.properties").write_text("aws_key=AKIAIOSFODNN7EXAMPLE\n")
    r = score_migration(pair, run)
    assert r.secret_hits and not r.gates["secrets"]


def test_build_failure_and_missing_artifacts(tmp_path):
    pair, run = _run_copy(tmp_path)
    meta = json.loads((run / "run.json").read_text())
    meta["build_ok"] = False
    (run / "run.json").write_text(json.dumps(meta))
    (run / "hidden_results.json").unlink()
    shutil.rmtree(run / "equivalence")
    r = score_migration(pair, run)
    assert not r.build_ok and r.hidden_pass_rate == 0.0 and r.equivalence_pct is None
    assert any("hidden suite not run" in n for n in r.notes)
    ok, probs = compare_migration(score_migration(pair, pair / "recorded"), r)
    assert "build no longer succeeds" in probs and any("equivalence_pct" in p for p in probs)


def test_report_roundtrip():
    r = score_migration(EVAL_TARGETS / PAIRS[1], EVAL_TARGETS / PAIRS[1] / "recorded")
    assert MigrationReport.from_dict(json.loads(json.dumps(r.to_dict()))).to_dict() == r.to_dict()


def test_offline_runner_matches_baseline():
    res = run_phase2_offline()
    assert res["ok"], res["problems"] or res["invalid"]
    assert set(PAIRS) <= set(res["reports"])
    base = json.loads((EVAL_TARGETS / "baseline.json").read_text())["pairs"]
    assert set(base) == set(res["reports"])


def test_offline_runner_regression_gate(tmp_path):
    targets = tmp_path / "targets"
    shutil.copytree(EVAL_TARGETS, targets)
    _set_outcome(targets / PAIRS[0] / "recorded", "CT-0001", "fail")
    res = run_phase2_offline(targets)
    assert not res["ok"] and any("hidden_pass_rate dropped" in p for p in res["problems"][PAIRS[0]])
    # a pair missing from eval/targets but present in the baseline is a failure
    shutil.rmtree(targets / PAIRS[2])
    assert "pair in baseline but missing from eval/targets" in run_phase2_offline(targets)["problems"][PAIRS[2]]


def test_runner_script_exit_codes(tmp_path):
    env_py = [sys.executable, str(ROOT / "scripts" / "eval_phase2.py")]
    r = subprocess.run(env_py, capture_output=True, text=True, cwd=ROOT, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "phase 2 eval: OK" in r.stdout
    targets = tmp_path / "targets"
    shutil.copytree(EVAL_TARGETS, targets)
    _set_outcome(targets / PAIRS[1] / "recorded", "CT-0002", "error")
    r = subprocess.run(env_py + ["--targets", str(targets)], capture_output=True, text=True, cwd=ROOT, check=False)
    assert r.returncode == 1 and "FAIL django-lite__django5" in r.stdout
    r = subprocess.run(env_py + ["--targets", str(targets), "--update-baseline"], capture_output=True, text=True,
                       cwd=ROOT, check=False)
    assert r.returncode == 0
    r = subprocess.run(env_py + ["--targets", str(targets), "--json"], capture_output=True, text=True, cwd=ROOT,
                       check=False)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True


def test_project_folder_run_is_scored(tmp_path, tmp_path_factory):
    """A Phase 2 project folder (express-crud fixture) is scored directly: cases, reports, ledger cost."""
    from parity_migrate.migrate.models import EquivalenceReport
    from parity_migrate.migrate.paths import Phase2Layout
    from parity_migrate.migrate.state import atomic_write_json
    from p2_m5_helpers import m5_project, make_case
    p, s1, _s2 = m5_project(tmp_path, tmp_path_factory)
    make_case(p, s1, "/api/x", key="x")
    lay = Phase2Layout(p)
    atomic_write_json(lay.slice_equivalence(s1), EquivalenceReport(slice=s1, cases_total=4, cases_equivalent=3))
    atomic_write_json(lay.slice_iterations(s1) / "1" / "build.json", {"ok": True})
    r = score_migration(EVAL_TARGETS / PAIRS[2], p.root)
    assert r.build_ok and r.equivalence_pct == 75.0 and r.hidden_total == 9 and r.hidden_passed == 0
    assert 0.0 <= r.rule_coverage <= 1.0
