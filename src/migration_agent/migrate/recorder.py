"""M3 recording, coverage and suite runs (docs/phase2-plan.md 4.3-4.5):
`tests record`, `tests run`, `tests coverage`, `tests mark-untestable`; gate function `coverage_gaps(project)`.

Recording (`record_cases`): every case runs TWICE against the legacy service (two base URLs, e.g. two legacy
instances started with different clocks / seeds, or the same URL twice). Body fields whose values differ between
the two runs become `normalize` masks (`$.a.b`, array indices generalised to `[*]`), differing allowlisted headers
become `headers.<name>` masks, and string leaves that look like secrets (ingest/secrets.py) are masked too, so no
secret is stored as an expected value. Then the expected output is stored from the first run with masked values
replaced by "<masked>": `expect.source="recorded"`, or, for fix-decision cases, the legacy result goes to
`expect.legacy_observed` and the specified expectation is kept. Per-case outcome (RecordReport):
  recorded         stored
  harness_issue    no response in at least one run (connection, timeout, invalid case, job endpoint) - fix the
                   harness, nothing is stored
  unstable         the status differs between the two runs - nothing is stored
  not_implemented  grpc / message cases (runner support pending)
Where the cases run: `--base-url URL` (in-process, host-reachable URL) or `--harness` (in the sandbox: two
runner jobs on the legacy harness's internal network, `migrate.harness.run_cases_in_network`). `tests run`
likewise takes `--base-url` or `--harness --role legacy|new|composite`.
Slice state: after recording, every planned slice whose cases are now all recorded moves to `tests_recorded`
(`mark_slices_recorded`; slices already further along are left alone).
Runtime findings (migration/runtime_findings.json, EQ- ids keyed `runtime:<CT>:<kind>` so re-recording updates
instead of duplicating; a human status other than `open` is kept):
  contradicts_rule  the recording contradicts the hypothesis a business_rule case carries
                    (characterization/hypotheses.json): status, status class or body substring
  legacy_failure    a consistent 5xx from legacy for an http case: a possible legacy bug (still recorded, since
                    legacy behaviour is the truth until someone decides otherwise)

Where results go (chosen for M3): runner passes get RUN- ids from the shared allocator but are NOT exec jobs, so
they are written under characterization/, never runs/ (runs/RUN-NNNN/meta.json is reserved for ExecJob runs):
  characterization/recordings/RUN-NNNN/results.json   observations of one recording pass (redacted)
  characterization/recordings/RUN-NNNN/report.json    RecordReport (in the first pass's folder)
  characterization/results/RUN-NNNN/results.json      RunReport of `tests run` (pass/fail/error + diffs, redacted)
`Expectation.recorded_runs` holds the two recording RUN ids.

Coverage (`coverage_table`): one row per in-scope node (casestore.scope_nodes) with the CT ids covering it,
recorded ones, unapproved specified ones, `untestable` reason (characterization/untestable.json) and deferred
finding blockers. `coverage_gaps(project)` lists nodes with no case and no untestable reason
(`require_recorded=True`: also nodes whose cases are not recorded yet).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from .harness_runner.models import CaseResult

RESULTS_DIRNAME = "results"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseRecording(_Strict):
    case_id: str
    status: Literal["recorded", "harness_issue", "unstable", "not_implemented"]
    masks_added: list[str] = Field(default_factory=list)
    reason: str = ""
    finding_ids: list[str] = Field(default_factory=list)


class RecordReport(_Strict):
    schema_version: Literal[1] = 1
    created: str = ""
    runs: list[str] = Field(default_factory=list)
    base_urls: list[str] = Field(default_factory=list)
    cases: list[CaseRecording] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    whole_body_masked: list[str] = Field(default_factory=list)   # cases whose entire body is noise
    slices_recorded: list[str] = Field(default_factory=list)     # slices moved planned -> tests_recorded

    def by_status(self, status: str) -> list[str]:
        return [c.case_id for c in self.cases if c.status == status]


class CoverageRow(_Strict):
    node_id: str
    type: str
    name: str = ""
    quirk: bool = False
    suspected_bug: bool = False
    decision: str = "preserve"
    slice: str | None = None
    case_ids: list[str] = Field(default_factory=list)
    recorded_case_ids: list[str] = Field(default_factory=list)
    pending_spec_case_ids: list[str] = Field(default_factory=list)   # specified, not approved yet
    untestable: str = ""
    blockers: list[str] = Field(default_factory=list)
    status: Literal["recorded", "cases", "untestable", "gap"]


class CoverageReport(_Strict):
    schema_version: Literal[1] = 1
    created: str = ""
    graph_version: int = 0
    rows: list[CoverageRow] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)


class RunReport(_Strict):
    schema_version: Literal[1] = 1
    run_id: str
    base_url: str
    created: str = ""
    totals: dict[str, int] = Field(default_factory=dict)
    results: list[CaseResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.totals.get("fail") or self.totals.get("error"))


# ------------------------------------------------------------------ helpers
def _select(project, case_ids: list[str] | None):
    from .casestore import load_cases
    cases = load_cases(project)
    if case_ids:
        known = {c.id for c in cases}
        bad = [i for i in case_ids if i not in known]
        if bad:
            raise ValueError(f"unknown case id(s): {', '.join(bad)}")
        cases = [c for c in cases if c.id in set(case_ids)]
    return cases


def _cfg(base_url: str, timeout_s: float, job_endpoint: str | None):
    from .harness_runner import HostNotAllowed, RunnerConfig, parse_base_url
    from .harness_runner.models import DEFAULT_JOB_ENDPOINT
    try:
        parse_base_url(base_url)
    except HostNotAllowed as e:
        raise ValueError(f"invalid base URL {base_url!r}: {e}") from None
    return RunnerConfig(base_url=base_url, timeout_s=timeout_s,
                        job_endpoint=DEFAULT_JOB_ENDPOINT if job_endpoint is None else (job_endpoint or None))


def auto_masks(a, b) -> set[str]:
    """Masks from two observations of the same case: differing fields + secret-looking leaves."""
    from ..ingest.secrets import redact_string
    from .harness_runner.jsonpath import diff_paths, generalize, leaf_paths
    masks = set(generalize(diff_paths(a.body, b.body)))
    for name in set(a.headers) | set(b.headers):
        if a.headers.get(name) != b.headers.get(name):
            masks.add(f"headers.{name}")
    for obs in (a, b):
        for p, v in leaf_paths(obs.body):
            if isinstance(v, str) and redact_string(v) != v:
                masks.update(generalize([p]))
        for name, v in obs.headers.items():
            if redact_string(v) != v:
                masks.add(f"headers.{name}")
    return masks


def contradiction(h, obs) -> str:
    """Why an observation contradicts a rule hypothesis ("" if it does not)."""
    st = obs.status
    if h.status is not None and st != h.status:
        return f"rule predicts status {h.status}, legacy returned {st}"
    if h.status_class and (st is None or f"{st // 100}xx" != h.status_class):
        return f"rule predicts a {h.status_class} status, legacy returned {st}"
    if h.body_contains:
        text = obs.body if isinstance(obs.body, str) else json.dumps(obs.body, sort_keys=True)
        if h.body_contains not in (text or ""):
            return f"rule predicts the body contains {h.body_contains!r}; legacy body does not"
    return ""


def _excerpt(obs) -> str:
    from ..ingest.secrets import redact_string
    return redact_string(json.dumps({"status": obs.status, "body": obs.body}, sort_keys=True, default=str))[:1000]


def _save_findings(project, new: list) -> None:
    from .models import RuntimeFindingsFile
    from .paths import Phase2Layout
    from .state import atomic_write_json, file_lock, read_json
    if not new:
        return
    lay = Phase2Layout(project)
    lay.ensure(lay.migration)
    path = lay.runtime_findings
    with file_lock(path):
        ff = read_json(path, RuntimeFindingsFile, RuntimeFindingsFile())
        by_id = {f.id: i for i, f in enumerate(ff.findings)}
        out = list(ff.findings)
        for f in new:
            if f.id in by_id:
                old = out[by_id[f.id]]
                out[by_id[f.id]] = f.model_copy(update={"status": old.status, "created": old.created or f.created})
            else:
                out.append(f)
        atomic_write_json(path, RuntimeFindingsFile(findings=out))


def load_runtime_findings(project):
    from .models import RuntimeFindingsFile
    from .paths import Phase2Layout
    from .state import read_json
    return read_json(Phase2Layout(project).runtime_findings, RuntimeFindingsFile, RuntimeFindingsFile())


def _write_pass(project, run_id: str, results: list[CaseResult], base_url: str) -> None:
    from ..ingest.secrets import redact_obj
    from .paths import Phase2Layout
    from .state import atomic_write_json
    lay = Phase2Layout(project)
    d = lay.ensure(lay.recordings_dir / run_id)
    atomic_write_json(d / "results.json", {"run_id": run_id, "base_url": base_url,
                                           "results": redact_obj([r.model_dump(mode="json") for r in results])})


# ------------------------------------------------------------------ record
def record_cases(project, base_url: str | None = None, second_base_url: str | None = None, *,
                 case_ids: list[str] | None = None, timeout_s: float = 10.0, job_endpoint: str | None = None,
                 pin: bool = False, harness: bool = False, backend=None) -> RecordReport:
    """Record cases twice against legacy: in-process against `base_url` (and `second_base_url`), or with
    `harness=True` in-network against the running legacy harness (harness.run_cases_in_network, two runner jobs).
    Slices whose cases are then all recorded move planned -> tests_recorded."""
    from ..audit import AuditLog
    from .casestore import load_hypotheses, save_case
    from .contract import require_contract
    from .harness_runner import apply_masks, observe, validate_mask, MaskError
    from .harness_runner.compare import header_masks
    from .ids import allocate
    from .models import CharacterizationCase, Expectation, RuntimeFinding
    from .paths import Phase2Layout
    from .state import atomic_write_json, now_iso

    require_contract(project, pin=pin)
    if harness == bool(base_url):
        raise ValueError("give exactly one of --base-url URL or --harness")
    cases = _select(project, case_ids)
    if harness:
        from . import harness as _h
        try:
            res_a = _h.run_cases_in_network(project, "legacy", cases, backend=backend, compare=False,
                                            timeout_s=timeout_s, job_endpoint=job_endpoint)
            res_b = _h.run_cases_in_network(project, "legacy", cases, backend=backend, compare=False,
                                            timeout_s=timeout_s, job_endpoint=job_endpoint)
        except _h.HarnessError as e:
            raise ValueError(f"harness: {e}") from None
        url_a = url_b = _h.service_urls(project)["legacy"] or "harness:legacy"
    else:
        if second_base_url and not base_url:
            raise ValueError("--second-base-url needs --base-url")
        cfg_a = _cfg(base_url, timeout_s, job_endpoint)
        cfg_b = _cfg(second_base_url or base_url, timeout_s, job_endpoint)
        res_a = [observe(c, cfg_a) for c in cases]
        res_b = [observe(c, cfg_b) for c in cases]
        url_a, url_b = cfg_a.base_url, cfg_b.base_url
    run_a, run_b = allocate(project, "RUN"), allocate(project, "RUN")
    _write_pass(project, run_a, res_a, url_a)
    _write_pass(project, run_b, res_b, url_b)
    hyp = load_hypotheses(project).hypotheses
    report = RecordReport(created=now_iso(), runs=[run_a, run_b], base_urls=[url_a, url_b])
    findings: list[RuntimeFinding] = []

    for case, a, b in zip(cases, res_a, res_b):
        if a.outcome == "not_implemented":
            report.cases.append(CaseRecording(case_id=case.id, status="not_implemented", reason=a.error))
            continue
        if a.outcome == "error" or b.outcome == "error":
            which = "both runs" if a.outcome == b.outcome == "error" else ("run 1" if a.outcome == "error" else "run 2")
            err = a if a.outcome == "error" else b
            report.cases.append(CaseRecording(case_id=case.id, status="harness_issue",
                                              reason=f"{which}: {err.error_class or 'error'}: {err.error}"[:500]))
            continue
        oa, ob = a.observed, b.observed
        if oa.status != ob.status:
            report.cases.append(CaseRecording(case_id=case.id, status="unstable",
                                              reason=f"status differs between runs ({oa.status} vs {ob.status})"))
            continue
        existing = []
        for m in case.normalize:
            try:
                existing.append(validate_mask(m))
            except MaskError:
                continue
        added = sorted(auto_masks(oa, ob) - set(existing))
        masks = sorted(set(existing) | set(added))
        if "$" in masks and oa.body is not None:
            report.whole_body_masked.append(case.id)
        hm = header_masks(masks)
        observed = {"status": oa.status, "headers": {k: v for k, v in oa.headers.items() if k not in hm},
                    "body": apply_masks(oa.body, masks)}
        if case.expect.source == "specified":
            expect = case.expect.model_copy(update={"legacy_observed": observed, "recorded_runs": [run_a, run_b]})
        else:
            expect = Expectation(source="recorded", status=observed["status"], headers=observed["headers"],
                                 body=observed["body"], db_after=case.expect.db_after,
                                 stub_calls=case.expect.stub_calls, messages=case.expect.messages,
                                 recorded_runs=[run_a, run_b])
        new = CharacterizationCase.model_validate({**case.model_dump(), "normalize": masks,
                                                   "expect": expect.model_dump()})
        save_case(project, new)
        rec = CaseRecording(case_id=case.id, status="recorded", masks_added=added)

        h = hyp.get(case.id)
        why = contradiction(h, oa) if h is not None else ""
        if why:
            fid = allocate(project, "EQ", key=f"runtime:{case.id}:contradicts_rule")
            findings.append(RuntimeFinding(
                id=fid, kind="contradicts_rule", title=f"Legacy behaviour contradicts {', '.join(h.rule_ids)}"[:200],
                detail=f"{case.id}: {why}", item_ids=list(h.rule_ids) or list(case.covers), case_ids=[case.id],
                run_id=run_a, observed=_excerpt(oa), created=now_iso()))
            rec.finding_ids.append(fid)
        if case.kind in ("http", "replay") and oa.status is not None and 500 <= oa.status < 600:
            fid = allocate(project, "EQ", key=f"runtime:{case.id}:legacy_failure")
            findings.append(RuntimeFinding(
                id=fid, kind="legacy_failure", title=f"Legacy returns {oa.status} for {case.id}",
                detail=f"{case.id} returned {oa.status} on both recording runs: possible legacy bug "
                       "(recorded as-is; decide preserve or fix)", item_ids=list(case.covers), case_ids=[case.id],
                run_id=run_a, observed=_excerpt(oa), created=now_iso()))
            rec.finding_ids.append(fid)
        report.cases.append(rec)

    _save_findings(project, findings)
    report.findings = [f.id for f in findings]
    lay = Phase2Layout(project)
    report.slices_recorded = mark_slices_recorded(project, {c.slice for c in cases if c.slice})
    atomic_write_json(lay.recordings_dir / run_a / "report.json", report)
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "recorder", "tests_record", runs=[run_a, run_b], cases=len(cases), recorded=len(report.by_status("recorded")),
        harness_issues=len(report.by_status("harness_issue")), unstable=len(report.by_status("unstable")),
        findings=report.findings, harness=harness, slices_recorded=report.slices_recorded)
    return report


def case_is_recorded(case) -> bool:
    """A recorded case has a stored legacy observation: the recorded expectation, or `legacy_observed` for a
    specified (fix-decision) expectation (same rule as the coverage table)."""
    from .harness_runner import is_recorded
    if case.expect.source == "specified":
        return case.expect.legacy_observed is not None
    return is_recorded(case.expect)


def mark_slices_recorded(project, slice_ids) -> list[str]:
    """planned -> tests_recorded for each slice whose characterization cases are ALL recorded (at least one).
    Slices already past `planned` (or blocked/rejected) are left alone. Returns the slices moved."""
    from .casestore import load_cases, load_plan
    from .state import IllegalTransition, load_state, transition
    try:
        plan = load_plan(project)
    except Exception:  # noqa: BLE001 - no / invalid plan: nothing to move
        plan = None
    slice_ids = set(slice_ids or ()) & ({s.id for s in plan.slices} if plan is not None else set())
    if not slice_ids:
        return []
    by_slice: dict[str, list] = {}
    for c in load_cases(project):
        if c.slice in slice_ids:
            by_slice.setdefault(c.slice, []).append(c)
    moved = []
    for sid in sorted(by_slice):
        cases = by_slice[sid]
        if not cases or not all(case_is_recorded(c) for c in cases):
            continue
        try:
            if load_state(project, sid).status != "planned":
                continue
            transition(project, sid, "tests_recorded", actor="recorder",
                       reason=f"all {len(cases)} characterization case(s) recorded")
        except (IllegalTransition, ValueError):
            continue
        moved.append(sid)
    return moved


# ------------------------------------------------------------------ run
def run_suite(project, base_url: str | None = None, *, case_ids: list[str] | None = None, timeout_s: float = 10.0,
              job_endpoint: str | None = None, pin: bool = False, role: str | None = None,
              backend=None) -> RunReport:
    """Run cases against `base_url` (in-process), or with `role` (legacy | new | composite) in-network against
    the running harness role, and compare with their expectations; written to characterization/results/."""
    from ..audit import AuditLog
    from ..ingest.secrets import redact_obj
    from .contract import require_contract
    from .harness_runner import run_cases
    from .ids import allocate
    from .paths import Phase2Layout
    from .state import atomic_write_json, now_iso

    require_contract(project, pin=pin)
    if bool(role) == bool(base_url):
        raise ValueError("give exactly one of --base-url URL or --harness [--role legacy|new|composite]")
    cases = _select(project, case_ids)
    if role:
        from . import harness as _h
        try:
            results = _h.run_cases_in_network(project, role, cases, backend=backend, compare=True,
                                              timeout_s=timeout_s, job_endpoint=job_endpoint)
        except _h.HarnessError as e:
            raise ValueError(f"harness: {e}") from None
        url = _h.service_urls(project).get(role) or f"harness:{role}"
    else:
        cfg = _cfg(base_url, timeout_s, job_endpoint)
        results = run_cases(cases, cfg)
        url = cfg.base_url
    run_id = allocate(project, "RUN")
    totals: dict[str, int] = {}
    for r in results:
        totals[r.outcome] = totals.get(r.outcome, 0) + 1
    rep = RunReport(run_id=run_id, base_url=url, created=now_iso(), totals=totals, results=results)
    lay = Phase2Layout(project)
    d = lay.ensure(lay.characterization / RESULTS_DIRNAME / run_id)
    atomic_write_json(d / "results.json", redact_obj(rep.model_dump(mode="json")))
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "recorder", "tests_run", run_id=run_id, base_url=url, totals=totals, role=role or "")
    return rep


# ------------------------------------------------------------------ coverage
def coverage_table(project) -> CoverageReport:
    from .casestore import contract_graph, load_cases, load_untestable, scope_nodes, slice_of
    from .contract import load_contract
    from .harness_runner import is_recorded
    from .state import now_iso
    contract = load_contract(project)
    graph = contract_graph(project, contract)
    cases = load_cases(project)
    unt = load_untestable(project).nodes
    decisions = {d.item_id: d.decision for d in contract.decisions}
    deferred = [(f.id, set(f.item_ids)) for f in contract.deferred_findings]
    slices = slice_of(project)
    rows = []
    for n in scope_nodes(project, graph):
        cov = [c for c in cases if n.id in c.covers]
        rec = [c.id for c in cov if (is_recorded(c.expect) if c.expect.source == "recorded"
                                     else c.expect.legacy_observed is not None)]
        status = "recorded" if rec else "cases" if cov else "untestable" if n.id in unt else "gap"
        rows.append(CoverageRow(
            node_id=n.id, type=n.type, name=n.name, quirk=n.quirk, suspected_bug=n.suspected_bug,
            decision=decisions.get(n.id, "preserve"), slice=slices.get(n.id), case_ids=[c.id for c in cov],
            recorded_case_ids=rec,
            pending_spec_case_ids=[c.id for c in cov if c.expect.source == "specified" and not c.expect.approved_by],
            untestable=unt[n.id].reason if n.id in unt else "",
            blockers=[fid for fid, items in deferred if n.id in items], status=status))
    totals: dict[str, int] = {}
    for r in rows:
        totals[r.status] = totals.get(r.status, 0) + 1
    return CoverageReport(created=now_iso(), graph_version=graph.version, rows=rows,
                          gaps=[r.node_id for r in rows if r.status == "gap"], totals=totals)


def coverage_gaps(project, require_recorded: bool = False) -> list[str]:
    """Gate for later milestones: in-scope nodes without a case and without an approved untestable reason
    (with `require_recorded`, also nodes whose cases have no recording yet)."""
    rep = coverage_table(project)
    bad = {"gap", "cases"} if require_recorded else {"gap"}
    return [r.node_id for r in rep.rows if r.status in bad]


def write_coverage(project) -> CoverageReport:
    from .paths import Phase2Layout
    from .state import atomic_write_json
    rep = coverage_table(project)
    lay = Phase2Layout(project)
    lay.ensure(lay.characterization)
    atomic_write_json(lay.test_coverage, rep)
    return rep


def render_coverage(rep: CoverageReport) -> str:
    lines = [f"{'NODE':<11} {'TYPE':<14} {'STATUS':<10} CASES / NOTE"]
    for r in rep.rows:
        note = ", ".join(r.case_ids) or (f"untestable: {r.untestable}" if r.untestable else "-")
        extra = []
        if r.quirk or r.suspected_bug:
            extra.append(("quirk" if r.quirk else "suspected bug") + f" ({r.decision})")
        if r.pending_spec_case_ids:
            extra.append("spec pending: " + ", ".join(r.pending_spec_case_ids))
        if r.blockers:
            extra.append("blocked by " + ", ".join(r.blockers))
        lines.append(f"{r.node_id:<11} {r.type:<14} {r.status:<10} {note}" + (f"  [{'; '.join(extra)}]" if extra else ""))
    lines.append("totals: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.totals.items())))
    lines.append("gaps: " + (", ".join(rep.gaps) if rep.gaps else "none"))
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def _errors():
    from .contract import ContractError
    return (ContractError, ValueError)


def register(groups) -> None:
    @groups.tests.command("record")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--base-url", default=None, help="Where the legacy service listens (e.g. http://127.0.0.1:8080).")
    @click.option("--second-base-url", default=None,
                  help="A second legacy instance (other clock/seed) for the second run; default: --base-url again.")
    @click.option("--harness", "use_harness", is_flag=True,
                  help="Record in the sandbox against the running legacy harness (`harness up --legacy`), twice.")
    @click.option("--case", "case_ids", multiple=True, help="Only these CT ids (repeatable).")
    @click.option("--timeout", type=float, default=10.0, show_default=True)
    @click.option("--job-endpoint", default=None, help="Harness job trigger path (default /__harness/job).")
    @click.option("--pin-contract", "pin", is_flag=True)
    def record_cmd(project: Path, base_url: str | None, second_base_url: str | None, use_harness: bool,
                   case_ids: tuple[str, ...], timeout: float, job_endpoint: str | None, pin: bool) -> None:
        """Run every case twice against legacy, mask noise, store expected outputs."""
        try:
            rep = record_cases(_project(project), base_url, second_base_url, case_ids=list(case_ids) or None,
                               timeout_s=timeout, job_endpoint=job_endpoint, pin=pin, harness=use_harness)
        except _errors() as e:
            raise click.ClickException(str(e))
        click.echo(f"recorded {len(rep.by_status('recorded'))}/{len(rep.cases)} case(s) (runs {', '.join(rep.runs)})")
        if rep.slices_recorded:
            click.echo(f"  slices now tests_recorded: {', '.join(rep.slices_recorded)}")
        for c in rep.cases:
            if c.masks_added:
                click.echo(f"  {c.case_id}: masked {', '.join(c.masks_added)}")
            if c.status != "recorded":
                click.echo(f"  {c.case_id}: {c.status}: {c.reason}")
        for fid in rep.findings:
            click.echo(f"  runtime finding {fid} (migration/runtime_findings.json)")

    @groups.tests.command("run")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--base-url", default=None, help="Where the service under test listens.")
    @click.option("--harness", "use_harness", is_flag=True,
                  help="Run in the sandbox against a running harness role (see --role).")
    @click.option("--role", type=click.Choice(["legacy", "new", "composite"]), default=None,
                  help="Harness role for --harness (default legacy).")
    @click.option("--case", "case_ids", multiple=True, help="Only these CT ids (repeatable).")
    @click.option("--timeout", type=float, default=10.0, show_default=True)
    @click.option("--job-endpoint", default=None, help="Harness job trigger path (default /__harness/job).")
    @click.option("--pin-contract", "pin", is_flag=True)
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def run_cmd(project: Path, base_url: str | None, use_harness: bool, role: str | None, case_ids: tuple[str, ...],
                timeout: float, job_endpoint: str | None, pin: bool, fmt: str) -> None:
        """Run cases against a service and compare with the expectations (exit 1 on any fail/error)."""
        if role and not use_harness:
            raise click.ClickException("--role needs --harness")
        try:
            rep = run_suite(_project(project), base_url, case_ids=list(case_ids) or None, timeout_s=timeout,
                            job_endpoint=job_endpoint, pin=pin, role=(role or "legacy") if use_harness else None)
        except _errors() as e:
            raise click.ClickException(str(e))
        if fmt == "json":
            click.echo(rep.model_dump_json(indent=2))
        else:
            click.echo(f"{rep.run_id}: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.totals.items())))
            for r in rep.results:
                if r.outcome in ("fail", "error"):
                    detail = r.error or "; ".join(f"{d.field}: {d.expected!r} != {d.actual!r}" for d in r.diffs[:5])
                    click.echo(f"  {r.outcome.upper()} {r.case_id}: {detail[:300]}")
            click.echo(f"results: characterization/{RESULTS_DIRNAME}/{rep.run_id}/results.json")
        if not rep.ok:
            raise SystemExit(1)

    @groups.tests.command("coverage")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    @click.option("--fail-on-gaps", is_flag=True, help="Exit 1 when any in-scope node has no case or untestable reason.")
    def coverage_cmd(project: Path, fmt: str, fail_on_gaps: bool) -> None:
        """Coverage table: in-scope node -> CT ids (writes characterization/coverage.json)."""
        try:
            rep = write_coverage(_project(project))
        except _errors() as e:
            raise click.ClickException(str(e))
        click.echo(rep.model_dump_json(indent=2) if fmt == "json" else render_coverage(rep))
        if fail_on_gaps and rep.gaps:
            raise SystemExit(1)

    @groups.tests.command("mark-untestable")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("node_id")
    @click.option("--reason", required=True)
    @click.option("--by", default="user", show_default=True, help="Who approves the reason.")
    def mark_untestable_cmd(project: Path, node_id: str, reason: str, by: str) -> None:
        """Record an approved reason why NODE cannot be covered by a black-box case."""
        from .casestore import mark_untestable
        try:
            mark_untestable(_project(project), node_id, reason, by)
        except _errors() as e:
            raise click.ClickException(str(e))
        click.echo(f"{node_id}: untestable ({reason})")

