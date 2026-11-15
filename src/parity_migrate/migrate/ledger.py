"""M0 cumulative cost ledger: project spend across sessions, from audit/audit.jsonl (docs/phase2-plan.md 5.5).

`llm.litellm_client.Budget` is per process, so a resumed run would start at $0. The ledger sums every
`llm_call` audit record (the only records that carry real model cost; cached calls are recorded with
cost 0), so caps hold across sessions:

    from parity_migrate.migrate.ledger import budget_for
    budget = budget_for(project, spec.budget_usd, max_usd=cli_max_usd)   # Budget(max_usd=remaining)
    llm = LiteLLMClient(config, budget=budget, audit=...)

The audit log is append-only and every line is one atomic write; unparseable lines are skipped and counted
(`Ledger.bad_lines`) instead of failing the ledger.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

COST_ACTIONS = frozenset({"llm_call"})


class Ledger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    cached_calls: int = 0
    by_agent: dict[str, float] = Field(default_factory=dict)
    by_task: dict[str, float] = Field(default_factory=dict)
    by_slice: dict[str, float] = Field(default_factory=dict)   # records tagged slice=SLICE-NNNN
    bad_lines: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def _audit_path(project) -> Path:
    return Path(project.audit_dir) / "audit.jsonl"


def spent(project) -> Ledger:
    """Cumulative spend of the whole project (Phase 1 and Phase 2) from the audit log."""
    led = Ledger()
    p = _audit_path(project)
    if not p.exists():
        return led
    usd = 0.0
    with open(p, "rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                led.bad_lines += 1
                continue
            if not isinstance(e, dict) or e.get("action") not in COST_ACTIONS:
                continue
            try:
                cost = float(e.get("cost_usd") or 0.0)
                tin, tout = int(e.get("tokens_in") or 0), int(e.get("tokens_out") or 0)
            except (TypeError, ValueError):
                led.bad_lines += 1
                continue
            usd += cost
            led.tokens_in += tin
            led.tokens_out += tout
            led.calls += 1
            led.cached_calls += 1 if e.get("cached") else 0
            for key, bucket in (("agent", led.by_agent), ("task", led.by_task), ("slice", led.by_slice)):
                v = e.get(key)
                if isinstance(v, str) and v:
                    bucket[v] = round(bucket.get(v, 0.0) + cost, 6)
    led.usd = round(usd, 6)
    return led


def remaining(project, budget_usd: float | None) -> float | None:
    """USD left of the project budget (never negative); None when no project budget is set."""
    if budget_usd is None:
        return None
    return round(max(0.0, float(budget_usd) - spent(project).usd), 6)


def project_budget_usd(project) -> float | None:
    """`budget_usd` from target/target.yaml, or None when there is no (valid) target spec yet."""
    from .models import TargetSpec
    from .paths import Phase2Layout
    p = Phase2Layout(project).target_yaml
    if not p.exists():
        return None
    try:
        return TargetSpec.from_yaml(p.read_text()).budget_usd
    except Exception:  # noqa: BLE001 - an invalid spec is reported by `target` commands, not here
        return None


def budget_for(project, budget_usd: float | None, *, max_usd: float | None = None,
               max_tokens: int | None = None):
    """An llm Budget capped at min(remaining project budget, max_usd). Raises BudgetExceeded if nothing is left."""
    from ..llm.base import BudgetExceeded
    from ..llm.litellm_client import Budget
    rem = remaining(project, budget_usd)
    caps = [x for x in (rem, max_usd) if x is not None]
    cap = min(caps) if caps else None
    if rem is not None and rem <= 0:
        raise BudgetExceeded(f"project budget exhausted: spent {spent(project).usd:.4f} of {budget_usd:.4f} USD")
    return Budget(max_usd=cap, max_tokens=max_tokens)
