"""Final cut-over (docs/phase2-plan.md section 8, M7): readiness gate, cutover/plan.md and cutover/approval.json.

    parity cutover PROJECT [--format text|json]          readiness + (re)write cutover/plan.md
    parity cutover PROJECT --approve --by NAME [--note T] approve (refused while not ready)

Readiness (`readiness(project)`), every failing check is a blocker:
  contract      the contract gate passes (`require_contract`)
  plan          plan/migration_plan.json exists and is valid
  slices        every slice of the plan is `approved` with a valid slices/SLICE-NNNN/approval.json
  equivalence   every slice has an equivalence report without regression/unknown diffs
  full_suite    the latest FULL-SUITE run on the final tree is green: `equivalence/composite.json`
                (EquivalenceReport, slice null) + `equivalence/composite_run.json` (run meta: created, target_head,
                new_url, cases[{case_id}]) when present, else the newest slice equivalence run whose slice +
                regression slices cover every slice of the plan. It must cover every case of the plan's slices,
                have no regression/unknown diff and have run on the current target_code HEAD. No such run is a
                blocker ("composite report missing"), never a pass.
  target_code   target_code/ exists; a git repo must be clean (no uncommitted edits)
  coverage      `recorder.coverage_gaps` is empty
  config        `configmap.config_gaps` is empty
  schema        `schemacompat.schema_blockers` is empty; a schema-compat report with status incompatible /
                invalid / error blocks (a missing report is a warning)
  findings      per slice: plan blockers and contract deferred findings (F-) accepted (`slice accept-observed`),
                RISK- blockers resolved (`target resolve`), no open slice decisions; deferred findings that touch no
                slice are blockers
  budget        cumulative spend (migrate/ledger.py) within target.yaml budget_usd (no budget: warning)

cutover/plan.md is deterministic (no timestamps) so re-rendering an unchanged project keeps the approved hash:
route-flip order (plan order + harness route table), rollback per step, config/secret checklist (names only, the
config map never holds values), schema steps (expand/backfill/contract specs from schemacompat), monitoring per
endpoint, open risks. `--approve` re-checks readiness, re-renders the plan and writes cutover/approval.json
(`CutoverApproval`: sha256 of docs/migration_contract.json, of cutover/plan.md, the target_code HEAD sha (or
`tree-sha256:<hex>` of the files when target_code/ is not a git repo) and one digest over every equivalence
artifact) plus cutover/hashes.json listing the per-file hashes behind that digest. `verify_approval` recomputes
them (stale approval = something changed after approval).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, ConfigDict, Field

from .models import CutoverApproval, EquivalenceReport, MigrationPlan
from .paths import Phase2Layout
from .state import atomic_write_json, atomic_write_text, file_lock, now_iso, read_json, sha256_file

COMPOSITE_REPORT = "composite.json"
COMPOSITE_RUN = "composite_run.json"
HASHES_FILE = "hashes.json"
_SKIP_TREE = frozenset({".git"})


class CutoverError(RuntimeError):
    """Refused approval or invalid input; `blockers` lists every reason."""

    def __init__(self, msg: str, blockers: list[str] | None = None):
        super().__init__(msg)
        self.blockers = blockers or []


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Check(_Strict):
    name: str
    ok: bool
    detail: str = ""
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FullSuite(_Strict):
    source: str = ""                 # project-relative report file ("" when none)
    kind: str = ""                   # composite | slice
    created: str = ""
    target_head: str = ""
    new_url: str = ""
    cases_total: int = 0
    cases_equivalent: int = 0
    unexplained: list[str] = Field(default_factory=list)
    missing_cases: list[str] = Field(default_factory=list)


class Readiness(_Strict):
    ready: bool
    checks: list[Check] = Field(default_factory=list)
    full_suite: FullSuite | None = None
    target_head: str = ""

    @property
    def blockers(self) -> list[str]:
        return [f"{c.name}: {b}" for c in self.checks for b in c.blockers]

    @property
    def warnings(self) -> list[str]:
        return [f"{c.name}: {w}" for c in self.checks for w in c.warnings]


# ------------------------------------------------------------------ helpers
def _lay(project) -> Phase2Layout:
    return Phase2Layout(project)


def composite_report_path(project) -> Path:
    return _lay(project).equivalence_dir / COMPOSITE_REPORT


def composite_run_path(project) -> Path:
    return _lay(project).equivalence_dir / COMPOSITE_RUN


def _audit(project, action: str, **fields) -> None:
    from ..audit import AuditLog
    try:
        AuditLog(Path(project.root) / "audit" / "audit.jsonl").record("cutover", action, **fields)
    except OSError:
        pass


def _redact(text: str, limit: int) -> str:
    from ..ingest.secrets import redact_string
    return redact_string(str(text or "").strip())[:limit]


def _first_line(e: BaseException) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:300]


def _plan(project) -> MigrationPlan | None:
    """The plan (None when missing). A corrupt plan raises ValueError."""
    p = _lay(project).plan_json
    if not p.exists():
        return None
    return MigrationPlan.model_validate_json(p.read_text())


def _ordered(plan: MigrationPlan | None) -> list:
    return sorted(plan.slices, key=lambda s: (s.order, s.id)) if plan is not None else []


def _graph(project):
    from ..graph.models import Graph
    try:
        from .harness import approved_graph
        return approved_graph(project)
    except Exception:  # noqa: BLE001 - the contract check reports why
        return Graph()


def _git_head(project) -> str:
    from .approvals import git_head
    return git_head(project)


def tree_hash(root: Path) -> str:
    """sha256 over sorted `relpath\\0sha256` of the regular files under `root` (no .git, no symlinks)."""
    h = hashlib.sha256()
    if root.is_dir():
        rows = []
        for dp, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_TREE)
            for n in sorted(names):
                p = Path(dp) / n
                if p.is_symlink() or not p.is_file():
                    continue
                rows.append((p.relative_to(root).as_posix(), sha256_file(p)))
        for rel, sha in sorted(rows):
            h.update(f"{rel}\0{sha}\n".encode())
    return h.hexdigest()


def tree_commit(project) -> str:
    """target_code HEAD sha, else `tree-sha256:<hex>` of its files ("" when target_code/ does not exist)."""
    head = _git_head(project)
    if head:
        return head
    tc = _lay(project).target_code
    return f"tree-sha256:{tree_hash(tc)}" if tc.is_dir() else ""


def _load_composite_report(project):
    """equivalence/composite.json as written by `equivalence composite` (equivalence.CompositeReport), or None
    when absent or in the older EquivalenceReport shape (handled by _load_composite)."""
    rp = composite_report_path(project)
    if not rp.exists():
        return None
    from .equivalence import CompositeReport
    try:
        return CompositeReport.model_validate_json(rp.read_text())
    except ValueError:
        return None


def _load_composite(project) -> tuple[EquivalenceReport | None, dict]:
    rp, mp = composite_report_path(project), composite_run_path(project)
    rep = EquivalenceReport.model_validate_json(rp.read_text()) if rp.exists() else None
    meta = read_json(mp, default={}) or {}
    return rep, meta if isinstance(meta, dict) else {}


# thin wrappers (patched in tests; keep the gate functions owned by their modules)
def _coverage_gaps(project) -> list[str]:
    from .recorder import coverage_gaps
    return coverage_gaps(project)


def _config_gaps(project) -> list[dict]:
    from .configmap import config_gaps
    return config_gaps(project)


def _schema_blockers(project) -> dict[str, list[str]]:
    from .schemacompat import schema_blockers
    return schema_blockers(project)


def _schema_report(project):
    from .schemacompat import load_report
    return load_report(project)


# ------------------------------------------------------------------ readiness
def _check(name: str, fn, *args) -> Check:
    """Run one check; an unexpected error is a blocker (never read as ready)."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return Check(name=name, ok=False, detail="check failed", blockers=[f"cannot evaluate: {_first_line(e)}"])


