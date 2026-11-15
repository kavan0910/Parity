"""M0 cumulative cost ledger (migrate/ledger.py)."""
import pytest

from parity_migrate.audit import AuditLog, read_audit, summarize_audit
from parity_migrate.llm.base import BudgetExceeded
from parity_migrate.migrate.ledger import budget_for, project_budget_usd, remaining, spent
from parity_migrate.migrate.paths import Phase2Layout
from parity_migrate.project import Project


@pytest.fixture()
def proj(tmp_path):
    return Project.init(tmp_path / "p", source="s")


def _log(p):
    return AuditLog(p.audit_dir / "audit.jsonl")


def test_empty_project(proj):
    led = spent(proj)
    assert led.usd == 0 and led.calls == 0 and remaining(proj, 10) == 10 and remaining(proj, None) is None


def test_sums_llm_calls_across_sessions(proj):
    a = _log(proj)
    a.record("analyst", "llm_call", tokens_in=100, tokens_out=50, cost_usd=0.25, task="chunk_extract")
    a.record("analyst", "analyze_chunks", duration_ms=5.0)                      # span: no cost
    a.record("codegen", "llm_call", tokens_in=10, tokens_out=5, cost_usd=0.5, task="codegen", slice="SLICE-0001")
    a.record("codegen", "llm_call", cost_usd=0.0, cached=True, task="codegen", slice="SLICE-0001")
    with open(proj.audit_dir / "audit.jsonl", "a") as f:
        f.write("{truncated\n")
    led = spent(proj)
    assert led.usd == 0.75 and led.calls == 3 and led.cached_calls == 1 and led.bad_lines == 1
    assert led.tokens_in == 110 and led.tokens_out == 55 and led.tokens == 165
    assert led.by_agent == {"analyst": 0.25, "codegen": 0.5}
    assert led.by_task == {"chunk_extract": 0.25, "codegen": 0.5} and led.by_slice == {"SLICE-0001": 0.5}
    assert remaining(proj, 1.0) == 0.25 and remaining(proj, 0.5) == 0.0


def test_ledger_matches_audit_totals(proj):
    a = _log(proj)
    for c in (0.1, 0.2, 0.3):
        a.record("verifier", "llm_call", cost_usd=c, tokens_in=1, tokens_out=1)
    assert spent(proj).usd == summarize_audit(read_audit(proj.audit_dir / "audit.jsonl"))["cost_usd"]


def test_budget_for_caps_at_remaining_and_max_usd(proj):
    _log(proj).record("analyst", "llm_call", cost_usd=4.0)
    assert budget_for(proj, 10.0).max_usd == 6.0
    assert budget_for(proj, 10.0, max_usd=2.0).max_usd == 2.0
    assert budget_for(proj, None, max_usd=3.0).max_usd == 3.0
    assert budget_for(proj, None).max_usd is None
    b = budget_for(proj, 10.0, max_tokens=100)
    assert b.max_tokens == 100 and b.spent_usd == 0
    with pytest.raises(BudgetExceeded):
        b.check(6.5, 1)
    with pytest.raises(BudgetExceeded, match="exhausted"):
        budget_for(proj, 4.0)


def test_project_budget_from_target_yaml(proj):
    assert project_budget_usd(proj) is None
    lay = Phase2Layout(proj)
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text("kind: version_upgrade\nsource: {language: java}\ntarget: {language: java}\n"
                               "budget_usd: 25\n")
    assert project_budget_usd(proj) == 25
    lay.target_yaml.write_text("kind: nope\n")
    assert project_budget_usd(proj) is None
