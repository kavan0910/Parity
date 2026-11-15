"""M4 generation loop: deterministic per-slice orchestrator (docs/phase2-plan.md 5.1, 5.5, 6; decision P2-Q6).

    migration-agent generate PROJECT (--slice SLICE-NNNN | --all) [--dry-run] [--from-comments] [--offline]
                             [--max-usd N] [--max-usd-per-slice N] [--max-iterations 4] [--config PATH]

The model is called only for `codegen` (generate / fix) and `code_review`; it has no exec or network tools.
Builds, unit tests and the characterization suite run in the exec sandbox (ExecJobs via exec.get_backend:
build phase with the mirror network per policy, test phase with no network). The host never runs generated code.

Per slice (state machine in state.py; every transition audited):
    planned -> tests_recorded     every characterization case of the slice is recorded (entry slices need >= 1)
    tests_recorded -> generating  iteration K starts (loop.json remembers the loop start and base commit)
    generating:   context pack -> codegen call(s) -> validate/apply (codegen.py) -> agent commit + tag
                  slice/SLICE-NNNN/iter-K -> build job              => built, or the fix loop
    built:        unit-test job + characterization suite against the new service (harness_suite: build + up the
                  new harness service, run the cases in-network with harness.run_cases_in_network); when the
                  harness cannot start/reach the new service the suite is `skipped` and the slice stays `built`
                                                                    => tests_passing, or the fix loop
    tests_passing: fresh-session review (reviewer.py: model + static deny checks)  => reviewed
    reviewed:     blocking findings => fix loop, else                => awaiting_human
    fix loop:     generating (K+1) with only the relevant failures, until --max-iterations => blocked (reason)
Refused (no state change): blocked slices, slices with unaccepted blockers, slices whose depends_on are not
approved, entry slices without recorded cases, a dirty target_code/ (uncommitted human edits are never touched).

Resumability: each step's result is a file under slices/SLICE-NNNN/iterations/K/ and is reused on the next run:
    prompt_meta.json  prompt version, sha256, section token counts per attempt (written BEFORE each call)
    edits.json        every model reply (redacted) + validation outcome, written right AFTER each call;
                      status pending | accepted | rejected | applied, commit sha, changed paths
    build.json        build job result      tests.json   unit + characterization results
    review.json       ReviewResult          outcome.json failure that sent the slice to the next iteration
A crash therefore loses at most the one model call in flight (unchanged prompts also hit llm_cache/).
Half-applied edits (crash between write and commit) are rolled back to HEAD before re-applying.

Budget: `ledger.budget_for` caps the LiteLLM client at min(project budget left, --max-usd); the per-slice cap
(--max-usd-per-slice, default 3x the plan estimate) is checked against `Ledger.by_slice` before every call
(model calls are audited with slice=SLICE-NNNN); reaching it blocks the slice. A cap is checked before a call,
so spend can exceed it by at most one call.

`--from-comments` consumes unprocessed comments for the slice from migration/comments.json
({"comments": [{id, item_id, file, line, text, processed}]}; migrate/approvals.py functions are used when that
module provides them) and reopens awaiting_human / rejected / approved slices with them as failures.
Equivalence fix requests (slices/SLICE-NNNN/fix_requests.json, written by equivalence.py): pending requests
reopen awaiting_human / rejected slices (approved slices are not reopened automatically) and are listed in the
context pack's relevant failures of every iteration of that fix loop (loop.json `fix_requests` remembers which
EQ ids each iteration saw). When the loop reaches awaiting_human they are marked `done`; the next equivalence run
resolves them or raises them again.
Iteration file contract with approvals.approval_blockers: build.json carries `ok` (bool) and `exit_code`;
tests.json carries `ok` (bool), `status`/`result` (passed | failed | skipped) and, when the characterization suite
ran, `failed` (list of CT ids). Approvals reads `failed` first, then `ok`.
Codemods (codemods/) run once, before the first slice, for in_place version upgrades.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from ..audit import AuditLog
from ..llm.base import BudgetExceeded

DEFAULT_MAX_ITERATIONS = 4
SLICE_CAP_FACTOR = 3.0
MAX_APPLY_RETRIES = 2
MAX_NEED_ROUNDS = 1
FAILURE_CHARS = 6_000
RESPONSE_STORE_CHARS = 400_000

# build tool -> (default image, build argv, unit-test argv, env)
TOOLCHAINS: dict[str, tuple[str, list[str], list[str] | None, dict[str, str]]] = {
    "maven": ("maven:3.9-eclipse-temurin-21", ["mvn", "-B", "-q", "--no-transfer-progress", "-DskipTests",
                                                "package"], ["mvn", "-B", "-q", "--no-transfer-progress", "-o", "test"],
              {}),
    "gradle": ("gradle:8-jdk21", ["gradle", "--no-daemon", "-q", "assemble"],
               ["gradle", "--no-daemon", "-q", "--offline", "test"], {}),
    "npm": ("node:20-alpine", ["npm", "install", "--no-audit", "--no-fund"], ["npm", "test", "--silent"], {}),
    "pip": ("python:3.12-slim", ["pip", "install", "--no-cache-dir", "--target", "/work/app/.deps", "-r",
                                 "requirements.txt"], ["python", "-m", "pytest", "-q"],
            {"PYTHONPATH": "/work/app/.deps"}),
    "go-mod": ("golang:1.22", ["go", "build", "./..."], ["go", "test", "./..."], {}),
}


class GenerateError(RuntimeError):
    """The run cannot proceed (no plan, dirty target_code/, invalid target spec ...)."""


class SliceBudgetExceeded(BudgetExceeded):
    pass


class RunBudgetExceeded(BudgetExceeded):
    pass


class _Stop(Exception):
    """Stop this slice for now without changing its state (sandbox unavailable, suite skipped ...)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuiteResult(_Strict):
    status: Literal["passed", "failed", "skipped"]
    reason: str = ""
    totals: dict[str, int] = Field(default_factory=dict)
    failures: list[dict] = Field(default_factory=list)


class SliceComment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    item_id: str = ""
    file: str = ""
    line: int | None = None
    text: str = ""
    processed: bool = False


class SliceOutcome(_Strict):
    slice_id: str
    action: Literal["ran", "refused", "nothing_to_do", "stopped", "dry_run"]
    status: str = ""
    reason: str = ""
    iterations: int = 0
    model_calls: int = 0
    spent_usd: float = 0.0
    estimate_usd: float = 0.0
    context_tokens: int = 0
    cap_usd: float | None = None


class GenerateReport(_Strict):
    outcomes: list[SliceOutcome] = Field(default_factory=list)
    codemods: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stopped: str = ""


