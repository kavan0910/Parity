"""Equivalence verification (docs/phase2-plan.md section 7): run the characterization cases of a slice, plus
every case of earlier APPROVED slices (regression), against the legacy and the new service and classify every
difference.

    parity equivalence PROJECT (--slice SLICE-NNNN | --all) --legacy-url URL --new-url URL
                                [--triage] [--offline] [--max-usd N]
    parity equivalence approve-mask PROJECT EQ-NNNN --by NAME [--reason TEXT]

    parity equivalence composite PROJECT [--base-url URL]

Without --legacy-url/--new-url: a host-reachable URL reported by `migrate.harness.status(project, role)`
(`url` / `base_url`, top-level or under `up`) is used when present; otherwise, when that harness role is up
(`harness up --legacy` / `--new`), the cases run IN-NETWORK in the sandbox (`harness.run_cases_in_network`, one
runner job per role on the role's internal network). Each side is resolved independently.

Composite (strangler mode, `run_composite`): every case of the approved (and flipped) slices runs against the
composite (router in front of legacy + new) and is compared with what that route should answer: flipped slices
-> the case expectation (recorded legacy output, or the approved specification for `fix` cases); not flipped ->
the recorded legacy output (`legacy_observed` for `fix` cases). Run after every `harness flip|unflip` (the CLI
does it automatically when the composite is up). Outputs equivalence/composite.json (CompositeReport, latest)
and one summary line per run in equivalence/composite_history.jsonl; report.md gets a Composite section.

Per case both services are observed with harness_runner.observe (same origin jail and limits as the recorder).
The legacy observation is the oracle for `preserve` cases; diffs: status, allowlisted headers, JSON-semantic
body; masks = case.normalize + human-approved masks (equivalence/masks.json). DB state, stub calls and published
messages are compared when the observations carry them (attributes `db_after`, `stub_calls`, `messages`);
otherwise they are listed as `not_compared` per case (equivalence_run.json, report.md).

Classification of each differing field:
  noise                diff disappears under the masks
  intended_divergence  `fix` case with an APPROVED specified expectation that the new output meets; cites the
                       decision (case.decision_ref)
  regression           legacy still reproduces its recorded expectation (or answered while new failed) and new
                       differs; or new misses an approved specified expectation
  unknown              everything else (unrecorded case, legacy no longer matches its recording, legacy error,
                       unapproved specified expectation)

Optional `equiv_triage` model task (cheap tier) proposes legacy_bug | new_bug | harness_issue | spec_ambiguity for
unknown diffs, quoting both outputs; quotes are verified verbatim against the exact (redacted) text the model saw,
otherwise the proposal is dropped. The model never sees generator reasoning.

Outputs:
  slices/SLICE-NNNN/equivalence.json     EquivalenceReport (EQ ids keyed `eq:<case_id>/<field>`)
  slices/SLICE-NNNN/equivalence_run.json EquivalenceRunMeta: URLs, target_code HEAD at run time, per-case outcome
  slices/SLICE-NNNN/fix_requests.json    FixRequestsFile, read by the slice runner. Schema:
      {"schema_version": 1, "slice_id": "SLICE-0001", "requests": [
        {"eq_id": "EQ-0003", "case_id": "CT-0002", "field": "body.$[0].name",
         "reason": "regression|new_bug|legacy_bug_preserve", "legacy": <json>, "new": <json>, "note": "...",
         "status": "pending|done|resolved", "created": "<iso>", "updated": "<iso>"}]}
      pending: the slice runner should run a fix iteration and set status "done"; a later equivalence run marks
      pending requests whose diff disappeared as "resolved". Approval is refused while any request is pending.
  equivalence/masks.json                 MasksFile: mask proposals (harness_issue triage) and human approvals
  equivalence/report.md                  per-slice equivalence %, regressions, intended divergences with decision
                                         ids, untestable items, cases not compared
Disagreements: new_bug/regression -> fix request; legacy_bug -> open decision (slices/<id>/decisions.json);
harness_issue -> mask proposal needing `equivalence approve-mask`; spec_ambiguity -> open question and the slice
is moved to `blocked`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from .models import CharacterizationCase, EquivalenceDiff, EquivalenceReport, Triage, is_id
from .paths import Phase2Layout
from .state import atomic_write_json, atomic_write_text, file_lock, now_iso, read_json

TASK = "equiv_triage"
PROMPT_VERSION = "equiv_triage.v1"
PROMPT_FILE = Path(__file__).parent / "prompts" / "equiv_triage.md"
TRIAGES: tuple[str, ...] = ("legacy_bug", "new_bug", "harness_issue", "spec_ambiguity")
EXTRA_CHANNELS: tuple[str, ...] = ("db_after", "stub_calls", "messages")
MAX_OUTPUT_CHARS = 6000
MAX_QUOTE = 300


class EquivalenceError(RuntimeError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ local models
class CaseOutcome(_Strict):
    case_id: str
    slice: str | None = None                 # slice owning the case
    role: Literal["slice", "regression"] = "slice"
    legacy_outcome: str = ""
    new_outcome: str = ""
    legacy_error: str = ""
    new_error: str = ""
    compared: bool = True                    # False when neither side could run the case (e.g. grpc)
    equivalent: bool = False
    not_compared: list[str] = Field(default_factory=list)   # db_after / stub_calls / messages not observed
    diff_ids: list[str] = Field(default_factory=list)
    masks: list[str] = Field(default_factory=list)
    legacy_s: float | None = None            # response time on each side (advisory latency comparison)
    new_s: float | None = None


class TriageStats(_Strict):
    requested: bool = False
    calls: int = 0
    accepted: int = 0
    dropped_unverified: int = 0
    invalid_outputs: int = 0
    budget_stopped: bool = False
    skipped: str = ""


class EquivalenceRunMeta(_Strict):
    schema_version: Literal[1] = 1
    slice: str
    created: str = ""
    legacy_url: str = ""
    new_url: str = ""
    legacy_mode: Literal["url", "harness"] = "url"    # harness = in-network runner job on the role's network
    new_mode: Literal["url", "harness"] = "url"
    target_head: str = ""                   # target_code HEAD when the run started ("" without git)
    regression_slices: list[str] = Field(default_factory=list)
    cases: list[CaseOutcome] = Field(default_factory=list)
    triage: TriageStats = Field(default_factory=TriageStats)
    latency: dict | None = None             # differential.LatencySummary as a dict (advisory, never a gate)


class CompositeCase(_Strict):
    case_id: str
    slice: str | None = None
    upstream: Literal["new", "legacy"] = "legacy"     # where the router sends the case (flipped slice -> new)
    expected_from: Literal["expect", "legacy_observed", "none"] = "expect"
    outcome: Literal["pass", "fail", "error", "not_implemented", "not_compared"] = "pass"
    error: str = ""
    diffs: list[dict] = Field(default_factory=list)


class CompositeReport(_Strict):
    schema_version: Literal[1] = 1
    created: str = ""
    base_url: str = ""
    mode: Literal["url", "harness"] = "harness"
    target_head: str = ""
    flipped: list[str] = Field(default_factory=list)
    slices: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    cases: list[CompositeCase] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.totals.get("fail") or self.totals.get("error"))


class FixRequest(_Strict):
    eq_id: str
    case_id: str = ""
    field: str = ""
    reason: Literal["regression", "new_bug", "legacy_bug_preserve"]
    legacy: Any = None
    new: Any = None
    note: str = ""
    status: Literal["pending", "done", "resolved"] = "pending"
    created: str = ""
    updated: str = ""


class FixRequestsFile(_Strict):
    schema_version: Literal[1] = 1
    slice_id: str = ""
    requests: list[FixRequest] = Field(default_factory=list)


class MaskRequest(_Strict):
    eq_id: str
    case_id: str
    mask: str
    reason: str = ""
    status: Literal["proposed", "approved"] = "proposed"
    proposed_by: str = "triage"
    approved_by: str = ""
    approved_at: str = ""
    created: str = ""


class MasksFile(_Strict):
    schema_version: Literal[1] = 1
    masks: list[MaskRequest] = Field(default_factory=list)

    def approved_for(self, case_id: str) -> list[str]:
        return sorted({m.mask for m in self.masks if m.case_id == case_id and m.status == "approved"})


# ------------------------------------------------------------------ files
def _lay(project) -> Phase2Layout:
    return Phase2Layout(project)


def _sid(slice_id: str) -> str:
    if not is_id(slice_id or "", "SLICE"):
        raise EquivalenceError(f"not a valid SLICE id: {str(slice_id)[:40]!r}")
    return slice_id


def run_meta_path(project, slice_id: str) -> Path:
    return _lay(project).slice_dir(_sid(slice_id)) / "equivalence_run.json"


def fix_requests_path(project, slice_id: str) -> Path:
    return _lay(project).slice_dir(_sid(slice_id)) / "fix_requests.json"


def masks_path(project) -> Path:
    return _lay(project).equivalence_dir / "masks.json"


def load_report(project, slice_id: str) -> EquivalenceReport | None:
    return read_json(_lay(project).slice_equivalence(_sid(slice_id)), EquivalenceReport, None)


def load_run_meta(project, slice_id: str) -> EquivalenceRunMeta | None:
    return read_json(run_meta_path(project, slice_id), EquivalenceRunMeta, None)


def load_fix_requests(project, slice_id: str) -> FixRequestsFile:
    f = read_json(fix_requests_path(project, slice_id), FixRequestsFile, None)
    return f if f is not None else FixRequestsFile(slice_id=slice_id)


def load_masks(project) -> MasksFile:
    return read_json(masks_path(project), MasksFile, MasksFile())


def add_fix_request(project, slice_id: str, *, eq_id: str, case_id: str = "", field: str = "",
                    reason: str = "regression", legacy: Any = None, new: Any = None, note: str = "") -> FixRequest:
    """Add (or refresh) a pending fix request for the slice runner."""
    path = fix_requests_path(project, slice_id)
    _lay(project).ensure(path.parent)
    with file_lock(path):
        f = load_fix_requests(project, slice_id)
        cur = next((r for r in f.requests if r.eq_id == eq_id and r.status == "pending"), None)
        if cur is None:
            cur = FixRequest(eq_id=eq_id, case_id=case_id, field=field, reason=reason, legacy=legacy, new=new,
                             note=note[:500], created=now_iso(), updated=now_iso())
            f.requests.append(cur)
        else:
            cur.updated = now_iso()
        atomic_write_json(path, f)
    return cur


def pending_fix_requests(project, slice_id: str) -> list[FixRequest]:
    return [r for r in load_fix_requests(project, slice_id).requests if r.status == "pending"]


def mark_fix_requests_done(project, slice_id: str, eq_ids, note: str = "") -> list[str]:
    """pending -> done for the given EQ ids (the slice runner, after the iteration that addressed them).
    A later equivalence run re-opens a request (new pending entry) when the diff is still there."""
    want = set(eq_ids or ())
    if not want:
        return []
    path = fix_requests_path(project, slice_id)
    if not path.exists():
        return []
    done = []
    with file_lock(path):
        f = load_fix_requests(project, slice_id)
        for r in f.requests:
            if r.status == "pending" and r.eq_id in want:
                r.status, r.updated = "done", now_iso()
                if note:
                    r.note = (r.note + ("; " if r.note else "") + note)[:500]
                done.append(r.eq_id)
        if done:
            atomic_write_json(path, f)
    if done:
        _audit(project, "fix_requests_done", slice=slice_id, eq_ids=done, note=note[:200])
    return done


def _sync_fix_requests(project, slice_id: str, wanted: list[FixRequest], live_eq: set[str]) -> FixRequestsFile:
    """Pending requests whose diff disappeared -> resolved; add the new ones (no duplicates while pending)."""
    path = fix_requests_path(project, slice_id)
    _lay(project).ensure(path.parent)
    with file_lock(path):
        f = load_fix_requests(project, slice_id)
        for r in f.requests:
            if r.status == "pending" and r.eq_id not in live_eq:
                r.status, r.updated = "resolved", now_iso()
        pending = {r.eq_id for r in f.requests if r.status == "pending"}
        for w in wanted:
            if w.eq_id not in pending:
                f.requests.append(w)
                pending.add(w.eq_id)
        atomic_write_json(path, f)
    return f


def field_mask(field: str) -> str | None:
    """The mask that would hide a diff field, or None (status and extra channels are never masked)."""
    from .harness_runner import MaskError, validate_mask
    if field == "body":
        cand = "$"
    elif field.startswith("body."):
        cand = field[5:]
    elif field.startswith("headers."):
        cand = field
    else:
        return None
    try:
        return validate_mask(cand)
    except MaskError:
        return None


def propose_mask(project, eq_id: str, case_id: str, mask: str, reason: str) -> MaskRequest:
    path = masks_path(project)
    _lay(project).ensure(path.parent)
    with file_lock(path):
        mf = load_masks(project)
        cur = next((m for m in mf.masks if m.case_id == case_id and m.mask == mask), None)
        if cur is None:
            cur = MaskRequest(eq_id=eq_id, case_id=case_id, mask=mask, reason=reason[:500], created=now_iso())
            mf.masks.append(cur)
            atomic_write_json(path, mf)
    return cur


def _find_diff(project, eq_id: str) -> EquivalenceDiff | None:
    d = _lay(project).slices_dir
    if not d.is_dir():
        return None
    for sd in sorted(d.iterdir()):
        if is_id(sd.name, "SLICE") and (sd / "equivalence.json").is_file():
            rep = load_report(project, sd.name)
            for x in rep.diffs if rep else []:
                if x.id == eq_id:
                    return x
    return None


def approve_mask(project, eq_id: str, by: str, reason: str = "") -> MaskRequest:
    """Human approval of a mask for the field behind EQ-NNNN (a proposal, or any differing maskable field).
    Approved masks apply from the next equivalence run on."""
    from ..ingest.secrets import redact_string
    by = redact_string((by or "").strip())[:64]
    if not by:
        raise EquivalenceError("--by is required")
    if not is_id(eq_id or "", "EQ"):
        raise EquivalenceError(f"not a valid EQ id: {str(eq_id)[:40]!r}")
    path = masks_path(project)
    _lay(project).ensure(path.parent)
    with file_lock(path):
        mf = load_masks(project)
        cur = next((m for m in mf.masks if m.eq_id == eq_id), None)
        if cur is None:
            diff = _find_diff(project, eq_id)
            if diff is None:
                raise EquivalenceError(f"{eq_id} is not a diff in any equivalence report")
            mask = field_mask(diff.field)
            if mask is None:
                raise EquivalenceError(f"{eq_id} ({diff.field}) cannot be masked")
            cur = MaskRequest(eq_id=eq_id, case_id=diff.case_id, mask=mask, proposed_by=f"human:{by}",
                              created=now_iso())
            mf.masks.append(cur)
        cur.status, cur.approved_by, cur.approved_at = "approved", by, now_iso()
        if reason.strip():
            cur.reason = redact_string(reason.strip())[:500]
        atomic_write_json(path, mf)
    _audit(project, "approve_mask", eq_id=eq_id, case_id=cur.case_id, mask=cur.mask, by=by)
    return cur


def _audit(project, action: str, **fields) -> None:
    from ..audit import AuditLog
    try:
        AuditLog(Path(project.root) / "audit" / "audit.jsonl").record("equivalence", action, **fields)
    except OSError:
        pass


# ------------------------------------------------------------------ case selection
def _plan(project):
    from .casestore import load_plan
    plan = load_plan(project)
    if plan is None:
        raise EquivalenceError("no plan/migration_plan.json: run `parity plan` first")
    return plan


def regression_slices(project, slice_id: str) -> list[str]:
    """Earlier (plan order) slices whose state is approved."""
    from .state import load_state
    plan = _plan(project)
    me = next((s for s in plan.slices if s.id == slice_id), None)
    if me is None:
        raise EquivalenceError(f"{slice_id} is not in the plan")
    return [s.id for s in sorted(plan.slices, key=lambda s: (s.order, s.id))
            if s.order < me.order and s.id != slice_id and load_state(project, s.id).status == "approved"]


# ------------------------------------------------------------------ comparison
def _obs_dict(obs) -> dict:
    return {"status": obs.status, "headers": dict(obs.headers), "body": obs.body}


def _secs(res) -> float | None:
    obs = getattr(res, "observed", None)
    v = getattr(obs, "duration_s", None) if obs is not None else None
    return float(v) if isinstance(v, (int, float)) and v >= 0 else None


def _extra(obs, name: str):
    return getattr(obs, name, None) if obs is not None else None


def classify_case(case: CharacterizationCase, legacy, new, allowlist, extra_masks: list[str]) -> tuple[list[dict], dict]:
    """Pure classification of one case. `legacy`/`new` are harness_runner CaseResults.
    Returns ([{field, legacy, new, classification, decision_ref, note}], {compared, not_compared})."""
    from .harness_runner import compare, is_recorded, json_equal
    masks = sorted(set(case.normalize) | set(extra_masks))
    info = {"compared": True, "not_compared": []}
    lo, no = legacy.observed, new.observed
    if lo is None and no is None:
        if legacy.outcome == "not_implemented" and new.outcome == "not_implemented":
            info["compared"] = False
            return [], info
    if lo is None or no is None:
        if lo is not None:       # legacy answered, new failed
            return [dict(field="outcome", legacy=legacy.outcome, new=f"{new.outcome}: {new.error}"[:300],
                         classification="regression", note="new service did not answer the case")], info
        return [dict(field="outcome", legacy=f"{legacy.outcome}: {legacy.error}"[:300],
                     new=new.outcome if no is None else "answered", classification="unknown",
                     note="legacy service did not answer the case")], info

    out: list[dict] = []
    legacy_d = _obs_dict(lo)
    raw = {d.field: d for d in compare(legacy_d, no, [], allowlist)}
    masked = {d.field: d for d in compare(legacy_d, no, masks, allowlist)}
    for f, d in raw.items():
        if f not in masked:
            out.append(dict(field=f, legacy=d.expected, new=d.actual, classification="noise",
                            note="hidden by masks"))
    specified = case.decision == "fix" and case.expect.source == "specified"
    stable_ok = False
    if specified:
        approved = bool(case.expect.approved_by)
        spec_diffs = compare(case.expect, no, masks, allowlist)
        ref = case.decision_ref or "fix"
        if approved and not spec_diffs:
            for f, d in masked.items():
                out.append(dict(field=f, legacy=d.expected, new=d.actual, classification="intended_divergence",
                                decision_ref=ref, note=f"fix decision {ref}; new meets the approved specification"))
        elif approved:
            for d in spec_diffs:
                out.append(dict(field=d.field, legacy=d.expected, new=d.actual, classification="regression",
                                decision_ref=ref, note="new output does not meet the approved specified expectation"
                                                       " (legacy column shows the specification)"))
        else:
            for f, d in masked.items():
                out.append(dict(field=f, legacy=d.expected, new=d.actual, classification="unknown",
                                decision_ref=ref, note="specified expectation not approved (`tests approve-spec`)"))
    else:
        stable = is_recorded(case.expect) and not compare(case.expect, lo, masks, allowlist)
        why = ("" if stable else "case not recorded" if not is_recorded(case.expect)
               else "legacy no longer reproduces its recorded expectation")
        for f, d in masked.items():
            out.append(dict(field=f, legacy=d.expected, new=d.actual,
                            classification="regression" if stable else "unknown", note=why))
        stable_ok = stable
    for ch in EXTRA_CHANNELS:
        a, b = _extra(lo, ch), _extra(no, ch)
        if a is None or b is None:
            info["not_compared"].append(ch)
        elif not json_equal(a, b):
            cls = "regression" if (not specified and stable_ok) else "unknown"
            out.append(dict(field=ch, legacy=a, new=b, classification=cls, note=""))
    return out, info


# ------------------------------------------------------------------ triage
def load_prompt() -> tuple[str, str, str]:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    m = re.match(r"<!-- prompt: (\S+)", text)
    _, _, rest = text.partition("<!-- SYSTEM -->")
    system, _, user = rest.partition("<!-- USER -->")
    return (m.group(1) if m else ""), system.strip() + "\n", user.strip() + "\n"


def _norm(s: str) -> str:
    return " ".join(str(s).split())


def render_output(res) -> str:
    """The exact (redacted, capped) text of one side shown to the model and used for quote verification."""
    from ..ingest.secrets import redact_string
    if res.observed is None:
        txt = f"outcome: {res.outcome}\nerror: {res.error}"
    else:
        o = res.observed
        txt = (f"status: {o.status}\nheaders: {json.dumps(o.headers, sort_keys=True, ensure_ascii=False)}\n"
               f"body: {json.dumps(o.body, sort_keys=True, ensure_ascii=False)}")
    return redact_string(txt)[:MAX_OUTPUT_CHARS]


def _short(v: Any, n: int = 200) -> str:
    s = json.dumps(v, sort_keys=True, ensure_ascii=False) if not isinstance(v, str) else v
    return s.replace("\n", " ")[:n]


def _rules_text(project, case: CharacterizationCase) -> str:
    try:
        from .casestore import contract_graph
        from .contract import load_contract
        g = contract_graph(project, load_contract(project))
    except Exception:  # noqa: BLE001 - rule text is optional context
        return "(none)"
    by_id = {n.id: n for n in g.nodes}
    lines = []
    for i in case.covers:
        n = by_id.get(i)
        if n is not None:
            lines.append(f"{n.id} | {n.type} | {n.name} | {(n.responsibility or '')[:400]}")
    return "\n".join(lines) or "(none)"


def triage_case(project, llm, case: CharacterizationCase, legacy, new, diffs: list[EquivalenceDiff],
                stats: TriageStats) -> None:
    """One `equiv_triage` call for the unknown diffs of a case; fills triage fields in place when verified."""
    from ..analyze.module_analyzer import render
    from ..ingest.secrets import redact_string
    from ..llm.base import BudgetExceeded, LLMAuthError, LLMError, parse_json
    version, system, user = load_prompt()
    if version != PROMPT_VERSION:
        raise EquivalenceError(f"prompt version {version!r} != {PROMPT_VERSION!r}")
    lt, nt = render_output(legacy), render_output(new)
    case_view = {"id": case.id, "kind": case.kind, "covers": case.covers, "decision": case.decision,
                 "request": case.request.model_dump(mode="json"), "tags": case.tags}
    prompt = render(user, CASE_ID=case.id, CASE=redact_string(json.dumps(case_view, sort_keys=True))[:4000],
                    RULES=_rules_text(project, case), LEGACY=lt, NEW=nt,
                    DIFFS="\n".join(f"{d.id} | {d.field} | {_short(d.legacy)} | {_short(d.new)}" for d in diffs))
    if stats.budget_stopped:
        return
    try:
        resp = llm.complete(task=TASK, system=system, prompt=prompt, json_mode=True, prompt_version=PROMPT_VERSION)
    except BudgetExceeded:
        stats.budget_stopped = True
        return
    except LLMAuthError:
        raise
    except LLMError:
        stats.invalid_outputs += 1
        return
    stats.calls += 1
    try:
        data = parse_json(resp.text)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("triage"), list):
        stats.invalid_outputs += 1
        return
    by_id = {d.id: d for d in diffs}
    nl, nn = _norm(lt), _norm(nt)
    for item in data["triage"]:
        if not isinstance(item, dict):
            stats.invalid_outputs += 1
            continue
        d = by_id.get(item.get("id"))
        cat = item.get("category")
        ql, qn = _norm(item.get("quote_legacy") or ""), _norm(item.get("quote_new") or "")
        if d is None or cat not in TRIAGES:
            stats.invalid_outputs += 1
            continue
        if not ql or not qn or ql not in nl or qn not in nn or len(ql) > MAX_QUOTE or len(qn) > MAX_QUOTE:
            stats.dropped_unverified += 1
            continue
        d.triage = cat
        d.triage_quote = f"legacy: {ql} | new: {qn}"[:2 * MAX_QUOTE + 20]
        expl = redact_string(_norm(item.get("explanation") or ""))[:400]
        d.note = (d.note + ("; " if d.note else "") + f"triage: {expl}")[:600]
        stats.accepted += 1


# ------------------------------------------------------------------ orchestration
def resolve_urls(project, legacy_url: str | None, new_url: str | None) -> tuple[str | None, str | None]:
    """Explicit URLs win; then a host-reachable URL from migrate.harness.status (lazy import, any failure
    tolerated); else None = run that side in-network against the running harness role (see module docstring).
    Raises EquivalenceError when a side has neither."""
    def from_harness(role: str) -> str | None:
        try:
            from . import harness as h
            st = h.status(project, role)
        except Exception:  # noqa: BLE001 - harness optional / no sandbox
            return None
        for src in (st, st.get("up") or {}):
            if isinstance(src, dict):
                for k in ("url", "base_url"):
                    if isinstance(src.get(k), str) and src[k]:
                        return src[k]
        return None
    legacy_url = legacy_url or from_harness("legacy")
    new_url = new_url or from_harness("new")
    if legacy_url and new_url:
        return legacy_url, new_url
    up = _harness_up(project)
    missing = [r for r, u in (("legacy", legacy_url), ("new", new_url)) if not u and not up.get(r)]
    if missing:
        raise EquivalenceError("legacy and new base URLs are required (--legacy-url/--new-url, or a running harness: "
                               "`harness up PROJECT --legacy` / `--new`); missing: " + ", ".join(missing))
    return legacy_url, new_url


def _harness_up(project) -> dict:
    try:
        from . import harness as h
        return h.service_urls(project)
    except Exception:  # noqa: BLE001 - no harness state
        return {}


def observe_side(project, role: str, url: str | None, cases: list, *, timeout_s: float, header_allowlist: list[str],
                 job_endpoint: str | None, backend=None) -> list:
    """Observations (harness_runner CaseResults, outcome `unrecorded`/error) of `cases` on one side: in-process
    against `url`, or in-network against the running harness `role` when `url` is None."""
    from .harness_runner import RunnerConfig, observe
    if url:
        cfg = RunnerConfig(base_url=url, timeout_s=timeout_s, header_allowlist=header_allowlist,
                           job_endpoint=job_endpoint)
        return [observe(c, cfg) for c in cases]
    from . import harness as h
    try:
        return h.run_cases_in_network(project, role, cases, backend=backend, compare=False, timeout_s=timeout_s,
                                      job_endpoint=job_endpoint or "", header_allowlist=header_allowlist)
    except h.HarnessError as e:
        raise EquivalenceError(f"{role} (in-network): {e}") from None


def reset_harness_sides(project, legacy_url: str | None, new_url: str | None, backend=None) -> list[str]:
    """Restart each side that runs in the harness (URL None) so both start from the same seeded state."""
    from . import harness as h
    done = []
    try:
        h.load_spec(project)
    except h.HarnessError:
        return done                 # no harness spec: nothing this tool can restart (a hand-made harness)
    for role, url in (("legacy", legacy_url), ("new", new_url)):
        if url is None:
            try:
                h.restart(project, role, backend=backend)
            except h.HarnessError as e:
                raise EquivalenceError(f"cannot restart the {role} harness: {e}") from None
            done.append(role)
    return done


def run_equivalence(project, slice_id: str, legacy_url: str | None = None, new_url: str | None = None, *, llm=None,
                    timeout_s: float = 10.0, header_allowlist: list[str] | None = None,
                    job_endpoint: str | None = "/__harness/job", log=None, backend=None,
                    reset: bool = False) -> EquivalenceReport:
    """Run, classify, (optionally) triage, write all outputs; returns the EquivalenceReport.
    A side whose URL is None runs in-network against the running harness role (legacy / new); `reset=True`
    restarts those sides first (fresh seeded state; the CLI default in harness mode)."""
    from .approvals import git_head, upsert_open_decision
    from .casestore import load_cases, load_untestable
    from .harness_runner import DEFAULT_HEADER_ALLOWLIST, parse_base_url
    from .ids import allocator
    from .state import IllegalTransition, load_state, transition
    _sid(slice_id)
    plan = _plan(project)
    sl = next((s for s in plan.slices if s.id == slice_id), None)
    if sl is None:
        raise EquivalenceError(f"{slice_id} is not in the plan")
    for u in (legacy_url, new_url):
        if u is None:
            continue
        try:
            parse_base_url(u)
        except ValueError as e:
            raise EquivalenceError(f"bad base URL {u[:100]!r}: {e}") from None
    allow = list(header_allowlist or DEFAULT_HEADER_ALLOWLIST)
    common = dict(timeout_s=timeout_s, header_allowlist=allow, job_endpoint=job_endpoint, backend=backend)
    if reset:
        reset_harness_sides(project, legacy_url, new_url, backend)
    head = git_head(project)
    regress = regression_slices(project, slice_id)
    cases = load_cases(project)
    selected = [(c, "slice") for c in cases if c.slice == slice_id]
    selected += [(c, "regression") for c in cases if c.slice in regress]
    masks = load_masks(project)

    sel_cases = [c for c, _r in selected]
    l_all = observe_side(project, "legacy", legacy_url, sel_cases, **common)
    n_all = observe_side(project, "new", new_url, sel_cases, **common)
    up = _harness_up(project) if not (legacy_url and new_url) else {}
    raw: list[tuple[CharacterizationCase, str, Any, Any, list[dict], dict]] = []
    for (case, role), lres, nres in zip(selected, l_all, n_all):
        diffs, info = classify_case(case, lres, nres, allow, masks.approved_for(case.id))
        raw.append((case, role, lres, nres, diffs, info))
        if log:
            bad = sum(d["classification"] in ("regression", "unknown") for d in diffs)
            log(f"{case.id} ({role}): {'not compared' if not info['compared'] else 'OK' if not bad else f'{bad} diff(s)'}")

    keys = [f"eq:{c.id}/{d['field']}" for c, _r, _l, _n, ds, _i in raw for d in ds]
    ids = allocator(project).allocate_many("EQ", keys) if keys else {}
    report_diffs: list[EquivalenceDiff] = []
    outcomes: list[CaseOutcome] = []
    stats = TriageStats(requested=llm is not None)
    for case, role, lres, nres, ds, info in raw:
        cdiffs = []
        for d in ds:
            cdiffs.append(EquivalenceDiff(id=ids[f"eq:{case.id}/{d['field']}"], case_id=case.id, slice=case.slice,
                                          field=d["field"][:300], legacy=d.get("legacy"), new=d.get("new"),
                                          classification=d["classification"], decision_ref=d.get("decision_ref", ""),
                                          note=d.get("note", "")[:600]))
        unknown = [d for d in cdiffs if d.classification == "unknown"]
        if llm is not None and unknown:
            triage_case(project, llm, case, lres, nres, unknown, stats)
        report_diffs += cdiffs
        bad = [d for d in cdiffs if d.classification in ("regression", "unknown")]
        outcomes.append(CaseOutcome(case_id=case.id, slice=case.slice, role=role, legacy_outcome=lres.outcome,
                                    new_outcome=nres.outcome, legacy_error=lres.error[:300], new_error=nres.error[:300],
                                    compared=info["compared"], equivalent=info["compared"] and not bad,
                                    not_compared=info["not_compared"], diff_ids=[d.id for d in cdiffs],
                                    masks=sorted(set(case.normalize) | set(masks.approved_for(case.id))),
                                    legacy_s=_secs(lres), new_s=_secs(nres)))
    if llm is None:
        stats.skipped = "no model (offline or --triage not given)"

    unt = load_untestable(project).nodes
    compared = [o for o in outcomes if o.compared]
    report = EquivalenceReport(slice=slice_id, cases_total=len(compared),
                               cases_equivalent=sum(o.equivalent for o in compared), diffs=report_diffs,
                               untestable={n: unt[n].reason for n in sl.node_ids if n in unt}, created=now_iso())
    lay = _lay(project)
    lay.ensure(lay.slice_dir(slice_id))
    atomic_write_json(lay.slice_equivalence(slice_id), report)
    meta = EquivalenceRunMeta(slice=slice_id, created=report.created, legacy_url=legacy_url or up.get("legacy") or "",
                              new_url=new_url or up.get("new") or "", legacy_mode="url" if legacy_url else "harness",
                              new_mode="url" if new_url else "harness", target_head=head, regression_slices=regress,
                              cases=outcomes, triage=stats)
    from .differential import latency_summary
    lat = latency_summary([(o.case_id, o.legacy_s, o.new_s) for o in outcomes if o.compared])
    meta.latency = lat.model_dump(mode="json") if lat else None
    atomic_write_json(run_meta_path(project, slice_id), meta)

    # ---- disagreement handling (plan 7)
    wanted: list[FixRequest] = []
    ambiguous = []
    for d in report_diffs:
        if d.classification == "regression" or (d.classification == "unknown" and d.triage == "new_bug"):
            wanted.append(FixRequest(eq_id=d.id, case_id=d.case_id, field=d.field,
                                     reason="regression" if d.classification == "regression" else "new_bug",
                                     legacy=d.legacy, new=d.new, note=d.note[:500], created=now_iso(),
                                     updated=now_iso()))
        elif d.classification == "unknown" and d.triage == "legacy_bug":
            upsert_open_decision(project, slice_id, "legacy_bug", d.id,
                                 f"{d.case_id} {d.field}: legacy may be wrong ({d.triage_quote}). "
                                 "Preserve legacy behaviour or fix it?", case_id=d.case_id)
        elif d.classification == "unknown" and d.triage == "harness_issue":
            m = field_mask(d.field)
            if m:
                propose_mask(project, d.id, d.case_id, m, f"triage harness_issue: {d.triage_quote}")
        elif d.classification == "unknown" and d.triage == "spec_ambiguity":
            upsert_open_decision(project, slice_id, "spec_ambiguity", d.id,
                                 f"{d.case_id} {d.field}: which behaviour is right? ({d.triage_quote})",
                                 case_id=d.case_id)
            ambiguous.append(d.id)
    live = {d.id for d in report.unexplained}
    _sync_fix_requests(project, slice_id, wanted, live)
    if ambiguous:
        st = load_state(project, slice_id)
        if st.status not in ("blocked", "approved"):
            try:
                transition(project, slice_id, "blocked", actor="equivalence",
                           reason="open question(s) from equivalence triage: " + ", ".join(ambiguous[:5]))
            except IllegalTransition:
                pass
    write_report_md(project)
    _audit(project, "equivalence_run", slice=slice_id, cases=report.cases_total, equivalent=report.cases_equivalent,
           regressions=sum(d.classification == "regression" for d in report_diffs),
           unknown=sum(d.classification == "unknown" for d in report_diffs), triage_calls=stats.calls,
           triage_dropped=stats.dropped_unverified, target_head=head)
    return report


# ------------------------------------------------------------------ composite (strangler)
def composite_path(project) -> Path:
    return _lay(project).equivalence_dir / "composite.json"


def composite_history_path(project) -> Path:
    return _lay(project).equivalence_dir / "composite_history.jsonl"


def load_composite(project) -> CompositeReport | None:
    return read_json(composite_path(project), CompositeReport, None)


def _composite_expectation(case: CharacterizationCase, flipped: bool):
    """(expectation to compare with, source) for a case answered through the router."""
    if flipped or not (case.decision == "fix" and case.expect.source == "specified"):
        return case.expect, "expect"
    lo = case.expect.legacy_observed
    if isinstance(lo, dict) and lo.get("status") is not None:
        return {"status": lo.get("status"), "headers": lo.get("headers") or {}, "body": lo.get("body")}, \
            "legacy_observed"
    return None, "none"


def run_composite(project, *, base_url: str | None = None, backend=None, timeout_s: float = 10.0,
                  header_allowlist: list[str] | None = None, job_endpoint: str | None = "/__harness/job",
                  slice_ids: list[str] | None = None) -> CompositeReport:
    """Run every case of the approved (and flipped) slices against the composite and write
    equivalence/composite.json (see module docstring). In-network against the running composite harness unless
    `base_url` is given."""
    from .approvals import git_head
    from .casestore import load_cases
    from .harness_runner import DEFAULT_HEADER_ALLOWLIST, compare, is_recorded, parse_base_url
    from .state import load_state
    from . import harness as h
    plan = _plan(project)
    if base_url:
        try:
            parse_base_url(base_url)
        except ValueError as e:
            raise EquivalenceError(f"bad base URL {base_url[:100]!r}: {e}") from None
    elif not _harness_up(project).get("composite"):
        raise EquivalenceError("the composite harness is not up: `harness build PROJECT --composite` and "
                               "`harness up PROJECT --composite` (or pass --base-url)")
    try:
        flips = h.load_flips(project)
    except Exception:  # noqa: BLE001 - no flips file yet
        flips = {}
    flipped = sorted(flips)
    order = [s.id for s in sorted(plan.slices, key=lambda s: (s.order, s.id))]
    chosen = [sid for sid in order if load_state(project, sid).status == "approved" or sid in flips]
    if slice_ids:
        chosen = [sid for sid in chosen if sid in set(slice_ids)]
    cases = [c for c in load_cases(project) if c.slice in set(chosen)]
    allow = list(header_allowlist or DEFAULT_HEADER_ALLOWLIST)
    results = observe_side(project, "composite", base_url, cases, timeout_s=timeout_s, header_allowlist=allow,
                           job_endpoint=job_endpoint, backend=backend)
    masks = load_masks(project)
    rows: list[CompositeCase] = []
    for case, res in zip(cases, results):
        is_flipped = case.slice in flips
        exp, src = _composite_expectation(case, is_flipped)
        row = CompositeCase(case_id=case.id, slice=case.slice, upstream="new" if is_flipped else "legacy",
                            expected_from=src)
        if res.outcome == "not_implemented":
            row.outcome, row.error = "not_implemented", res.error[:300]
        elif res.observed is None:
            row.outcome, row.error = "error", f"{res.error_class or 'error'}: {res.error}"[:300]
        elif exp is None or (src == "expect" and not is_recorded(case.expect)):
            row.outcome, row.expected_from = "not_compared", "none"
            row.error = "no recorded legacy output to compare with"
        else:
            m = sorted(set(case.normalize) | set(masks.approved_for(case.id)))
            diffs = compare(exp, res.observed, m, allow)
            row.outcome = "fail" if diffs else "pass"
            row.diffs = [d.model_dump(mode="json") for d in diffs[:20]]
        rows.append(row)
    totals: dict[str, int] = {}
    for r in rows:
        totals[r.outcome] = totals.get(r.outcome, 0) + 1
    from ..ingest.secrets import redact_obj
    rep = CompositeReport(created=now_iso(), base_url=base_url or _harness_up(project).get("composite") or "",
                          mode="url" if base_url else "harness", target_head=git_head(project), flipped=flipped,
                          slices=chosen, totals=totals, cases=rows)
    lay = _lay(project)
    lay.ensure(lay.equivalence_dir)
    atomic_write_json(composite_path(project), redact_obj(rep.model_dump(mode="json")))
    line = json.dumps({"created": rep.created, "flipped": flipped, "slices": chosen, "totals": totals,
                       "ok": rep.ok, "target_head": rep.target_head}, sort_keys=True)
    with file_lock(composite_history_path(project)):
        with open(composite_history_path(project), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    write_report_md(project)
    _audit(project, "equivalence_composite", flipped=flipped, slices=chosen, totals=totals, ok=rep.ok,
           mode=rep.mode)
    return rep


# ------------------------------------------------------------------ report.md
def _md(s: Any) -> str:
    t = _short(s, 120) if not isinstance(s, str) else s[:120]
    return re.sub(r"[|`<>\r\n]", lambda m: {"|": "\\|", "\n": " ", "\r": " "}.get(m.group(0), " "), t)


def render_report_md(project) -> str:
    plan = _plan(project)
    lines = ["# Equivalence report", "", f"Generated {now_iso()}.", "",
             "| Slice | Status | Cases | Equivalent | Equivalence % | Regressions | Unknown | Intended | Noise |",
             "|---|---|---|---|---|---|---|---|---|"]
    from .state import load_state
    details = []
    for s in sorted(plan.slices, key=lambda s: (s.order, s.id)):
        rep = load_report(project, s.id)
        st = load_state(project, s.id).status
        if rep is None:
            lines.append(f"| {s.id} | {st} | - | - | not run | - | - | - | - |")
            continue
        cnt = {k: sum(d.classification == k for d in rep.diffs)
               for k in ("regression", "unknown", "intended_divergence", "noise")}
        lines.append(f"| {s.id} | {st} | {rep.cases_total} | {rep.cases_equivalent} | {rep.equivalence_pct:.2f} | "
                     f"{cnt['regression']} | {cnt['unknown']} | {cnt['intended_divergence']} | {cnt['noise']} |")
        details.append((s, rep, load_run_meta(project, s.id)))
    for s, rep, meta in details:
        lines += ["", f"## {s.id} {_md(s.name)}", ""]
        if meta:
            lines.append(f"Run {meta.created}; target_code HEAD `{meta.target_head[:12] or 'n/a'}`; regression "
                         f"slices: {', '.join(meta.regression_slices) or 'none'}.")
        for title, cls in (("Regressions", "regression"), ("Unknown differences", "unknown")):
            rows = [d for d in rep.diffs if d.classification == cls]
            if rows:
                lines += ["", f"### {title}", "", "| EQ | Case | Field | Legacy | New | Triage | Note |",
                          "|---|---|---|---|---|---|---|"]
                lines += [f"| {d.id} | {d.case_id} | {_md(d.field)} | {_md(d.legacy)} | {_md(d.new)} | "
                          f"{d.triage or '-'} | {_md(d.note)} |" for d in rows]
        rows = [d for d in rep.diffs if d.classification == "intended_divergence"]
        if rows:
            lines += ["", "### Intended divergences", "", "| EQ | Case | Field | Decision |", "|---|---|---|---|"]
            lines += [f"| {d.id} | {d.case_id} | {_md(d.field)} | {_md(d.decision_ref)} |" for d in rows]
        if rep.untestable:
            lines += ["", "### Untestable items", ""]
            lines += [f"- {k}: {_md(v)}" for k, v in sorted(rep.untestable.items())]
        if meta:
            nc = [o for o in meta.cases if not o.compared]
            if nc:
                lines += ["", "### Cases not compared", ""]
                lines += [f"- {o.case_id}: {_md(o.legacy_error or o.legacy_outcome)}" for o in nc]
            lat = meta.latency
            if lat:
                lines += ["", "### Latency (advisory)", "",
                          f"{lat['cases']} case(s) timed on both sides. p50 legacy {lat['legacy_p50_s']}s, new "
                          f"{lat['new_p50_s']}s (x{lat['ratio_p50']}); p95 legacy {lat['legacy_p95_s']}s, new "
                          f"{lat['new_p95_s']}s (x{lat['ratio_p95']}). Sandbox timings are noisy: a smoke signal, "
                          "not a benchmark."]
                if lat["slower"]:
                    lines += ["", f"New service more than x{lat['ratio_limit']} slower (and at least "
                                  f"{lat['min_delta_s']}s):", "", "| Case | Legacy s | New s | Ratio |", "|---|---|---|---|"]
                    lines += [f"| {x['case_id']} | {x['legacy_s']} | {x['new_s']} | {x['ratio']} |"
                              for x in lat["slower"][:20]]
            chans = sorted({c for o in meta.cases for c in o.not_compared})
            if chans:
                lines += ["", f"Not compared (not observed by the runner): {', '.join(chans)}."]
    from .differential import load_fuzz_reports
    for fz in load_fuzz_reports(project):
        lines += ["", f"## Differential fuzzing {fz.slice}", "",
                  f"Run {fz.created}; {fz.cases_considered} case(s), {fz.variants_run} variant(s) answered by both "
                  f"services, {fz.variants_skipped_unstable} skipped (legacy unstable), {fz.variants_not_answered} "
                  f"not answered; {len(fz.divergences)} divergence(s), {len(fz.high)} high."
                  + (" Truncated." if fz.truncated else "")]
        if fz.divergences:
            lines += ["", "| Case | Mutation | Field | Legacy | New | Severity |", "|---|---|---|---|---|---|"]
            lines += [f"| {d.case_id} | {_md(d.mutation)} | {_md(d.field)} | {_md(d.legacy)} | {_md(d.new)} | "
                      f"{d.severity} |" for d in fz.divergences[:50]]
    comp = load_composite(project)
    if comp is not None:
        tot = ", ".join(f"{k} {v}" for k, v in sorted(comp.totals.items())) or "no cases"
        lines += ["", "## Composite (strangler router)", "",
                  f"Run {comp.created}; flipped: {', '.join(comp.flipped) or 'none'}; slices: "
                  f"{', '.join(comp.slices) or 'none'}; {tot}" + ("" if comp.ok else " (FAILING)")]
        bad = [c for c in comp.cases if c.outcome in ("fail", "error")]
        if bad:
            lines += ["", "| Case | Slice | Upstream | Outcome | Detail |", "|---|---|---|---|---|"]
            lines += [f"| {c.case_id} | {c.slice or '-'} | {c.upstream} | {c.outcome} | "
                      f"{_md(c.error or '; '.join(str(d.get('field')) for d in c.diffs))} |" for c in bad[:50]]
    return "\n".join(lines) + "\n"


def write_report_md(project) -> Path:
    lay = _lay(project)
    lay.ensure(lay.equivalence_dir)
    return atomic_write_text(lay.equivalence_report_md, render_report_md(project))


# ------------------------------------------------------------------ CLI
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def _make_llm(project, config_path, max_usd):
    import os

    from ..audit import AuditLog
    from ..cli import _budget, _load_cfg
    from ..llm.base import BudgetExceeded
    from ..llm.litellm_client import LiteLLMClient
    from .ledger import budget_for, project_budget_usd
    if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")):
        raise click.ClickException("No LLM API key found (see .env.example), or run with --offline.")
    cfg = _load_cfg(config_path)
    if TASK not in cfg.models:      # cheap tier unless configured explicitly
        cfg.models[TASK] = cfg.models.get("chunk_extract", cfg.default_model)
    try:
        budget = budget_for(project, project_budget_usd(project), max_usd=_budget(max_usd))
    except BudgetExceeded as e:
        raise click.ClickException(str(e))
    return LiteLLMClient(cfg, budget=budget, cache_dir=Path(project.root) / "llm_cache",
                         audit=AuditLog(Path(project.root) / "audit" / "audit.jsonl"), agent="equivalence")


class _DefaultGroup(click.Group):
    """`equivalence PROJECT ...` runs the default sub-command; `equivalence approve-mask ...` the named one."""

    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["run", *args]
        return super().parse_args(ctx, args)


def echo_composite(rep: CompositeReport) -> None:
    click.echo(f"composite: {len(rep.cases)} case(s) from {len(rep.slices)} slice(s), flipped: "
               f"{', '.join(rep.flipped) or 'none'}: " + (", ".join(f"{k} {v}" for k, v in sorted(rep.totals.items()))
                                                           or "no cases"))
    for c in rep.cases:
        if c.outcome in ("fail", "error"):
            detail = c.error or "; ".join(f"{d.get('field')}: {d.get('expected')!r} != {d.get('actual')!r}"
                                          for d in c.diffs[:3])
            click.echo(f"  {c.outcome.upper()} {c.case_id} ({c.slice}, via {c.upstream}): {detail[:300]}")
    click.echo("report: equivalence/composite.json")


def register(groups) -> None:
    grp = _DefaultGroup(name="equivalence", help="Equivalence verification, legacy vs new (plan section 7).\n\n"
                              "Usage: equivalence PROJECT (--slice SLICE-NNNN | --all) "
                              "[--legacy-url URL --new-url URL] [--triage] [--offline] [--max-usd N]   "
                              "(same as `equivalence run ...`; without URLs the cases run in-network against the "
                              "running harness)\n\n"
                              "       equivalence composite PROJECT [--base-url URL]")
    groups.main.add_command(grp)

    @grp.command("run")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--slice", "slice_id", default=None, help="SLICE-NNNN to verify.")
    @click.option("--all", "all_", is_flag=True, help="Every slice in plan order.")
    @click.option("--legacy-url", default=None, help="Base URL of the legacy service.")
    @click.option("--new-url", default=None, help="Base URL of the new service.")
    @click.option("--triage", is_flag=True, help="Ask the cheap model tier to explain unknown diffs.")
    @click.option("--offline", is_flag=True, help="Never call a model.")
    @click.option("--max-usd", type=float, default=None)
    @click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
    @click.option("--timeout", type=float, default=10.0, show_default=True)
    @click.option("--reset/--no-reset", default=True, show_default=True,
                  help="Restart the harness services first (harness mode only) so both start from the same seeded "
                       "state; with URLs you run yourself nothing is restarted.")
    def run_cmd(project: Path, slice_id, all_, legacy_url, new_url, triage, offline, max_usd, config_path, timeout,
                reset):
        """Run a slice's cases (+ approved slices' cases) against both services and classify the differences."""
        from .contract import ContractError, require_contract
        p = _project(project)
        if bool(slice_id) == bool(all_):
            raise click.ClickException("give exactly one of --slice SLICE-NNNN or --all")
        try:
            require_contract(p)
        except ContractError as e:
            raise click.ClickException(f"contract gate: {e}")
        try:
            lu, nu = resolve_urls(p, legacy_url, new_url)
            ids = [slice_id] if slice_id else [s.id for s in sorted(_plan(p).slices, key=lambda s: (s.order, s.id))]
            llm = None
            if triage and not offline:
                llm = _make_llm(p, config_path, max_usd)
            elif triage:
                click.echo("--offline: triage skipped")
            bad = 0
            for sid in ids:
                rep = run_equivalence(p, sid, lu, nu, llm=llm, timeout_s=timeout, reset=reset)
                reg = sum(d.classification == "regression" for d in rep.diffs)
                unk = sum(d.classification == "unknown" for d in rep.diffs)
                bad += reg + unk
                click.echo(f"{sid}: {rep.cases_equivalent}/{rep.cases_total} equivalent ({rep.equivalence_pct:.2f}%), "
                           f"{reg} regression(s), {unk} unknown")
        except EquivalenceError as e:
            raise click.ClickException(str(e))
        click.echo(f"report: {Phase2Layout(p).rel(Phase2Layout(p).equivalence_report_md)}")
        if bad:
            raise SystemExit(1)

    @grp.command("composite")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--base-url", default=None, help="Host-reachable composite URL (default: in-network, running "
                                                    "composite harness).")
    @click.option("--timeout", type=float, default=10.0, show_default=True)
    def composite_cmd(project: Path, base_url, timeout):
        """Run every approved (and flipped) slice's cases against the composite (legacy + new behind the router)."""
        from .contract import ContractError, require_contract
        p = _project(project)
        try:
            require_contract(p)
        except ContractError as e:
            raise click.ClickException(f"contract gate: {e}")
        try:
            rep = run_composite(p, base_url=base_url, timeout_s=timeout)
        except EquivalenceError as e:
            raise click.ClickException(str(e))
        echo_composite(rep)
        if not rep.ok:
            raise SystemExit(1)

    @grp.command("fuzz")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--slice", "slice_id", default=None, help="SLICE-NNNN to fuzz.")
    @click.option("--all", "all_", is_flag=True, help="Every slice in plan order.")
    @click.option("--legacy-url", default=None)
    @click.option("--new-url", default=None)
    @click.option("--max-variants", type=click.IntRange(1, 64), default=8, show_default=True,
                  help="Mutated variants per recorded case.")
    @click.option("--timeout", type=float, default=10.0, show_default=True)
    @click.option("--reset/--no-reset", default=True, show_default=True,
                  help="Restart the harness services first (harness mode only) so both start from the same state.")
    def fuzz_cmd(project: Path, slice_id, all_, legacy_url, new_url, max_variants, timeout, reset):
        """Differential fuzzing: mutate recorded HTTP cases and compare legacy with new (advisory; never a gate)."""
        from .contract import ContractError, require_contract
        from .differential import run_fuzz
        p = _project(project)
        if bool(slice_id) == bool(all_):
            raise click.ClickException("give exactly one of --slice SLICE-NNNN or --all")
        try:
            require_contract(p)
        except ContractError as e:
            raise click.ClickException(f"contract gate: {e}")
        try:
            lu, nu = resolve_urls(p, legacy_url, new_url)
            ids = [slice_id] if slice_id else [s.id for s in sorted(_plan(p).slices, key=lambda s: (s.order, s.id))]
            high = 0
            for sid in ids:
                if reset:
                    reset_harness_sides(p, lu, nu)
                r = run_fuzz(p, sid, lu, nu, max_variants=max_variants, timeout_s=timeout)
                high += len(r.high)
                click.echo(f"{sid}: {r.variants_run} variant(s), {len(r.divergences)} divergence(s) ({len(r.high)} "
                           f"high), {r.variants_skipped_unstable} skipped unstable, {r.variants_not_answered} not answered")
        except EquivalenceError as e:
            raise click.ClickException(str(e))
        click.echo(f"report: {Phase2Layout(p).rel(Phase2Layout(p).equivalence_report_md)}")
        if high:
            raise SystemExit(1)

    @grp.command("approve-mask")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("eq_id")
    @click.option("--by", required=True)
    @click.option("--reason", default="")
    def approve_mask_cmd(project: Path, eq_id: str, by: str, reason: str):
        """Approve the mask hiding the field of EQ-NNNN (applies from the next equivalence run)."""
        try:
            m = approve_mask(_project(project), eq_id, by, reason)
        except EquivalenceError as e:
            raise click.ClickException(str(e))
        click.echo(f"{eq_id}: mask {m.mask} approved for {m.case_id}")