def _c_contract(project) -> Check:
    from .contract import ContractError, require_contract
    try:
        require_contract(project)
    except ContractError as e:
        return Check(name="contract", ok=False, blockers=[_first_line(e)])
    return Check(name="contract", ok=True, detail="contract gate passes")


def _c_slices(project, plan) -> Check:
    from .approvals import load_approval
    from .state import load_state
    bl = []
    for s in _ordered(plan):
        st = load_state(project, s.id).status
        try:
            appr = load_approval(project, s.id)
        except ValueError:
            appr = None
            bl.append(f"{s.id}: approval.json is invalid")
            continue
        if st != "approved":
            bl.append(f"{s.id} ({s.name}) is {st!r}, not approved")
        elif appr is None or appr.slice_id != s.id:
            bl.append(f"{s.id}: state approved but no slices/{s.id}/approval.json")
    n = len(plan.slices)
    return Check(name="slices", ok=not bl, detail=f"{n - len(bl)}/{n} slice(s) approved" if n else "no slices",
                 blockers=bl if n else ["the plan has no slices"])


def _c_equivalence(project, plan) -> Check:
    from .equivalence import load_report
    bl = []
    for s in _ordered(plan):
        rep = load_report(project, s.id)
        if rep is None:
            bl.append(f"{s.id}: no equivalence report")
        elif rep.unexplained:
            bl.append(f"{s.id}: {len(rep.unexplained)} regression/unknown diff(s): "
                      + ", ".join(d.id for d in rep.unexplained[:10]))
    warn = []
    try:
        from .differential import load_fuzz_reports
        for fz in load_fuzz_reports(project):
            if fz.high:
                warn.append(f"{fz.slice}: differential fuzzing found {len(fz.high)} high-severity divergence(s) "
                            "(advisory; see equivalence/report.md)")
            elif fz.divergences:
                warn.append(f"{fz.slice}: differential fuzzing found {len(fz.divergences)} divergence(s) (advisory)")
        for s in _ordered(plan):
            from .equivalence import load_run_meta
            meta = load_run_meta(project, s.id)
            slow = ((meta.latency or {}).get("slower") if meta else None) or []
            if slow:
                warn.append(f"{s.id}: {len(slow)} case(s) answered much slower by the new service (advisory latency "
                            "comparison)")
    except Exception:  # noqa: BLE001 - advisory checks never break readiness
        pass
    return Check(name="equivalence", ok=not bl, detail="every slice report clean" if not bl else "", blockers=bl,
                 warnings=warn)