@dataclass
class GenerateOptions:
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_usd: float | None = None
    max_usd_per_slice: float | None = None
    from_comments: bool = False
    pack_tokens: int | None = None
    pin: bool = False
    accept_unmapped: bool = False


SuiteRunner = Callable[..., SuiteResult]   # (project, slice, cases, *, backend) -> SuiteResult


# ------------------------------------------------------------------ audit tagging
class TaggedAudit(AuditLog):
    """AuditLog that adds `tags` (e.g. slice=SLICE-NNNN) to every record (Ledger.by_slice reads `slice`)."""

    def __init__(self, path):
        super().__init__(path)
        self.tags: dict[str, str] = {}

    def record(self, agent, action, **kw):
        for k, v in self.tags.items():
            kw.setdefault(k, v)
        return super().record(agent, action, **kw)


# ------------------------------------------------------------------ comments
def _comments_reader(project):
    try:
        from . import approvals  # type: ignore[attr-defined]
    except ImportError:
        return None
    if callable(getattr(approvals, "unprocessed_comments", None)) and \
            callable(getattr(approvals, "mark_comments_processed", None)):
        return approvals
    return None


def load_comments(project) -> list[SliceComment]:
    from .paths import Phase2Layout
    p = Phase2Layout(project).comments
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    items = data.get("comments", []) if isinstance(data, dict) else data
    out = []
    for it in items if isinstance(items, list) else []:
        try:
            out.append(SliceComment.model_validate(it))
        except Exception:  # noqa: BLE001 - foreign/partial entries are skipped
            continue
    return out


def unprocessed_comments(project, slice_, written_files: set[str] | None = None) -> list[SliceComment]:
    mod = _comments_reader(project)
    if mod is not None:
        return [SliceComment.model_validate(c if isinstance(c, dict) else c.model_dump())
                for c in mod.unprocessed_comments(project, slice_.id)]
    nodes = set(slice_.node_ids)
    files = written_files or set()
    return [c for c in load_comments(project) if not c.processed and
            (c.item_id in nodes or c.item_id == slice_.id or (c.file and c.file in files))]


def mark_comments_processed(project, ids: list[str]) -> None:
    if not ids:
        return
    mod = _comments_reader(project)
    if mod is not None:
        mod.mark_comments_processed(project, ids)
        return
    from .paths import Phase2Layout
    from .state import atomic_write_json, file_lock
    p = Phase2Layout(project).comments
    with file_lock(p):
        data = json.loads(p.read_text()) if p.exists() else {"comments": []}
        items = data.get("comments", []) if isinstance(data, dict) else data
        for it in items:
            if isinstance(it, dict) and it.get("id") in set(ids):
                it["processed"] = True
        atomic_write_json(p, data if isinstance(data, dict) else {"comments": items})


def human_commits(project, slice_id: str) -> list[dict]:
    """Commits by non-agent authors touching the slice (for metrics). See targetrepo.human_commits."""
    from .targetrepo import human_commits as _hc
    return _hc(project, slice_id)


# ------------------------------------------------------------------ characterization suite
def harness_suite(project, slice_, cases, *, backend) -> SuiteResult:
    """Run the slice's cases against the NEW service in the sandbox: (re)build target_code with
    `harness build --new`, start it (`harness up --new`; a running new service is stale by construction and is
    restarted), run the cases in-network (harness.run_cases_in_network, a runner job on the new service's
    internal network) and stop it again unless it was up before. Skipped (never a pass) when the harness cannot
    build/start the new service or the runner job cannot run."""
    if not cases:
        return SuiteResult(status="passed", reason="no characterization cases for this slice")
    from . import harness
    try:
        hs = harness.load_spec(project, adopt=True)
        harness.resolve_service(project, hs, "new")
    except Exception as e:  # noqa: BLE001
        return SuiteResult(status="skipped", reason=f"harness cannot start the new service yet: {e}")
    if not hasattr(backend, "start_service"):
        return SuiteResult(status="skipped",
                           reason=f"exec backend {getattr(backend, 'name', '?')} cannot run long-lived services")
    was_up = bool(harness.service_urls(project).get("new"))
    try:
        if was_up:
            harness.down(project, "new", backend=backend)
        ent = harness.build(project, "new", backend=backend)
        if not ent.get("ok"):
            return SuiteResult(status="skipped", reason=f"harness build of the new service failed "
                               f"({ent.get('build_run')}, exit {ent.get('exit_code')}): {ent.get('error') or ''}"[:400])
        harness.up(project, "new", backend=backend)
    except Exception as e:  # noqa: BLE001
        return SuiteResult(status="skipped", reason=f"harness could not start the new service: {str(e)[:300]}")
    try:
        results = harness.run_cases_in_network(project, "new", cases, backend=backend, compare=True)
    except Exception as e:  # noqa: BLE001 - runner job could not run: not a verdict on the code
        return SuiteResult(status="skipped", reason=f"in-network runner failed: {str(e)[:300]}")
    finally:
        if not was_up:
            try:
                harness.down(project, "new", backend=backend)
            except Exception:  # noqa: BLE001
                pass
    return suite_from_results(results)


def suite_from_results(results) -> SuiteResult:
    totals: dict[str, int] = {}
    fails = []
    for r in results:
        totals[r.outcome] = totals.get(r.outcome, 0) + 1
        if r.outcome != "pass":
            fails.append({"case_id": r.case_id, "outcome": r.outcome, "error": r.error[:300],
                          "diffs": [d.model_dump(mode="json") for d in r.diffs[:5]]})
    return SuiteResult(status="failed" if fails else "passed", totals=totals, failures=fails)


# ------------------------------------------------------------------ helpers
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text: str, n: int = FAILURE_CHARS) -> str:
    return text if len(text) <= n else "[... earlier output cut ...]\n" + text[-n:]


def _copy_workspace(src: Path, dst: Path) -> None:
    import os
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    for dp, _dirs, names in os.walk(dst):
        os.chmod(dp, 0o777)
        for n in names:
            p = Path(dp) / n
            if not p.is_symlink():
                os.chmod(p, (p.stat().st_mode | 0o666) & 0o777)


def accepted_blockers(project, slice_id: str) -> set[str]:
    """Blockers accepted for the slice in slices/SLICE-NNNN/decisions.json (tolerant reader)."""
    from .paths import Phase2Layout
    from .state import read_json
    try:
        d = read_json(Phase2Layout(project).slice_decisions(slice_id), default={})
    except ValueError:
        return set()
    out = set(d.get("accepted_blockers") or []) if isinstance(d, dict) else set()
    for ent in (d.get("decisions") or []) if isinstance(d, dict) else []:
        if not isinstance(ent, dict):
            continue
        if ent.get("blocker") and ent.get("decision") in ("accept", "accept_as_is", "migrate_as_is"):
            out.add(ent["blocker"])
        # migrate/approvals.py SliceDecisionsFile: {kind: accept_observed, ref: F-NNNN, status: resolved}
        if ent.get("kind") == "accept_observed" and ent.get("status") == "resolved" and ent.get("ref"):
            out.add(ent["ref"])
    return out


