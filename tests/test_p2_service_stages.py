"""M7 service.run_stage Phase 2 stages: target_fit, plan, tests, generate, equivalence, cutover; progress and usage
events, budget stop, failures returned (never raised), Phase 1 stages unchanged."""
import pytest

from migration_agent import service
from migration_agent.llm.base import BudgetExceeded
from migration_agent.migrate import cutover as co
from migration_agent.migrate.codegen import OfflineCodegenLLM
from migration_agent.migrate.exec.fake import FakeExecBackend
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import load_state

from p2_m4_helpers import m4_project, plan, ready_slice
from p2_m5_helpers import Svc, m5_project, standard_cases, walk_to_awaiting


class PricedCodegen(OfflineCodegenLLM):
    """Offline codegen with a fake price per call (usage events must carry it)."""

    def complete(self, **kw):
        r = super().complete(**kw)
        return r.model_copy(update={"cost_usd": 0.01, "tokens_in": 10, "tokens_out": 5, "model": "fake"})


class BrokeLLM:
    def complete(self, **kw):
        raise BudgetExceeded("budget reached")


@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return m4_project(tmp_path, tmp_path_factory)


def _run(p, stage, **kw):
    events = []
    res = service.run_stage(p.root, stage, on_event=events.append, **kw)
    return res, events


def _progress(events, stage):
    return [e for e in events if e["type"] == "progress" and e["stage"] == stage]


def test_stage_lists():
    assert service.STAGES == ("ingest", "extract", "analyze", "verify", "resolve")
    assert service.P2_STAGES == ("target_fit", "plan", "tests", "generate", "equivalence", "cutover")
    with pytest.raises(service.ServiceError):
        service.run_stage("/nonexistent", "deploy")


def test_target_fit_offline(proj):
    res, ev = _run(proj, "target_fit", offline=True)
    assert res.status == "ok", res.error
    assert set(res.summary) >= {"risks", "by_severity", "blockers", "model_calls"}
    assert Phase2Layout(proj).fit_report_json.exists()
    assert _progress(ev, "target_fit")[-1] == {"type": "progress", "stage": "target_fit", "done": 1, "total": 1}


def test_plan_stage_dry_run_and_real(proj):
    res, _ = _run(proj, "plan", offline=True)
    assert res.status == "failed" and "target fit is required" in res.error     # planner gate, returned not raised
    assert _run(proj, "target_fit", offline=True)[0].status == "ok"
    res, _ = _run(proj, "plan", offline=True, options={"dry_run": True})
    assert res.status == "ok", res.error
    assert res.summary["dry_run"] is True and res.summary["slices"] > 0
    res, _ = _run(proj, "plan", offline=True)
    assert res.status == "ok" and res.summary["version"] >= 1


def test_tests_stage_offline(proj):
    res, ev = _run(proj, "tests", offline=True)
    assert res.status == "ok", res.error
    assert res.summary["created"] + res.summary["kept"] > 0
    assert _progress(ev, "tests")


def test_generate_stage_emits_usage_and_cost(proj, monkeypatch):
    import migration_agent.migrate.exec as ex
    monkeypatch.setattr(ex, "get_backend", lambda project: FakeExecBackend(project))
    sid = ready_slice(proj).id
    res, ev = _run(proj, "generate", llm=PricedCodegen(), options={"slice_id": sid})
    assert res.status == "ok", res.error
    assert res.summary["outcomes"][0]["slice_id"] == sid
    usage = [e for e in ev if e["type"] == "usage"]
    assert usage and all(e["stage"] == "generate" and e["usd"] == 0.01 for e in usage)
    assert res.cost_usd == pytest.approx(0.01 * len(usage))
    assert load_state(proj, sid).status != "planned"


def test_generate_stage_offline_client(proj, monkeypatch):
    import migration_agent.migrate.exec as ex
    monkeypatch.setattr(ex, "get_backend", lambda project: FakeExecBackend(project))
    res, _ = _run(proj, "generate", offline=True, options={"slice_id": ready_slice(proj).id})
    assert res.status == "ok" and res.cost_usd == 0.0


def test_generate_stage_budget_stop_is_graceful(proj, monkeypatch):
    import migration_agent.migrate.exec as ex
    monkeypatch.setattr(ex, "get_backend", lambda project: FakeExecBackend(project))
    res, _ = _run(proj, "generate", llm=BrokeLLM(), options={"slice_id": ready_slice(proj).id})
    assert res.status == "stopped_budget" and "budget reached" in res.error


def test_equivalence_stage(tmp_path, tmp_path_factory):
    p, s1, _s2 = m5_project(tmp_path, tmp_path_factory)
    with Svc("legacy") as legacy, Svc("new") as new:
        standard_cases(p, s1, legacy.base_url)
        walk_to_awaiting(p, s1)
        res, ev = _run(p, "equivalence", options={"slice_id": s1, "legacy_url": legacy.base_url,
                                                   "new_url": new.base_url})
    assert res.status == "ok", res.error
    assert res.summary["slices"][s1]["cases"] == 3 and res.summary["unexplained"] == 0
    assert _progress(ev, "equivalence")[-1]["done"] == 1


def test_equivalence_stage_without_plan_fails_cleanly(tmp_path, tmp_path_factory):
    from phase2_helpers import approved_project
    p = approved_project(tmp_path, tmp_path_factory)
    res, _ = _run(p, "equivalence", options={"legacy_url": "http://127.0.0.1:1", "new_url": "http://127.0.0.1:2"})
    assert res.status == "failed" and "plan" in res.error


def test_cutover_stage_not_ready_and_refused_approve(proj):
    res, _ = _run(proj, "cutover")
    assert res.status == "ok" and res.summary["ready"] is False and res.summary["blockers"]
    assert Phase2Layout(proj).cutover_plan.exists()
    res, _ = _run(proj, "cutover", options={"approve": True, "by": "alice"})
    assert res.status == "failed" and "cannot be approved" in res.error and res.summary["blockers"]
    assert not Phase2Layout(proj).cutover_approval.exists()


def test_cutover_stage_approves_when_ready(proj, monkeypatch):
    from migration_agent.migrate.cutover import Readiness
    from p2_m5_helpers import init_target
    head = init_target(proj)
    monkeypatch.setattr(co, "readiness", lambda project: Readiness(ready=True))
    res, _ = _run(proj, "cutover", options={"approve": True, "by": "alice"})
    assert res.status == "ok", res.error
    assert res.summary["approved"]["approved_by"] == "alice" and res.summary["approved"]["tree_commit"] == head
    assert Phase2Layout(proj).cutover_approval.exists()


def test_stage_on_missing_project_fails(tmp_path):
    res = service.run_stage(tmp_path / "nope", "plan")
    assert res.status == "failed" and res.error


def test_contract_gate_failure_is_reported(proj):
    from phase2_helpers import edit_contract
    edit_contract(proj, graph_sha256="0" * 64)
    res, _ = _run(proj, "target_fit", offline=True)
    assert res.status == "failed" and "contract" in res.error.lower()
    assert plan(proj) is not None       # nothing else was touched