def _full_suite_candidates(project, plan) -> list[FullSuite]:
    from .equivalence import load_report, load_run_meta, run_meta_path
    lay = _lay(project)
    all_ids = {s.id for s in plan.slices}
    out: list[FullSuite] = []
    comp = _load_composite_report(project)
    if comp is not None:
        ran = [c for c in comp.cases if c.outcome not in ("not_implemented",)]
        bad = [c.case_id for c in ran if c.outcome in ("fail", "error")]
        have = {c.case_id for c in comp.cases}
        from .casestore import load_cases
        want = sorted(c.id for c in load_cases(project) if c.slice in all_ids)
        out.append(FullSuite(source=lay.rel(composite_report_path(project)), kind="composite", created=comp.created,
                             target_head=comp.target_head, new_url=comp.base_url, cases_total=len(ran),
                             cases_equivalent=len(ran) - len(bad), unexplained=bad))
        out[-1].missing_cases = [c for c in want if c not in have]
    rep, meta = (None, {}) if comp is not None else _load_composite(project)
    if rep is not None:
        out.append(FullSuite(source=lay.rel(composite_report_path(project)), kind="composite",
                             created=str(meta.get("created") or rep.created), target_head=str(meta.get("target_head")
                                                                                              or ""),
                             new_url=str(meta.get("new_url") or ""), cases_total=rep.cases_total,
                             cases_equivalent=rep.cases_equivalent, unexplained=[d.id for d in rep.unexplained]))
        out[-1].missing_cases = _missing(project, plan, meta, rep)
    for s in _ordered(plan):
        m = load_run_meta(project, s.id)
        r = load_report(project, s.id)
        if m is None or r is None or ({s.id, *m.regression_slices} != all_ids):
            continue
        fs = FullSuite(source=lay.rel(run_meta_path(project, s.id)), kind="slice", created=m.created,
                       target_head=m.target_head, new_url=m.new_url, cases_total=r.cases_total,
                       cases_equivalent=r.cases_equivalent, unexplained=[d.id for d in r.unexplained])
        fs.missing_cases = _missing(project, plan, {"cases": [c.model_dump() for c in m.cases]}, r)
        out.append(fs)
    return out


def _missing(project, plan, meta: dict, rep: EquivalenceReport) -> list[str]:
    from .casestore import load_cases
    ids = {s.id for s in plan.slices}
    want = sorted(c.id for c in load_cases(project) if c.slice in ids)
    cases = meta.get("cases")
    if isinstance(cases, list) and cases:
        have = {str(c.get("case_id")) for c in cases if isinstance(c, dict)}
        return [c for c in want if c not in have]
    return want[rep.cases_total:] if rep.cases_total < len(want) else []


def _c_full_suite(project, plan, head: str) -> tuple[Check, FullSuite | None]:
    cands = _full_suite_candidates(project, plan)
    if not cands:
        return Check(name="full_suite", ok=False, blockers=[
            "no full-suite run on the final tree: composite report equivalence/composite.json is missing and no "
            "slice equivalence run covers every slice (run the full suite against the composite after the last "
            "flip, e.g. `parity equivalence PROJECT --all` with the router URL as --new-url)"]), None
    fs = max(cands, key=lambda c: (c.created, c.kind == "composite"))
    bl = []
    if fs.unexplained:
        bl.append(f"latest full-suite run ({fs.source}) has {len(fs.unexplained)} regression/unknown diff(s): "
                  + ", ".join(fs.unexplained[:10]))
    if fs.missing_cases:
        bl.append(f"latest full-suite run ({fs.source}) did not run {len(fs.missing_cases)} case(s): "
                  + ", ".join(fs.missing_cases[:10]))
    if head and fs.target_head != head:
        bl.append(f"latest full-suite run ({fs.source}) ran on target_code {fs.target_head[:12] or '(unknown)'}, "
                  f"HEAD is {head[:12]}: re-run the full suite on the final tree")
    return Check(name="full_suite", ok=not bl, blockers=bl,
                 detail=f"{fs.kind} run {fs.created}: {fs.cases_equivalent}/{fs.cases_total} equivalent"), fs