def _toolchain(project, spec):
    """(image, build argv, build network, test argv | None, env, source) or a reason string."""
    try:
        from . import harness
        hs = harness.load_spec(project)
        svc = hs.new
    except Exception:  # noqa: BLE001
        svc = None
    tool = (spec.target.build or "").lower()
    tc = TOOLCHAINS.get(tool)
    if svc is not None:
        test = tc[2] if tc else None
        return svc.image, list(svc.build.command), svc.build.network, test, dict(svc.build.env), "harness new"
    if tc is None:
        return f"no build command for target.build {spec.target.build!r}: add a `new:` service to harness.yaml"
    image = spec.target.base_image or tc[0]
    return image, tc[1], "mirror", tc[2], tc[3], f"default {tool} toolchain"


# ------------------------------------------------------------------ the runner
class _Accounted:
    """Wraps the LLM client: budget checks before, audit (slice-tagged) and spend tracking after each call."""

    def __init__(self, inner, runner: "SliceRunner"):
        self.inner, self.runner = inner, runner
        self.self_audits = getattr(inner, "audit", None) is not None

    def complete(self, *, task, system, prompt, json_mode=True, prompt_version="v1"):
        r = self.runner
        r.check_budget()
        resp = self.inner.complete(task=task, system=system, prompt=prompt, json_mode=json_mode,
                                   prompt_version=prompt_version)
        r.calls += 1
        r.slice_calls += 1
        r.inv_spent += float(resp.cost_usd or 0.0)
        if not self.self_audits:
            r.audit.record("codegen", "llm_call", model=resp.model, tokens_in=resp.tokens_in,
                           tokens_out=resp.tokens_out, cost_usd=resp.cost_usd, prompt_version=prompt_version,
                           input_hash=_sha("\x00".join([resp.model, system, prompt, prompt_version])), task=task,
                           cached=resp.cached, slice=r.current or "")
        return resp


class SliceRunner:
    def __init__(self, project, *, llm, backend=None, suite_runner: SuiteRunner | None = None,
                 options: GenerateOptions | None = None, audit: AuditLog | None = None):
        from . import exec as _exec
        from .casestore import contract_graph, load_plan
        from .contract import ContractError, require_contract
        from .paths import Phase2Layout
        from .target import TargetSpecError, load_target
        self.project = project
        self.opts = options or GenerateOptions()
        self.lay = Phase2Layout(project)
        try:
            self.contract = require_contract(project, pin=self.opts.pin, accept_unmapped=self.opts.accept_unmapped)
        except ContractError as e:
            raise GenerateError(str(e)) from None
        try:
            self.spec = load_target(project)
        except TargetSpecError as e:
            raise GenerateError(str(e)) from None
        self.plan = load_plan(project)
        if self.plan is None:
            raise GenerateError("no valid plan/migration_plan.json: run `migration-agent plan PROJECT` first")
        self.graph = contract_graph(project, self.contract)
        self.backend = backend if backend is not None else _exec.get_backend(project)
        self.suite_runner = suite_runner or harness_suite
        self.audit = audit if audit is not None else TaggedAudit(Path(project.audit_dir) / "audit.jsonl")
        self.llm = _Accounted(llm, self)
        self.calls = 0
        self.slice_calls = 0
        self.inv_spent = 0.0
        self.current: str | None = None
        self.slice_cap: float | None = None
        self._risks, self._resolutions = self._load_fit()

    # ---- inputs
    def _load_fit(self):
        try:
            from .fit import load_report, load_resolutions
            rep = load_report(self.project)
            return (rep.risks if rep else []), load_resolutions(self.project).current()
        except Exception:  # noqa: BLE001 - no / invalid fit report: no resolutions in the spec
            return [], {}

    def slice(self, sid: str):
        for s in self.plan.slices:
            if s.id == sid:
                return s
        raise GenerateError(f"unknown slice {sid} (not in plan/migration_plan.json)")

    def cases(self, sid: str):
        from .casestore import load_cases
        return [c for c in load_cases(self.project) if c.slice == sid]

    def fix_requests(self, sid: str) -> list:
        """Pending equivalence fix requests (slices/SLICE-NNNN/fix_requests.json)."""
        from .equivalence import pending_fix_requests
        try:
            return pending_fix_requests(self.project, sid)
        except Exception:  # noqa: BLE001 - corrupt file: surfaced by approvals, not fatal here
            return []

    def dep_status(self, sl) -> dict[str, str]:
        from .state import load_state
        return {d: load_state(self.project, d).status for d in sl.depends_on}

    # ---- budget
    def check_budget(self) -> None:
        from .ledger import project_budget_usd, remaining, spent
        if self.opts.max_usd is not None and self.inv_spent >= self.opts.max_usd:
            raise RunBudgetExceeded(f"--max-usd {self.opts.max_usd:.4f} reached (spent {self.inv_spent:.4f} USD "
                                    "in this run)")
        rem = remaining(self.project, project_budget_usd(self.project))
        if rem is not None and rem <= 0:
            raise RunBudgetExceeded("project budget (target.yaml budget_usd) exhausted")
        if self.current and self.slice_cap is not None:
            got = spent(self.project).by_slice.get(self.current, 0.0)
            if got >= self.slice_cap:
                raise SliceBudgetExceeded(f"per-slice budget reached: spent {got:.4f} of {self.slice_cap:.4f} USD")

    # ---- files
    def itdir(self, sid: str, k: int) -> Path:
        return self.lay.slice_iterations(sid) / str(k)

    def _read(self, sid: str, k: int, name: str):
        from .state import read_json
        return read_json(self.itdir(sid, k) / name)

    def _write(self, sid: str, k: int, name: str, obj) -> None:
        from .state import atomic_write_json
        self.lay.ensure(self.itdir(sid, k))
        atomic_write_json(self.itdir(sid, k) / name, obj)

    def loop(self, sid: str) -> dict:
        from .state import read_json
        return read_json(self.lay.slice_dir(sid) / "loop.json", default={}) or {}

    def _save_loop(self, sid: str, data: dict) -> None:
        from .state import atomic_write_json
        self.lay.ensure(self.lay.slice_dir(sid))
        atomic_write_json(self.lay.slice_dir(sid) / "loop.json", data)

    def _span(self, sid: str, step: str, k: int):
        return self.audit.span("slice_runner", "generate_step", slice=sid, step=step, iteration=k)

    # ---- refusal checks (read-only)
    def refusal(self, sl, st, comments: list[SliceComment]) -> tuple[str, str]:
        """(action, reason): ("", "") to run; ("refused" | "nothing_to_do", reason) otherwise."""
        from .harness_runner import is_recorded
        if st.status == "blocked":
            return "refused", f"slice is blocked: {st.reason}"
        open_b = [b for b in sl.blockers if b not in accepted_blockers(self.project, sl.id)]
        if open_b:
            return "refused", (f"slice has unresolved blockers {', '.join(open_b)}: resolve them or accept "
                               "'migrate legacy behaviour as-is' for this slice first")
        deps = self.dep_status(sl)
        unmet = [f"{d} ({s})" for d, s in deps.items() if s != "approved"]
        if unmet:
            return "refused", f"depends on slices that are not approved yet: {', '.join(unmet)}"
        if st.status in ("awaiting_human", "approved", "rejected"):
            if self.opts.from_comments and comments:
                return "", ""
            fixes = self.fix_requests(sl.id)
            if fixes and st.status != "approved":
                return "", ""
            return "nothing_to_do", {"awaiting_human": "awaiting human review",
                                     "approved": "already approved" + (
                                         f" ({len(fixes)} pending fix request(s) are not applied to approved "
                                         "slices)" if fixes else ""),
                                     "rejected": "rejected: add comments and run with --from-comments"}[st.status]
        if st.status == "planned":
            cases = self.cases(sl.id)
            if sl.kind == "entry" and not cases:
                return "refused", ("no characterization cases for this entry slice: run `tests generate` and "
                                   "`tests record` first")
            unrec = [c.id for c in cases if not is_recorded(c.expect)]
            if unrec:
                return "refused", f"characterization cases not recorded yet: {', '.join(unrec[:10])}"
        return "", ""

    # ---- main
    def run(self, slice_ids: list[str]) -> GenerateReport:
        rep = GenerateReport()
        for sid in slice_ids:
            sl = self.slice(sid)
            try:
                rep.outcomes.append(self.run_slice(sl))
            except RunBudgetExceeded as e:
                rep.stopped = str(e)
                rep.outcomes.append(SliceOutcome(slice_id=sid, action="stopped", reason=str(e),
                                                 status=self._status(sid)))
                break
            finally:
                self.current = None
                if isinstance(self.audit, TaggedAudit):
                    self.audit.tags.pop("slice", None)
        return rep

    def _status(self, sid: str) -> str:
        from .state import load_state
        return load_state(self.project, sid).status

    def run_slice(self, sl) -> SliceOutcome:
        from .codegen import slice_written_files
        from .ledger import spent
        from .state import load_state, transition
        sid = sl.id
        st = load_state(self.project, sid)
        comments = unprocessed_comments(self.project, sl, set(slice_written_files(self.project, sid))) \
            if self.opts.from_comments else []
        cap = self.opts.max_usd_per_slice
        if cap is None and sl.estimate_usd > 0:
            cap = round(SLICE_CAP_FACTOR * sl.estimate_usd, 6)
        out = SliceOutcome(slice_id=sid, action="ran", status=st.status, cap_usd=cap, estimate_usd=sl.estimate_usd)
        action, reason = self.refusal(sl, st, comments)
        if action:
            out.action, out.reason = action, reason
            return out
        self.current, self.slice_cap, self.slice_calls = sid, cap, 0
        if isinstance(self.audit, TaggedAudit):
            self.audit.tags["slice"] = sid
        before = spent(self.project).by_slice.get(sid, 0.0)
        try:
            with self.audit.span("slice_runner", "generate_slice", slice=sid, from_status=st.status) as sp:
                try:
                    self._drive(sl, comments)
                except SliceBudgetExceeded as e:
                    cur = load_state(self.project, sid)
                    if cur.status not in ("blocked", "approved"):
                        transition(self.project, sid, "blocked", actor="agent", reason=str(e))
                    out.reason = str(e)
                except _Stop as e:
                    out.action, out.reason = "stopped", str(e)
                sp["to_status"] = load_state(self.project, sid).status
        finally:
            final = load_state(self.project, sid)
            out.status = final.status
            out.iterations = final.iteration
            out.model_calls = self.slice_calls
            out.spent_usd = round(spent(self.project).by_slice.get(sid, 0.0) - before, 6)
        if final.status == "blocked" and not out.reason:
            out.reason = final.reason
        return out

    def _start_loop(self, sl, comments: list[SliceComment]) -> None:
        from . import targetrepo
        from .state import load_state
        st = load_state(self.project, sl.id)
        k = st.iteration + 1
        data = self.loop(sl.id)
        data.update({"start": k, "base_commit": targetrepo.head(self.project),
                     "comments": {str(k): [c.model_dump(mode="json") for c in comments]} if comments else {}})
        self._save_loop(sl.id, data)

    def _drive(self, sl, comments: list[SliceComment]) -> None:
        from .state import load_state, transition
        sid = sl.id
        st = load_state(self.project, sid)
        if st.status == "planned":
            n = len(self.cases(sid))
            transition(self.project, sid, "tests_recorded", reason=f"{n} recorded characterization case(s)")
            st = load_state(self.project, sid)
        if st.status in ("tests_recorded", "awaiting_human", "rejected", "approved"):
            fixes = self.fix_requests(sid) if st.status in ("awaiting_human", "rejected") else []
            if st.status != "tests_recorded" and not comments and not fixes:
                return
            self._start_loop(sl, comments)
            transition(self.project, sid, "generating",
                       reason="start generation" if st.status == "tests_recorded" else
                       ", ".join(x for x in ("human comments" if comments else "",
                                             f"{len(fixes)} equivalence fix request(s)" if fixes else "") if x))
        while True:
            st = load_state(self.project, sid)
            k = st.iteration
            if st.status == "generating":
                ok, failure, stage = self.step_generate(sl, k)
                if ok:
                    ok, failure = self.step_build(sl, k)
                    stage = "build"
                if ok:
                    transition(self.project, sid, "built", reason=f"iteration {k} built")
                    continue
                if not self.next_iteration(sl, k, stage, failure):
                    return
            elif st.status == "built":
                res = self.step_tests(sl, k)
                if res["result"] == "skipped":
                    raise _Stop(f"stopped at `built`: {res['reason']}")
                if res["result"] == "passed":
                    transition(self.project, sid, "tests_passing", reason=f"iteration {k}: tests passed")
                    continue
                if not self.next_iteration(sl, k, "tests", res["failure"]):
                    return
            elif st.status == "tests_passing":
                rv = self.step_review(sl, k)
                transition(self.project, sid, "reviewed",
                           reason=f"{len(rv.findings)} finding(s), {len(rv.blocking)} blocking")
            elif st.status == "reviewed":
                from .reviewer import ReviewResult
                rv = ReviewResult.model_validate(self._read(sid, k, "review.json"))
                if rv.blocking:
                    text = "\n".join(f"- [{f.kind}{'/' + f.rule if f.rule else ''}] {f.file}:{f.line or '?'} "
                                     f"{f.detail} | quote: {f.quote}" for f in rv.blocking)
                    if not self.next_iteration(sl, k, "review", "Blocking review findings:\n" + text):
                        return
                    continue
                self._fix_requests_done(sid, k)
                transition(self.project, sid, "awaiting_human", reason=f"iteration {k} ready for human review")
                return
            else:
                return

    def next_iteration(self, sl, k: int, stage: str, failure: str) -> bool:
        from .state import transition
        self._write(sl.id, k, "outcome.json", {"iteration": k, "result": "failed", "stage": stage,
                                               "failure": _clip(failure)})
        loop = self.loop(sl.id)
        n = k - int(loop.get("start", 1)) + 1
        if n >= self.opts.max_iterations:
            transition(self.project, sl.id, "blocked", reason=(
                f"fix loop stopped after {n} iteration(s) (--max-iterations {self.opts.max_iterations}); "
                f"last failure at {stage}: {failure.strip().splitlines()[0][:200] if failure.strip() else '?'}"))
            return False
        transition(self.project, sl.id, "generating", reason=f"fix iteration after {stage} failure")
        return True

    # ---- fix requests (equivalence)
    def _fix_requests_text(self, sid: str, k: int) -> str:
        """Pending fix requests as relevant failures; remembers which ones this loop addressed (loop.json)."""
        fixes = self.fix_requests(sid)
        if not fixes:
            return ""
        loop = self.loop(sid)
        seen = loop.setdefault("fix_requests", {})
        ids = sorted(set(seen.get(str(k)) or []) | {r.eq_id for r in fixes})
        if ids != (seen.get(str(k)) or []):
            seen[str(k)] = ids
            self._save_loop(sid, loop)

        def j(v) -> str:
            return json.dumps(v, sort_keys=True, default=str)[:300]
        return ("Equivalence fix requests (the new service differs from legacy; make it match):\n"
                + "\n".join(f"- {r.eq_id} {r.case_id} {r.field} ({r.reason}): legacy {j(r.legacy)} | new {j(r.new)}"
                            + (f" | note: {r.note[:200]}" if r.note else "") for r in fixes[:30]))

    def _fix_requests_done(self, sid: str, k: int) -> None:
        from .equivalence import mark_fix_requests_done
        start = int(self.loop(sid).get("start", 1))
        seen = (self.loop(sid).get("fix_requests") or {})
        ids = sorted({e for kk, v in seen.items() if str(kk).isdigit() and int(kk) >= start for e in v or []})
        if ids:
            mark_fix_requests_done(self.project, sid, ids, note=f"addressed by iteration {k}")

    # ---- step: generate + apply + commit
    def _failures_for(self, sl, k: int) -> str:
        parts = []
        prev = self._read(sl.id, k - 1, "outcome.json") if k > 1 else None
        loop = self.loop(sl.id)
        if prev and k > int(loop.get("start", 1)):
            parts.append(f"Iteration {k - 1} failed at {prev.get('stage')}:\n{prev.get('failure', '')}")
        fx = self._fix_requests_text(sl.id, k)
        if fx:
            parts.append(fx)
        cms = (loop.get("comments") or {}).get(str(k)) or []
        if cms:
            parts.append("Human review comments:\n" + "\n".join(
                f"- {c.get('id')} {c.get('item_id') or ''} {c.get('file') or ''}"
                f"{(':' + str(c['line'])) if c.get('line') else ''}: {c.get('text', '')}" for c in cms))
        return "\n\n".join(parts)

    def build_pack(self, sl, k: int):
        from .codegen import DEFAULT_PACK_TOKENS, build_context_pack
        return build_context_pack(self.project, sl, graph=self.graph, contract=self.contract, spec=self.spec,
                                  risks=self._risks, resolutions=self._resolutions, dep_status=self.dep_status(sl),
                                  failures=self._failures_for(sl, k),
                                  max_tokens=self.opts.pack_tokens or DEFAULT_PACK_TOKENS)

    def step_generate(self, sl, k: int) -> tuple[bool, str, str]:
        from . import targetrepo
        from ..ingest.secrets import redact_string
        from .codegen import (CODEGEN_FIX_PROMPT, CODEGEN_PROMPT, CodegenOutput, EditRejected, answer_needs,
                              load_prompt, parse_output, prepare_changes, render_prompt, write_changes)
        from .state import atomic_write_json
        sid = sl.id
        rec = self._read(sid, k, "edits.json") or {"iteration": k, "status": "pending", "attempts": []}
        if rec["status"] == "applied":
            return True, "", "codegen"
        if rec["status"] == "rejected":
            return False, "Edits rejected:\n" + "\n".join(rec.get("errors") or []), "apply"
        root = self.lay.target_code
        if rec["status"] == "accepted":
            done = targetrepo.tag_commit(self.project, targetrepo.iter_tag(sid, k))
            if done:                                   # crashed after the commit, before edits.json was updated
                rec.update(status="applied", commit=done, changed=rec.get("planned") or [])
                self._write(sid, k, "edits.json", rec)
                self._mark_iteration_comments(sid, k)
                return True, "", "codegen"
        dirty = targetrepo.dirty_paths(self.project)
        if dirty and rec["status"] == "accepted":
            targetrepo.restore_paths(self.project, sorted(set(rec.get("planned") or []) & set(dirty)))
            dirty = targetrepo.dirty_paths(self.project)
        if dirty:
            raise GenerateError("target_code/ has uncommitted changes (" + ", ".join(dirty[:5]) +
                                "): commit them (human commits are counted) or remove them; they are never "
                                "overwritten")
        with self._span(sid, "codegen", k) as sp:
            pack = None
            while rec["status"] == "pending":
                pack = pack or self.build_pack(sl, k)
                if pack.sections.get("failures"):
                    version, system, template = load_prompt(CODEGEN_FIX_PROMPT)
                else:
                    version, system, template = load_prompt(CODEGEN_PROMPT)
                atts = rec["attempts"]
                needs = [a for a in atts if a.get("outcome") == "need"]
                rejected = [a for a in atts if a.get("outcome") == "rejected"]
                last = atts[-1] if atts else None
                need_answers = answer_needs(self.project, last["need"]) if last and last.get("outcome") == "need" \
                    else ""
                rejection = "\n".join(last.get("errors") or []) if last and last.get("outcome") == "rejected" else ""
                prompt = render_prompt(template, pack, slice_id=sid, iteration=k, node_ids=sl.node_ids,
                                       need_answers=need_answers, rejection=rejection)
                meta = self._read(sid, k, "prompt_meta.json") or {"iteration": k, "attempts": []}
                meta["attempts"].append({"attempt": len(atts) + 1, "template": Path(
                    CODEGEN_FIX_PROMPT if pack.sections.get("failures") else CODEGEN_PROMPT).name,
                    "prompt_version": version, "prompt_sha256": _sha(system + "\x00" + prompt),
                    "section_tokens": pack.tokens, "total_tokens": pack.total_tokens, "truncated": pack.truncated,
                    "legacy_files": pack.legacy_files, "need_answers": bool(need_answers),
                    "rejection": bool(rejection)})
                self._write(sid, k, "prompt_meta.json", meta)
                self.lay.ensure(self.lay.slice_context(sid))
                atomic_write_json(self.lay.slice_context(sid) / f"iter-{k}.json", pack)
                resp = self.llm.complete(task="codegen", system=system, prompt=prompt, json_mode=True,
                                         prompt_version=version)
                att = {"attempt": len(atts) + 1, "kind": "need" if need_answers else ("retry" if rejection else
                                                                                       "initial"),
                       "response": redact_string(resp.text[:RESPONSE_STORE_CHARS]), "model": resp.model,
                       "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "cost_usd": resp.cost_usd,
                       "cached": resp.cached}
                try:
                    output = parse_output(resp.text)
                    if output.need and not output.files:
                        if len(needs) >= MAX_NEED_ROUNDS:
                            raise EditRejected(["you already used your `need` round: return file edits now"])
                        att.update(outcome="need", need=list(output.need))
                    else:
                        if not output.files:
                            raise EditRejected(["reply contains no file changes"])
                        ch = prepare_changes(output, root, self.spec)
                        att.update(outcome="accepted", warnings=ch.warnings)
                        rec.update(status="accepted", output=output.model_dump(mode="json"), planned=ch.paths)
                except EditRejected as e:
                    att.update(outcome="rejected", errors=[redact_string(x) for x in e.errors[:30]])
                    if len(rejected) + 1 > MAX_APPLY_RETRIES:
                        rec.update(status="rejected", errors=att["errors"])
                rec["attempts"].append(att)
                self._write(sid, k, "edits.json", rec)
            sp["attempts"] = len(rec["attempts"])
            sp["outcome"] = rec["status"]
            if rec["status"] == "rejected":
                return False, "Edits rejected:\n" + "\n".join(rec.get("errors") or []), "apply"
            output = CodegenOutput.model_validate(rec["output"])
            try:
                ch = prepare_changes(output, root, self.spec)
            except EditRejected as e:
                rec.update(status="rejected", errors=e.errors)
                self._write(sid, k, "edits.json", rec)
                return False, "Edits rejected:\n" + "\n".join(e.errors), "apply"
            write_changes(root, ch)
            first = (output.notes or "generated").strip().splitlines()
            commit = targetrepo.commit_iteration(self.project, sid, k, redact_string(first[0] if first else "")[:200])
            rec.update(status="applied", commit=commit, changed=ch.paths, warnings=ch.warnings)
            self._write(sid, k, "edits.json", rec)
            self._mark_iteration_comments(sid, k)
            sp["commit"] = commit
        return True, "", "codegen"

    def _mark_iteration_comments(self, sid: str, k: int) -> None:
        cms = [c.get("id") for c in (self.loop(sid).get("comments") or {}).get(str(k)) or []]
        mark_comments_processed(self.project, [c for c in cms if c])

    # ---- step: build
    def _require_backend(self) -> None:
        ok, reason = self.backend.available()
        if not ok:
            raise _Stop(f"exec sandbox unavailable ({getattr(self.backend, 'name', '?')}: {reason}); builds and "
                        "tests never run on the host")

    def step_build(self, sl, k: int) -> tuple[bool, str]:
        from .ids import allocate
        from .models import ExecJob, Limits, Mount
        sid = sl.id
        prev = self._read(sid, k, "build.json")
        if prev and not prev.get("backend_error"):
            return bool(prev["ok"]), prev.get("failure", "")
        self._require_backend()
        tc = _toolchain(self.project, self.spec)
        if isinstance(tc, str):
            raise _Stop(f"cannot build: {tc}")
        image, cmd, network, _test, env, source = tc
        with self._span(sid, "build", k) as sp:
            run_id = allocate(self.project, "RUN")
            ws = self.lay.run_work(run_id) / "app"
            self.lay.ensure(ws.parent)
            _copy_workspace(self.lay.target_code, ws)
            job = ExecJob(run_id=run_id, image=image, command=cmd, phase="build", network=network,
                          workdir="/work/app", mounts=[Mount(source=self.lay.rel(ws), target="/work/app",
                                                             read_only=False)],
                          limits=Limits(timeout_s=1800), env=env, labels={"slice": sid, "stage": "build",
                                                                         "iteration": str(k)})
            res = self.backend.run(job)
            failure = ""
            if not res.ok:
                failure = (f"BUILD FAILED (run {run_id}, exit {res.exit_code}{', timed out' if res.timed_out else ''})"
                           f"\n<<<BUILD_LOG_BEGIN>>>\n{_clip(res.stderr_tail + chr(10) + res.stdout_tail)}"
                           "\n<<<BUILD_LOG_END>>>")
            rec = {"iteration": k, "run_id": run_id, "ok": res.ok, "exit_code": res.exit_code, "image": image,
                   "command": cmd, "toolchain": source, "workspace": self.lay.rel(ws), "failure": failure,
                   "backend_error": res.error if res.exit_code is None else ""}
            self._write(sid, k, "build.json", rec)
            sp.update(run_id=run_id, ok=res.ok, exit_code=res.exit_code)
        if rec["backend_error"]:
            raise _Stop(f"exec backend error during build ({run_id}): {rec['backend_error'][:300]}")
        return res.ok, failure

    # ---- step: unit tests + characterization suite
    def step_tests(self, sl, k: int) -> dict:
        from .ids import allocate
        from .models import ExecJob, Limits, Mount
        sid = sl.id
        prev = self._read(sid, k, "tests.json")
        if prev and prev.get("result") in ("passed", "failed"):
            return prev
        self._require_backend()
        build = self._read(sid, k, "build.json") or {}
        tc = _toolchain(self.project, self.spec)
        rec: dict = {"iteration": k, "unit": {}, "characterization": {}}
        with self._span(sid, "tests", k) as sp:
            if isinstance(tc, str) or tc[3] is None or not build.get("workspace"):
                rec["unit"] = {"status": "skipped", "reason": "no unit-test command for this build tool"}
            else:
                image, _b, _n, test_cmd, env, _src = tc
                run_id = allocate(self.project, "RUN")
                job = ExecJob(run_id=run_id, image=image, command=test_cmd, phase="test", network="none",
                              workdir="/work/app", mounts=[Mount(source=build["workspace"], target="/work/app",
                                                                 read_only=False)],
                              limits=Limits(timeout_s=1800), env=env,
                              labels={"slice": sid, "stage": "unit_tests", "iteration": str(k)})
                res = self.backend.run(job)
                if res.exit_code is None:
                    raise _Stop(f"exec backend error during unit tests ({run_id}): {res.error[:300]}")
                rec["unit"] = {"status": "passed" if res.ok else "failed", "run_id": run_id,
                               "exit_code": res.exit_code}
                if not res.ok:
                    rec["unit"]["failure"] = (f"UNIT TESTS FAILED (run {run_id}, exit {res.exit_code})\n"
                                              f"<<<TEST_LOG_BEGIN>>>\n{_clip(res.stdout_tail + chr(10) + res.stderr_tail)}"
                                              "\n<<<TEST_LOG_END>>>")
            if rec["unit"].get("status") == "failed":
                rec.update(result="failed", failure=rec["unit"]["failure"])
                rec["characterization"] = {"status": "not_run", "reason": "unit tests failed"}
            else:
                suite = self.suite_runner(self.project, sl, self.cases(sid), backend=self.backend)
                rec["characterization"] = suite.model_dump(mode="json")
                if suite.status == "skipped":
                    rec.update(result="skipped", reason=f"characterization suite skipped: {suite.reason}")
                elif suite.status == "failed":
                    lines = []
                    for f in suite.failures[:20]:
                        d = "; ".join(f"{x.get('field')}: expected {json.dumps(x.get('expected'))[:200]} got "
                                      f"{json.dumps(x.get('actual'))[:200]}" for x in f.get("diffs") or [])
                        lines.append(f"- {f.get('case_id')} {f.get('outcome')}: {f.get('error') or d}")
                    rec.update(result="failed", failure="CHARACTERIZATION CASES FAILED (legacy behaviour differs):\n"
                               "<<<TEST_LOG_BEGIN>>>\n" + "\n".join(lines) + "\n<<<TEST_LOG_END>>>")
                else:
                    rec.update(result="passed")
            # readable by migrate/approvals.py (tolerant `failed` / `ok` / `status` reader)
            rec["status"], rec["ok"] = rec["result"], rec["result"] == "passed"
            if rec["characterization"].get("status") in ("passed", "failed"):
                rec["failed"] = [f.get("case_id") for f in rec["characterization"].get("failures") or []]
            self._write(sid, k, "tests.json", rec)
            sp.update(result=rec["result"], unit=rec["unit"].get("status"),
                      characterization=rec["characterization"].get("status"))
        return rec

    # ---- step: review
    def declared_hosts(self) -> set[str]:
        try:
            from . import harness
            hs = harness.load_spec(self.project)
            return {s.name for s in hs.stubs}
        except Exception:  # noqa: BLE001
            return set()

    def step_review(self, sl, k: int):
        from . import targetrepo
        from .codegen import render_spec
        from .reviewer import ReviewResult, review
        from ..sandbox.tools import ReadOnlyTools
        from .state import atomic_write_json
        sid = sl.id
        prev = self._read(sid, k, "review.json")
        if prev:
            return ReviewResult.model_validate(prev)
        with self._span(sid, "review", k) as sp:
            base = self.loop(sid).get("base_commit") or ""
            diff = targetrepo.diff(self.project, base, "HEAD")
            spec_text = render_spec(sl, self.graph, self.contract, tools=ReadOnlyTools(Path(self.project.code)) if Path(self.project.code).is_dir() else None, risks=self._risks,
                                    resolutions=self._resolutions, dep_status=self.dep_status(sl))
            known = set(sl.node_ids) | {e.target for e in self.graph.edges if e.source in set(sl.node_ids)} | \
                {e.source for e in self.graph.edges if e.target in set(sl.node_ids)}
            rv = review(self.llm, slice_id=sid, iteration=k, spec_text=spec_text, diff=diff, known_ids=known,
                        declared_hosts=self.declared_hosts())
            self._write(sid, k, "review.json", rv.model_dump(mode="json"))
            atomic_write_json(self.lay.slice_review(sid), rv)
            sp.update(findings=len(rv.findings), blocking=len(rv.blocking), dropped=rv.dropped_unverified)
        return rv