def _supply_chain_warnings(project) -> list[str]:
    """Advisory: lockfile and pinning findings of the last build of each service (harness/state.json)."""
    out = []
    try:
        from .harness import load_state as _hs
        st = _hs(project)
    except Exception:  # noqa: BLE001
        return out
    for role in ("new", "legacy"):
        sc = (st.get(role) or {}).get("supply_chain") or {}
        for w in (sc.get("warnings") or [])[:5]:
            out.append(f"{role} build: {w}")
    return out


def _c_target_code(project) -> Check:
    tc = _lay(project).target_code
    if not tc.is_dir():
        return Check(name="target_code", ok=False, blockers=["target_code/ does not exist"])
    from . import targetrepo
    if not targetrepo.is_repo(project):
        return Check(name="target_code", ok=True, detail="not a git repository (tree hash is used)",
                     warnings=["target_code/ is not a git repository: approval records a tree hash"])
    if not _git_head(project):
        return Check(name="target_code", ok=False, blockers=["target_code/ has no commit"])
    dirty = targetrepo.dirty_paths(project)
    if dirty:
        return Check(name="target_code", ok=False, blockers=[
            f"{len(dirty)} uncommitted change(s) in target_code/: {', '.join(dirty[:5])}"])
    return Check(name="target_code", ok=True, detail="clean")


def _c_coverage(project) -> Check:
    gaps = _coverage_gaps(project)
    return Check(name="coverage", ok=not gaps, detail="no gaps" if not gaps else "",
                 blockers=[f"{len(gaps)} in-scope item(s) without a case or untestable reason: "
                           + ", ".join(gaps[:15])] if gaps else [])


def _c_config(project) -> Check:
    gaps = _config_gaps(project)
    return Check(name="config", ok=not gaps, detail="no gaps" if not gaps else "",
                 blockers=[(f"{g.get('id')}: " if g.get("id") else "") + str(g.get("reason", "")) for g in gaps])


def _c_schema(project) -> Check:
    bl = [f"{sid}: {r}" for sid, reasons in _schema_blockers(project).items() for r in reasons]
    warn = []
    try:
        rep = _schema_report(project)
    except ValueError as e:
        rep = None
        bl.append(f"data/schema_compat.json is invalid: {_first_line(e)}")
    if rep is None:
        warn.append("no schema-compat report (run `parity config schema-check PROJECT`)")
    elif rep.status in ("incompatible", "invalid", "error"):
        bl.append(f"schema-compat status {rep.status}" + (f": {len(rep.blockers)} blocking diff(s)"
                                                           if rep.blockers else ""))
    elif rep.status in ("unverified", "skipped"):
        warn.append(f"schema-compat status {rep.status}")
    return Check(name="schema", ok=not bl, detail=f"schema-compat {rep.status}" if rep else "", blockers=bl,
                 warnings=warn)


def _c_findings(project, plan) -> Check:
    from .approvals import load_decisions, slice_blockers
    from .contract import check_contract
    from .fit import load_resolutions
    bl = []
    risk_done = set(load_resolutions(project).current())
    accepted_any: set[str] = set()
    for s in _ordered(plan):
        accepted = load_decisions(project, s.id).accepted()
        accepted_any |= accepted
        for b in slice_blockers(project, s.id):
            if b.startswith("F-") and b not in accepted:
                bl.append(f"{s.id}: deferred finding {b} neither resolved nor accepted")
            elif b.startswith("RISK-") and b not in risk_done:
                bl.append(f"{s.id}: fit risk {b} not resolved")
            elif not b.startswith(("F-", "RISK-")):
                bl.append(f"{s.id}: blocker {b}")
        for d in load_decisions(project, s.id).open():
            bl.append(f"{s.id}: open {d.kind} decision on {d.ref}")
    nodes = {n for s in plan.slices for n in s.node_ids}
    for b in check_contract(project).blockers:
        if not (set(b.item_ids) & nodes) and b.finding_id not in accepted_any:
            bl.append(f"deferred finding {b.finding_id} touches no slice and was not accepted")
    return Check(name="findings", ok=not bl, detail="all resolved or accepted" if not bl else "", blockers=bl)


def _c_budget(project) -> Check:
    from .ledger import project_budget_usd, spent
    budget = project_budget_usd(project)
    usd = spent(project).usd
    if budget is None:
        return Check(name="budget", ok=True, detail=f"spent ${usd:.4f}, no project budget",
                     warnings=["no project budget (target/target.yaml budget_usd)"])
    if usd > budget:
        return Check(name="budget", ok=False, blockers=[f"spent ${usd:.4f} exceeds the project budget ${budget:.4f}"])
    return Check(name="budget", ok=True, detail=f"spent ${usd:.4f} of ${budget:.4f}")


def readiness(project) -> Readiness:
    """Every cut-over gate (module docstring). Never raises for gate failures."""
    checks = [_check("contract", _c_contract, project)]
    try:
        plan = _plan(project)
    except ValueError as e:
        plan = None
        checks.append(Check(name="plan", ok=False, blockers=[f"plan/migration_plan.json is invalid: {_first_line(e)}"]))
    else:
        checks.append(Check(name="plan", ok=plan is not None, detail=f"v{plan.version}" if plan else "",
                            blockers=[] if plan else ["no plan: run `parity plan PROJECT`"]))
    head = ""
    try:
        head = _git_head(project)
    except Exception:  # noqa: BLE001
        head = ""
    fs = None
    if plan is not None:
        checks.append(_check("slices", _c_slices, project, plan))
        checks.append(_check("equivalence", _c_equivalence, project, plan))
        try:
            c, fs = _c_full_suite(project, plan, head)
        except Exception as e:  # noqa: BLE001
            c = Check(name="full_suite", ok=False, blockers=[f"cannot evaluate: {_first_line(e)}"])
        checks.append(c)
    checks.append(_check("target_code", _c_target_code, project))
    sc_warn = _supply_chain_warnings(project)
    if sc_warn:
        checks.append(Check(name="supply_chain", ok=True, detail="advisory", warnings=sc_warn))
    checks.append(_check("coverage", _c_coverage, project))
    checks.append(_check("config", _c_config, project))
    checks.append(_check("schema", _c_schema, project))
    if plan is not None:
        checks.append(_check("findings", _c_findings, project, plan))
    checks.append(_check("budget", _c_budget, project))
    return Readiness(ready=all(c.ok for c in checks), checks=checks, full_suite=fs, target_head=head)


# ------------------------------------------------------------------ plan.md
def _md(s: Any, n: int = 160) -> str:
    t = s if isinstance(s, str) else json.dumps(s, default=str)
    t = re.sub(r"\s+", " ", t)[:n]
    return re.sub(r"[|`<>]", lambda m: {"|": "\\|", "`": "'"}.get(m.group(0), " "), t)


def _routes(project, plan) -> dict:
    from .harness import HarnessError, build_route_table, load_flips, slice_approved
    try:
        flips = load_flips(project)
    except HarnessError:
        flips = {}
    approved = {s.id for s in plan.slices if slice_approved(project, s.id)}
    return build_route_table(plan, _graph(project), flips, approved, {"legacy": "legacy", "new": "new"})


def _schema_specs(project, plan) -> list:
    rep = None
    try:
        rep = _schema_report(project)
    except ValueError:
        rep = None
    if rep is not None and rep.changes:
        return list(rep.changes)
    from .schemacompat import change_specs, load_mapping, load_schema_changes, slice_tables
    try:
        changes = load_schema_changes(project).changes
    except Exception:  # noqa: BLE001 - invalid declarations are a schema-check problem, listed as a risk
        return []
    if not changes:
        return []
    try:
        mf = load_mapping(project)
    except Exception:  # noqa: BLE001
        mf = None
    return change_specs(changes, slice_tables(plan, _graph(project)), mf.mappings if mf is not None else None)