# ------------------------------------------------------------------ public API
def _select(plan, slice_id: str | None, all_slices: bool) -> list[str]:
    if bool(slice_id) == bool(all_slices):
        raise GenerateError("give exactly one of --slice SLICE-NNNN or --all")
    if slice_id:
        if slice_id not in {s.id for s in plan.slices}:
            raise GenerateError(f"unknown slice {slice_id}")
        return [slice_id]
    return [s.id for s in sorted(plan.slices, key=lambda s: s.order)]


def _codemods(project, spec, backend) -> list[dict]:
    """Run codemods once for in_place version upgrades (migration/codemods.json remembers the result)."""
    from .codemods import run_codemods
    from .paths import Phase2Layout
    from .state import atomic_write_json, read_json
    if spec.strategy != "in_place" or spec.kind != "version_upgrade":
        return []
    lay = Phase2Layout(project)
    path = lay.migration / "codemods.json"
    done = read_json(path, default={}) or {}
    if any(v.get("status") in ("applied", "no_changes") for v in done.values()):
        return []
    results = run_codemods(project, spec, backend=backend)
    for r in results:
        done[r.name] = r.model_dump(mode="json")
    if results:
        lay.ensure(lay.migration)
        atomic_write_json(path, done)
    return [r.model_dump(mode="json") for r in results]