def render_plan(project, rd: Readiness | None = None) -> str:
    """cutover/plan.md text (deterministic for an unchanged project)."""
    from .approvals import load_decisions
    from .configmap import load_config_map
    from .equivalence import load_report, load_run_meta
    from .fit import load_report as load_fit, load_resolutions
    from .recorder import load_runtime_findings
    lay = _lay(project)
    rd = rd or readiness(project)
    try:
        plan = _plan(project)
    except ValueError:
        plan = None
    L: list[str] = ["# Cut-over plan", ""]
    contract_sha = sha256_file(lay.contract_file) if lay.contract_file.exists() else ""
    plan_sha = sha256_file(lay.plan_json) if lay.plan_json.exists() else ""
    L += [f"- Project: `{_md(project.root.name, 80)}`",
          f"- Contract sha256: `{contract_sha or 'missing'}`",
          f"- Migration plan: v{plan.version if plan else '-'} sha256 `{plan_sha or 'missing'}`",
          f"- target_code: `{tree_commit(project) or 'missing'}`",
          f"- Readiness: **{'READY' if rd.ready else 'NOT READY'}**", ""]
    if rd.full_suite is not None:
        fs = rd.full_suite
        L += [f"Final full-suite run: {fs.kind} `{fs.source}` ({fs.cases_equivalent}/{fs.cases_total} equivalent, "
              f"target_code `{fs.target_head[:12] or 'n/a'}`).", ""]
    if rd.blockers:
        L += ["## Blockers", ""] + [f"- {_md(b, 400)}" for b in rd.blockers] + [""]
    if rd.warnings:
        L += ["## Warnings", ""] + [f"- {_md(w, 400)}" for w in rd.warnings] + [""]

    # ---- route flips
    L += ["## 1. Route-flip order", "",
          "One step per slice, in plan order. Before step 1: provision config and secrets (section 2) and run the "
          "schema expand steps (section 3). After every flip run the full characterization suite against the "
          "composite (legacy + new behind the router) and watch the signals in section 4 before the next step.", ""]
    slices = _ordered(plan)
    table = {"routes": []}
    if plan is not None:
        try:
            table = _routes(project, plan)
        except Exception as e:  # noqa: BLE001
            L += [f"(route table unavailable: {_md(_first_line(e), 200)})", ""]
    specs = _schema_specs(project, plan) if plan is not None else []
    for i, s in enumerate(slices, 1):
        routes = [r for r in table.get("routes", []) if r.get("slice") == s.id]
        L += [f"### Step {i}: {s.id} {_md(s.name, 80)} ({s.kind})", ""]
        if s.depends_on:
            L.append(f"- Depends on: {', '.join(s.depends_on)}")
        if not routes:
            L += ["- Routes: none (no HTTP endpoint; this code ships with the entry slices that use it).",
                  "- Flip: nothing to route.",
                  "- Rollback: not routed, nothing to undo; roll back the entry slices that depend on it.", ""]
            continue
        L.append(f"- Flip: `parity harness flip PROJECT {s.id}` (the router reloads routes.json, no restart)")
        L.append("- Routes moved to the new service:")
        L += [f"  - `{_md(r['method'], 10)} {_md(r['path'], 120)}` ({r.get('endpoint', '')}; now: "
              f"{r.get('upstream', 'legacy')})" for r in routes]
        L.append(f"- Verify: full suite against the composite (`parity equivalence PROJECT --slice {s.id}` "
                 "with the router URL as --new-url); every result must stay green.")
        contracts = [c.id for c in specs if s.id in c.contract_after]
        rb = (f"- Rollback: `parity harness unflip PROJECT {s.id}` routes these paths back to legacy "
              "immediately. The database is shared and unchanged by default, so legacy keeps working on rows the new "
              "service wrote.")
        if contracts:
            rb += f" Do not run the contract steps of {', '.join(contracts)} until this step is final."
        L += [rb, ""]
    if slices:
        L += [f"### Step {len(slices) + 1}: retire legacy", "",
              "- After the last flip and an agreed observation window: run the schema contract steps (section 3), "
              "then stop the legacy service.",
              "- Rollback: before the contract steps, unflip slices in reverse order; after them, restore the "
              "pre-contract database backup first.", ""]

    # ---- config / secrets
    L += ["## 2. Config and secret checklist", "", "Names only: values never appear in this plan.", ""]
    try:
        cmap = load_config_map(project)
    except ValueError:
        cmap = None
        L += ["- config/config_map.json is invalid: re-run `parity config map PROJECT`.", ""]
    if cmap is None:
        L += ["- No config map yet (`parity config map PROJECT`).", ""]
    else:
        if cmap.secrets:
            L += ["Secrets to provision in the vault before step 1:", ""]
            L += [f"- [ ] `{_md(x.env_name, 80)}` -> `{_md(x.target_key, 120)}` ({_md(x.vault, 40)}, reference "
                  f"`{_md(x.reference, 80)}`)" for x in sorted(cmap.secrets, key=lambda x: x.env_name)]
            L.append("")
        rows = [e for e in cmap.entries if e.status in ("mapped", "renamed") and not e.is_secret]
        if rows:
            L += ["Settings to set in the deploy configuration:", ""]
            L += [f"- [ ] `{_md(e.target_key, 120)}`" + (f" (env `{_md(e.env_name, 80)}`)" if e.env_name else "")
                  + f" from `{_md(e.id, 120)}`" + (" (renamed)" if e.status == "renamed" else "")
                  for e in sorted(rows, key=lambda e: e.id)]
            L.append("")
        dropped = [e for e in cmap.entries if e.status == "dropped"]
        if dropped:
            L += ["Dropped (confirm nothing in production still needs them):", ""]
            L += [f"- [ ] `{_md(e.id, 120)}`: {_md(e.reason, 200)}" for e in sorted(dropped, key=lambda e: e.id)]
            L.append("")
        opened = [e for e in cmap.entries if e.status == "open"]
        if opened:
            L += ["Open (blocks cut-over):", ""] + [f"- `{_md(e.id, 120)}`: {_md(e.reason, 200)}" for e in opened]
            L.append("")

    # ---- schema
    L += ["## 3. Schema steps", ""]
    if not specs:
        L += ["No declared schema change (decision P2-Q10): the new service runs on the legacy schema as-is.", ""]
    for c in specs:
        L += [f"### {_md(c.id, 40)} {c.kind} `{_md(c.table, 60)}`", "", f"- {_md(c.cutover_step, 400)}"]
        for title, sql in (("Expand (before step 1)", c.expand), ("Backfill (after expand, before the first "
                                                                    "writing slice)", c.backfill),
                           ("Contract (after " + (", ".join(c.contract_after) or "every slice") + " is final)",
                            c.contract)):
            if sql:
                L += [f"- {title}:", "", "```sql", *sql, "```"]
        L.append("")

    # ---- monitoring
    L += ["## 4. Monitoring", ""]
    perf = None
    try:
        from .target import load_target
        perf = load_target(project).equivalence.perf_regression_max_pct
    except Exception:  # noqa: BLE001
        perf = None
    L += ["Per step, compare the new upstream with the legacy baseline of the same routes:", "",
          "- 5xx rate: any increase over legacy is a rollback trigger.",
          "- 4xx rate: changes only where an approved intended divergence says so (section 5).",
          f"- p95 latency: at most {perf}% above legacy (target.yaml)." if perf is not None else
          "- p95 latency: no limit set in target.yaml (equivalence.perf_regression_max_pct); agree one.",
          "- Router upstream errors and connection resets (router logs).", ""]
    if table.get("routes"):
        L += ["| Step | Slice | Method | Path | Endpoint |", "|---|---|---|---|---|"]
        step = {s.id: i for i, s in enumerate(slices, 1)}
        for r in sorted(table["routes"], key=lambda r: (step.get(r.get("slice"), 0), r.get("path", ""))):
            L.append(f"| {step.get(r.get('slice'), '-')} | {r.get('slice')} | {_md(r['method'], 10)} | "
                     f"{_md(r['path'], 120)} | {r.get('endpoint', '')} |")
        L.append("")

    # ---- risks
    L += ["## 5. Open risks", ""]
    risks: list[str] = []
    try:
        fit = load_fit(project)
    except ValueError:
        fit = None
    if fit is not None:
        res = load_resolutions(project).current()
        for r in fit.risks:
            if r.id not in res:
                risks.append(f"{r.id} ({r.severity}, {r.kind}): {_md(r.title, 160)}: unresolved")
            elif res[r.id].status in ("accepted", "wont_fix"):
                risks.append(f"{r.id} ({r.severity}): {_md(r.title, 160)}: {res[r.id].status} "
                             f"({_md(res[r.id].note, 120)})")
    for s in slices:
        for ref in sorted(load_decisions(project, s.id).accepted()):
            risks.append(f"{s.id}: deferred finding {ref} migrated as observed (accepted)")
        rep = load_report(project, s.id)
        if rep is not None:
            for d in rep.diffs:
                if d.classification == "intended_divergence":
                    risks.append(f"{s.id}: intended divergence {d.id} on {d.case_id} {_md(d.field, 80)} "
                                 f"(decision {_md(d.decision_ref, 40) or '-'}): clients see the fixed behaviour")
            for k, v in sorted(rep.untestable.items()):
                risks.append(f"{s.id}: {k} untestable ({_md(v, 120)})")
        meta = load_run_meta(project, s.id)
        if meta is not None:
            chans = sorted({c for o in meta.cases for c in o.not_compared})
            if chans:
                risks.append(f"{s.id}: not compared by the harness: {', '.join(chans)}")
    try:
        for f in load_runtime_findings(project).findings:
            if f.status == "open":
                risks.append(f"runtime finding {f.id} ({f.kind}): {_md(f.title, 160)}: open")
    except ValueError:
        risks.append("migration/runtime_findings.json is invalid")
    L += [f"- {x}" for x in risks] if risks else ["- None recorded."]
    return "\n".join(L) + "\n"


def write_plan(project, rd: Readiness | None = None) -> Path:
    lay = _lay(project)
    lay.ensure(lay.cutover_dir)
    return atomic_write_text(lay.cutover_plan, render_plan(project, rd))


# ------------------------------------------------------------------ approval
def equivalence_files(project) -> list[Path]:
    """Every equivalence artifact the cut-over approval covers, in a stable order."""
    lay = _lay(project)
    out = []
    if lay.slices_dir.is_dir():
        for d in sorted(p for p in lay.slices_dir.iterdir() if p.is_dir()):
            out += [f for f in (d / "equivalence.json", d / "equivalence_run.json") if f.is_file()]
    out += [f for f in (composite_report_path(project), composite_run_path(project), lay.equivalence_report_md)
            if f.is_file()]
    return out


def equivalence_hashes(project) -> dict[str, str]:
    lay = _lay(project)
    return {lay.rel(f): sha256_file(f) for f in equivalence_files(project)}


def combined_digest(hashes: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(hashes):
        h.update(f"{rel}\0{hashes[rel]}\n".encode())
    return h.hexdigest()


def load_approval(project) -> CutoverApproval | None:
    return read_json(_lay(project).cutover_approval, CutoverApproval, None)


def approve(project, by: str, note: str = "") -> CutoverApproval:
    """Approve the cut-over (CutoverError with `.blockers` while not ready)."""
    by = _redact(by, 64)
    if not by:
        raise CutoverError("--by is required")
    lay = _lay(project)
    lay.ensure(lay.cutover_dir)
    with file_lock(lay.cutover_approval):
        rd = readiness(project)
        write_plan(project, rd)
        if not rd.ready:
            _audit(project, "cutover_approve_refused", by=by, blockers=rd.blockers[:20])
            raise CutoverError("cut-over cannot be approved:\n  - " + "\n  - ".join(rd.blockers), rd.blockers)
        hashes = equivalence_hashes(project)
        plan = _plan(project)
        appr = CutoverApproval(approved_by=by, approved_at=now_iso(), contract_sha256=sha256_file(lay.contract_file),
                               plan_sha256=sha256_file(lay.cutover_plan), tree_commit=tree_commit(project),
                               equivalence_report_sha256=combined_digest(hashes),
                               slice_ids=[s.id for s in _ordered(plan)], note=_redact(note, 2000))
        atomic_write_json(lay.cutover_dir / HASHES_FILE,
                          {"schema_version": 1, "migration_plan": sha256_file(lay.plan_json),
                           "equivalence": hashes, "equivalence_digest": appr.equivalence_report_sha256})
        atomic_write_json(lay.cutover_approval, appr)
    _audit(project, "cutover_approve", by=by, tree_commit=appr.tree_commit, slices=len(appr.slice_ids))
    return appr


def verify_approval(project) -> dict:
    """{approved, valid, mismatches[]}: recompute the hashes of an existing approval."""
    lay = _lay(project)
    try:
        appr = load_approval(project)
    except ValueError:
        return {"approved": False, "valid": False, "mismatches": ["cutover/approval.json is invalid"]}
    if appr is None:
        return {"approved": False, "valid": False, "mismatches": []}
    mm = []
    cur = {"contract": sha256_file(lay.contract_file) if lay.contract_file.exists() else "",
           "plan": sha256_file(lay.cutover_plan) if lay.cutover_plan.exists() else "",
           "tree": tree_commit(project), "equivalence": combined_digest(equivalence_hashes(project))}
    for k, want in (("contract", appr.contract_sha256), ("plan", appr.plan_sha256), ("tree", appr.tree_commit),
                    ("equivalence", appr.equivalence_report_sha256)):
        if cur[k] != want:
            mm.append(f"{k} changed since approval")
    return {"approved": True, "valid": not mm, "mismatches": mm, "approved_by": appr.approved_by,
            "approved_at": appr.approved_at}


# ------------------------------------------------------------------ CLI
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def register(groups) -> None:
    @groups.main.command("cutover")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--approve", "do_approve", is_flag=True, help="Approve the cut-over (refused while not ready).")
    @click.option("--by", default="", help="Approver name (required with --approve).")
    @click.option("--note", default="", help="Approval note.")
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def cutover_cmd(project: Path, do_approve: bool, by: str, note: str, fmt: str) -> None:
        """Cut-over readiness and plan (cutover/plan.md); --approve writes cutover/approval.json.
        Exit 1 while not ready."""
        p = _project(project)
        lay = _lay(p)
        if do_approve:
            if not by.strip():
                raise click.ClickException("--approve needs --by NAME")
            try:
                appr = approve(p, by, note)
            except CutoverError as e:
                raise click.ClickException(str(e))
            if fmt == "json":
                click.echo(appr.model_dump_json(indent=2))
            else:
                click.echo(f"cut-over approved by {appr.approved_by} at {appr.approved_at} "
                           f"(target_code {appr.tree_commit[:20]})\nplan: {lay.rel(lay.cutover_plan)}\n"
                           f"approval: {lay.rel(lay.cutover_approval)}")
            return
        rd = readiness(p)
        write_plan(p, rd)
        _audit(p, "cutover_plan", ready=rd.ready, blockers=len(rd.blockers))
        if fmt == "json":
            click.echo(json.dumps({**rd.model_dump(mode="json"), "blockers": rd.blockers, "warnings": rd.warnings,
                                   "plan": lay.rel(lay.cutover_plan), "approval": verify_approval(p)}, indent=2))
        else:
            click.echo(f"cut-over: {'READY' if rd.ready else 'NOT READY'}")
            for c in rd.checks:
                click.echo(f"  {'ok  ' if c.ok else 'FAIL'} {c.name}" + (f": {c.detail}" if c.detail else ""))
                for b in c.blockers:
                    click.echo(f"       - {b}")
                for w in c.warnings:
                    click.echo(f"       warning: {w}")
            va = verify_approval(p)
            if va["approved"]:
                click.echo(f"approval: {'valid' if va['valid'] else 'STALE (' + '; '.join(va['mismatches']) + ')'}")
            click.echo(f"plan: {lay.rel(lay.cutover_plan)}")
        if not rd.ready:
            raise SystemExit(1)