def generate(project, *, slice_id: str | None = None, all_slices: bool = False, llm, backend=None,
             suite_runner: SuiteRunner | None = None, options: GenerateOptions | None = None,
             audit: AuditLog | None = None) -> GenerateReport:
    """Run the generation loop for one slice or all slices (plan order). See module docstring."""
    from . import targetrepo
    runner = SliceRunner(project, llm=llm, backend=backend, suite_runner=suite_runner, options=options, audit=audit)
    ids = _select(runner.plan, slice_id, all_slices)
    try:
        info = targetrepo.init_repo(project, runner.spec)
    except targetrepo.RepoError as e:
        raise GenerateError(str(e)) from None
    warnings = [info["warning"]] if info.get("warning") else []
    mods = _codemods(project, runner.spec, runner.backend)
    for m in mods:
        if m["status"] in ("skipped", "failed"):
            warnings.append(f"codemod {m['name']}: {m['status']}: {m['reason']}")
    rep = runner.run(ids)
    rep.codemods = mods
    rep.warnings = warnings + rep.warnings
    return rep


def dry_run(project, *, slice_id: str | None = None, all_slices: bool = False, config=None,
            options: GenerateOptions | None = None) -> list[SliceOutcome]:
    """Per-slice estimates. Reads only: no contract-check audit record, no folders, no state changes."""
    from ..llm.litellm_client import estimate_cost
    from .casestore import contract_graph, load_plan
    from .codegen import DEFAULT_PACK_TOKENS, build_context_pack
    from .contract import load_contract
    from .state import load_state
    from .target import load_target
    opts = options or GenerateOptions()
    contract = load_contract(project)
    plan = load_plan(project)
    if plan is None:
        raise GenerateError("no valid plan/migration_plan.json: run `migration-agent plan PROJECT` first")
    graph = contract_graph(project, contract)
    try:
        spec = load_target(project)
    except Exception:  # noqa: BLE001
        spec = None
    by_id = {s.id: s for s in plan.slices}
    out = []
    for sid in _select(plan, slice_id, all_slices):
        sl = by_id[sid]
        st = load_state(project, sid)
        deps = {d: load_state(project, d).status for d in sl.depends_on}
        pack = build_context_pack(project, sl, graph=graph, contract=contract, spec=spec, dep_status=deps,
                                  max_tokens=opts.pack_tokens or DEFAULT_PACK_TOKENS)
        tin = pack.total_tokens + 2_000
        tout = max(500, pack.tokens.get("legacy", 0))
        est = round(estimate_cost([("codegen", tin, tout), ("code_review", 2_000 + tout, 1_000)], config), 4)
        reason = ""
        if st.status == "blocked":
            reason = f"blocked: {st.reason}"
        elif sl.blockers:
            reason = f"blockers: {', '.join(sl.blockers)}"
        elif any(s != "approved" for s in deps.values()):
            reason = "waits for: " + ", ".join(d for d, s in deps.items() if s != "approved")
        cap = opts.max_usd_per_slice if opts.max_usd_per_slice is not None else (
            round(SLICE_CAP_FACTOR * sl.estimate_usd, 6) if sl.estimate_usd > 0 else None)
        out.append(SliceOutcome(slice_id=sid, action="dry_run", status=st.status, reason=reason,
                                estimate_usd=max(est, sl.estimate_usd), context_tokens=pack.total_tokens,
                                cap_usd=cap))
    return out


# ------------------------------------------------------------------ CLI
def _make_llm(project, offline: bool, config_path, max_usd, audit: TaggedAudit):
    from .codegen import OfflineCodegenLLM
    if offline:
        return OfflineCodegenLLM()
    import os
    from ..cli import _budget, _load_cfg
    from ..llm.litellm_client import LiteLLMClient
    from .ledger import budget_for, project_budget_usd
    if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                                           "OPENAI_API_KEY")):
        raise click.ClickException("No LLM API key found (see .env.example), or run with --offline.")
    cfg = _load_cfg(config_path)
    try:
        budget = budget_for(project, project_budget_usd(project), max_usd=_budget(max_usd))
    except BudgetExceeded as e:
        raise click.ClickException(str(e))
    return LiteLLMClient(cfg, budget=budget, cache_dir=Path(project.root) / "llm_cache", audit=audit,
                         agent="codegen")


def register(groups) -> None:
    @groups.main.command("generate")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--slice", "slice_id", default=None, help="One slice (SLICE-NNNN).")
    @click.option("--all", "all_slices", is_flag=True, help="Every slice in plan order (only ready ones run).")
    @click.option("--dry-run", is_flag=True, help="Per-slice token and USD estimate; writes nothing.")
    @click.option("--from-comments", is_flag=True, help="Reopen slices with unprocessed migration/comments.json.")
    @click.option("--offline", is_flag=True, help="Deterministic placeholder edits; no model, no network.")
    @click.option("--max-usd", type=float, default=None, help="Cap for this run (also capped by budget_usd).")
    @click.option("--max-usd-per-slice", type=float, default=None, help="Default: 3x the slice estimate.")
    @click.option("--max-iterations", type=click.IntRange(1, 20), default=DEFAULT_MAX_ITERATIONS, show_default=True)
    @click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None,
                  help="LLM config.json")
    @click.option("--pin-contract", "pin", is_flag=True, help="Accept a contract pinned to an older graph version.")
    @click.option("--accept-unmapped", is_flag=True)
    def generate_cmd(project: Path, slice_id, all_slices, dry_run, from_comments, offline, max_usd,
                     max_usd_per_slice, max_iterations, config_path, pin, accept_unmapped) -> None:
        """Generate (and fix) the code of slices in the sandboxed loop."""
        from .cli import _project
        p = _project(project)
        opts = GenerateOptions(max_iterations=max_iterations, max_usd=max_usd, max_usd_per_slice=max_usd_per_slice,
                               from_comments=from_comments, pin=pin, accept_unmapped=accept_unmapped)
        try:
            if dry_run:
                from .planner import _llm_config
                rows = globals()["dry_run"](p, slice_id=slice_id, all_slices=all_slices,
                                            config=_llm_config(config_path), options=opts)
                total = 0.0
                for r in rows:
                    total += r.estimate_usd
                    click.echo(f"{r.slice_id}  {r.status:<14} ~{r.context_tokens:>6} ctx tokens  "
                               f"~${r.estimate_usd:.4f}/iteration  cap {('$%.4f' % r.cap_usd) if r.cap_usd else '-'}"
                               + (f"  ({r.reason})" if r.reason else ""))
                click.echo(f"total ~${total:.4f} per iteration (x up to {max_iterations} iterations); nothing written")
                return
            audit = TaggedAudit(Path(p.audit_dir) / "audit.jsonl")
            llm = _make_llm(p, offline, config_path, max_usd, audit)
            rep = generate(p, slice_id=slice_id, all_slices=all_slices, llm=llm, options=opts, audit=audit)
        except (GenerateError, BudgetExceeded) as e:
            raise click.ClickException(str(e))
        for w in rep.warnings:
            click.echo(f"warning: {w}")
        for m in rep.codemods:
            click.echo(f"codemod {m['name']}: {m['status']}" + (f" ({m['reason'][:200]})" if m["reason"] else ""))
        for o in rep.outcomes:
            click.echo(f"{o.slice_id}: {o.action} -> {o.status} (iterations {o.iterations}, model calls "
                       f"{o.model_calls}, ${o.spent_usd:.4f})" + (f": {o.reason}" if o.reason else ""))
        if rep.stopped:
            click.echo(f"stopped: {rep.stopped}")
        if slice_id and rep.outcomes and rep.outcomes[0].status != "awaiting_human":
            raise SystemExit(1)
