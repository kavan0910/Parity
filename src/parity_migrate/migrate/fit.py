"""M2 target-fit analysis: `target fit`, `target resolve`, and the planner gate (docs/phase2-plan.md section 2).

Inputs (read-only): the approved contract (entry gate), the approved graph snapshot it names, the approved doc,
facts/manifests.json, target/target.yaml and code/ (the redacted copy, through sandbox.tools.ReadOnlyTools;
legacy_src/ is never read). Outputs: target/fit_report.json (models.FitReport), target/fit_report.md and, for
human decisions, target/fit_resolutions.json (FitResolutionsFile below).

Layer 1, deterministic (no model): the libmap tables in migrate/knowledge/libmap/*.yaml that apply to the
TargetSpec are matched against external_dependency nodes + manifest dependencies (globs), code lines (regexes
via ReadOnlyTools.grep) and config node names. One Risk per table entry with every matching file:line as
evidence; unknown dependencies become `unmapped_library` risks; `libraries.deny` hits are high severity.

Layer 2, model (task `target_fit`, prompt migrate/prompts/target_fit.md, PROMPT_VERSION): batches of graph
items with the code they cite. Output is strict JSON validated per risk by pydantic; a risk is kept only if
every item_id exists in the approved graph and every evidence entry is a line-pinned verbatim quote that
analyze.evidence.check_evidence verifies against code/ in a file the batch showed. Risks failing any check,
or whose text carries prompt-injection markers, are dropped and counted (FitReport.dropped_unverified).

Public API:
    run_fit(project, llm, *, pin=False, accept_unmapped=False) -> FitRun
    deterministic_risks(project, spec, graph) -> (list[RiskDraft], dropped)
    model_risks(project, spec, graph, llm, doc_text="") -> ModelLayerResult
    resolve_risk(project, risk_id, status, note, by="user") -> FitResolution
    fit_blockers(project, *, allow_stale=False) -> list[Risk]     # open high-severity risks (planner gate)
    fit_state(project) -> dict                                     # for the review UI (never raises)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..graph.models import Evidence, Graph
from .models import FitReport, Risk, RiskId, TargetSpec

PROMPT_VERSION = "target_fit.v1"
TASK = "target_fit"
PROMPT_FILE = Path(__file__).parent / "prompts" / "target_fit.md"
MODEL_NODE_TYPES = ("component", "business_rule", "data_entity", "endpoint", "job", "integration")
BATCH_CHARS = 24_000          # code characters per model call
DOC_CHARS = 6_000             # approved-doc excerpt per model call
MAX_QUOTE = 240
NON_RUNTIME_SCOPES = {"dev", "test", "testImplementation", "testRuntimeOnly", "provided", "optional"}
RESOLUTION_STATUSES = ("accepted", "mitigate", "wont_fix")
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_KIND_MAP = {"library_replacement": "library_replacement", "removed_api": "no_equivalent",
             "semantic_shift": "semantic_shift", "config_rename": "library_replacement"}
# text that should never appear in a risk a model wrote about code (forged delimiters, injection phrasing)
_SUSPICIOUS = re.compile(
    r"<<<|>>>|\b(ignore|disregard|forget)\b.{0,40}\b(instruction|prompt|rule)s?\b|\bsystem prompt\b|"
    r"\byou are now\b|\bnew instructions?\b|\bas an ai\b", re.I | re.S)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FitError(RuntimeError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ own models (not in migrate/models.py)
class FitResolution(_Strict):
    risk_id: RiskId
    status: Literal["accepted", "mitigate", "wont_fix"]
    note: str = Field(min_length=1)
    by: str = "user"
    created: str = ""
    risk_title: str = ""


class FitResolutionsFile(_Strict):
    """target/fit_resolutions.json: append-only history; the last entry per risk is current."""
    schema_version: Literal[1] = 1
    resolutions: list[FitResolution] = Field(default_factory=list)

    def current(self) -> dict[str, FitResolution]:
        out: dict[str, FitResolution] = {}
        for r in self.resolutions:
            out[r.risk_id] = r
        return out


class ModelEvidence(_Strict):
    file: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1)
    line_end: int | None = Field(default=None, ge=1)
    quote: str = Field(min_length=1, max_length=2000)


class ModelRisk(_Strict):
    """One risk as the target_fit model must return it (validated per risk)."""
    kind: Literal["no_equivalent", "semantic_shift", "library_replacement", "infra_gap", "unknown"]
    severity: Literal["low", "medium", "high"] = "medium"
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    item_ids: list[str] = Field(min_length=1, max_length=50)
    evidence: list[ModelEvidence] = Field(min_length=1, max_length=20)
    proposed_handling: str = Field(default="", max_length=1000)
    needs_decision: bool = False


@dataclass
class RiskDraft:
    key: str
    kind: str
    severity: str
    title: str
    detail: str = ""
    item_ids: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    proposed_handling: str = ""
    needs_decision: bool = False
    source: str = "deterministic"


@dataclass
class ModelLayerResult:
    drafts: list[RiskDraft] = field(default_factory=list)
    dropped: int = 0
    dropped_reasons: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    invalid_outputs: int = 0
    batches: int = 0
    budget_stopped: bool = False

    def drop(self, reason: str) -> None:
        self.dropped += 1
        self.dropped_reasons[reason] = self.dropped_reasons.get(reason, 0) + 1


@dataclass
class FitRun:
    report: FitReport
    json_path: Path
    md_path: Path
    deterministic: int = 0
    model: ModelLayerResult = field(default_factory=ModelLayerResult)
    offline: bool = False


class OfflineFitLLM:
    """Deterministic, no-network stand-in for `target fit --offline`: the model layer runs (batching, prompt,
    validation) but the 'model' never reports a risk. Only layer 1 produces findings offline."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, *, task, system, prompt, json_mode=True, prompt_version="v1"):
        from ..llm.base import LLMResponse
        self.calls.append({"task": task, "prompt_version": prompt_version})
        text = json.dumps({"risks": []})
        return LLMResponse(text=text, model="offline", tokens_in=len(prompt) // 4, tokens_out=len(text) // 4)


# ------------------------------------------------------------------ helpers
def _tools(project):
    from ..sandbox.tools import ReadOnlyTools
    code = Path(project.code)
    if not code.is_dir():
        raise FitError(f"no code/ folder in {project.root}")
    return ReadOnlyTools(code)


def _quote(text: str) -> str:
    return text.strip()[:MAX_QUOTE].strip()


def _clean(text: str, limit: int) -> str:
    from ..analyze.module_analyzer import neutralize
    return _CTRL.sub("", neutralize(str(text)))[:limit].strip()


def _verified(evs: list[Evidence], code_dir: Path) -> tuple[list[Evidence], int]:
    from ..analyze.evidence import check_evidence
    ok = [e for e in evs if check_evidence(e, code_dir) is None]
    return ok, len(evs) - len(ok)


def _dedupe_evidence(evs: list[Evidence]) -> list[Evidence]:
    seen, out = set(), []
    for e in sorted(evs, key=lambda e: (e.file, e.line_start or 0, e.quote or "")):
        k = (e.file, e.line_start, e.line_end)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


class _GraphIndex:
    def __init__(self, graph: Graph):
        self.graph = graph
        self.ids = {n.id for n in graph.nodes}
        self.by_file: dict[str, list] = {}
        for n in sorted(graph.nodes, key=lambda n: n.id):
            for f in {e.file for e in n.evidence}:
                self.by_file.setdefault(f, []).append(n)
        self.ext = [n for n in graph.nodes if n.type == "external_dependency"]
        self.config = [n for n in graph.nodes if n.type == "config"]

    def items_for_file(self, path: str) -> list[str]:
        nodes = self.by_file.get(path, [])
        comps = [n.id for n in nodes if n.type == "component"]
        return comps or [n.id for n in nodes]

    def ext_ids(self, coord: str) -> list[str]:
        c = coord.strip().lower()
        return sorted(n.id for n in self.ext if n.name.strip().lower() == c)


def _dependencies(project, idx: _GraphIndex) -> list[tuple[str, str, str]]:
    """(coordinate, manifest path, scope) from facts/manifests.json plus external_dependency nodes."""
    out: dict[str, tuple[str, str, str]] = {}
    p = Path(project.facts) / "manifests.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            data = {}
        for m in (data.get("manifests") or []) if isinstance(data, dict) else []:
            if not isinstance(m, dict) or m.get("parse_error"):
                continue
            for d in m.get("dependencies") or []:
                name = str(d.get("name") or "").strip()
                if name and name.lower() not in out:
                    out[name.lower()] = (name, str(m.get("path") or ""), str(d.get("scope") or ""))
    for n in idx.ext:
        if n.name.strip().lower() not in out:
            out[n.name.strip().lower()] = (n.name.strip(), n.module or (n.evidence[0].file if n.evidence else ""), "")
    return [out[k] for k in sorted(out)]


def _manifest_evidence(tools, path: str, coord: str) -> Evidence | None:
    if not path:
        return None
    try:
        lines = tools.read_file(path).content.splitlines()
    except Exception:  # noqa: BLE001 - manifest withheld/unreadable: no evidence
        return None
    artifact = coord.split(":", 1)[1] if ":" in coord else coord
    for token in (f"<artifactId>{artifact}</artifactId>", f'"{coord}"', coord, artifact):
        for i, ln in enumerate(lines, 1):
            j = ln.lower().find(token.lower())
            if j >= 0:
                return Evidence(file=path, line_start=i, line_end=i, quote=ln[j:j + len(token)])
    return Evidence(file=path)


def _grep(tools, rx: str):
    from ..sandbox.tools import SandboxViolation
    try:
        return tools.grep(rx)
    except SandboxViolation:
        return None


# ------------------------------------------------------------------ layer 1: deterministic
def deterministic_risks(project, spec: TargetSpec, graph: Graph) -> tuple[list[RiskDraft], int]:
    """Libmap matching over dependencies, code and config. Returns (drafts with verified evidence, dropped)."""
    from .knowledge import dep_glob_match, libmaps_for, split_framework
    tools = _tools(project)
    code_dir = tools.root
    idx = _GraphIndex(graph)
    deps = _dependencies(project, idx)
    maps = libmaps_for(spec)
    drafts: list[RiskDraft] = []
    dropped = 0
    matched: set[str] = set()

    for lm in maps:
        for ent in lm.entries:
            mt = ent.match
            if any((g := _grep(tools, rx)) is not None and g.matches for rx in mt.unless):
                continue
            evs: list[Evidence] = []
            items: set[str] = set()
            truncated = False
            dep_hits: list[str] = []
            for coord, mpath, _scope in deps:
                if mt.dependency_matches(coord):
                    matched.add(coord.lower())
                    dep_hits.append(coord)
                    items.update(idx.ext_ids(coord))
                    ev = _manifest_evidence(tools, mpath, coord)
                    if ev is not None:
                        evs.append(ev)
            files: set[str] = set()
            for rx in mt.code:
                res = _grep(tools, rx)
                if res is None:
                    continue
                truncated |= res.truncated
                for m in res.matches:
                    if not mt.file_ok(m.path):
                        continue
                    evs.append(Evidence(file=m.path, line_start=m.line, line_end=m.line, quote=_quote(m.text)))
                    files.add(m.path)
                    items.update(idx.items_for_file(m.path))
            for rx in mt.config_keys:
                for n in idx.config:
                    if re.search(rx, n.name):
                        items.add(n.id)
                        for e in n.evidence:
                            evs.append(_with_quote(tools, e))
            if not evs and not items:
                continue
            raw = _dedupe_evidence(evs)
            ok, bad = _verified(raw, code_dir)
            if not ok:
                dropped += 1
                continue
            occ = sum(1 for e in ok if e.line_start is not None)
            where = f"{occ} occurrence(s) in {len({e.file for e in ok})} file(s)"
            if dep_hits:
                where += f"; dependencies: {', '.join(dep_hits)}"
            if truncated:
                where += "; grep result truncated, more occurrences may exist"
            if bad:
                where += f"; {bad} unverifiable location(s) dropped"
            detail = (f"{ent.note} Replacement: {ent.replacement or 'none'}. Source: {ent.source} "
                      f"(libmap {lm.id} v{lm.version} / {ent.id}; {where}).")
            title = ent.title if ent.kind != "config_rename" else f"Config rename: {ent.title}"
            drafts.append(RiskDraft(key=f"libmap:{lm.id}/{ent.id}", kind=_KIND_MAP[ent.kind], severity=ent.severity,
                                    title=title, detail=detail, item_ids=sorted(items), evidence=ok,
                                    proposed_handling=ent.replacement, needs_decision=bool(ent.needs_decision)))

    # unmapped and denied dependencies
    unmapped_sev, unmapped_dec = {"language_move": ("medium", True), "framework_move": ("medium", False)}.get(
        spec.kind, ("low", False))
    for coord, mpath, scope in deps:
        ev = _manifest_evidence(tools, mpath, coord)
        evs = [ev] if ev is not None else []
        ok, _ = _verified(evs, code_dir)
        items = idx.ext_ids(coord)
        if any(dep_glob_match(g, coord) for g in spec.libraries.deny):
            drafts.append(RiskDraft(key=f"deny:{coord.lower()}", kind="library_replacement", severity="high",
                                    title=f"Denied library in use: {coord}",
                                    detail=f"{coord} matches target.yaml libraries.deny; it must be replaced.",
                                    item_ids=items, evidence=ok, proposed_handling="replace or remove",
                                    needs_decision=True))
        if coord.lower() in matched or scope in NON_RUNTIME_SCOPES or any(m.is_known(coord) for m in maps):
            continue
        if maps or spec.kind != "version_upgrade":
            drafts.append(RiskDraft(
                key=f"unmapped:{coord.lower()}", kind="unmapped_library", severity=unmapped_sev,
                title=f"No mapping for dependency {coord}",
                detail=(f"{coord} (from {mpath or 'the graph'}) is not covered by the libmap tables for this "
                        f"migration ({', '.join(m.id for m in maps) or 'none apply'}); confirm it works on the "
                        "target or choose a replacement."),
                item_ids=items, evidence=ok, needs_decision=unmapped_dec))
    if not maps:
        src = f"{spec.source.language} {spec.source.framework}".strip()
        tgt = f"{spec.target.language} {spec.target.framework or split_framework(spec.source.framework)[0]}".strip()
        drafts.append(RiskDraft(key=f"nomap:{src}->{tgt}", kind="unknown", severity="medium",
                                title=f"No knowledge table for {src} -> {tgt}",
                                detail="No libmap table applies to this source/target pair, so only the model "
                                       "layer and unmapped-dependency checks ran. Review dependencies manually.",
                                needs_decision=True))
    bi = spec.target.base_image
    if not bi or "@sha256:" not in bi:
        drafts.append(RiskDraft(key="infra:base_image", kind="infra_gap", severity="low",
                                title="Target base image not pinned by digest" if bi else "No target base image",
                                detail="Set target.base_image to name@sha256:<digest> so builds are reproducible "
                                       "(plan section 2, section 6).",
                                proposed_handling="pin the base image digest in target.yaml"))
    return drafts, dropped


def _with_quote(tools, e: Evidence) -> Evidence:
    """Fill a verbatim quote for a line-pinned graph evidence entry (graph facts carry file:line only)."""
    if e.quote or e.line_start is None:
        return e
    try:
        txt = tools.read_file(e.file, e.line_start, e.line_end or e.line_start).content
    except Exception:  # noqa: BLE001
        return e
    q = _quote(txt.splitlines()[0]) if txt.strip() else ""
    return e.model_copy(update={"quote": q or None})


# ------------------------------------------------------------------ layer 2: model
def load_prompt() -> tuple[str, str, str]:
    """(version, system, user template) from migrate/prompts/target_fit.md."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    m = re.match(r"<!-- prompt: (\S+)", text)
    version = m.group(1) if m else ""
    _, _, rest = text.partition("<!-- SYSTEM -->")
    system, _, user = rest.partition("<!-- USER -->")
    return version, system.strip() + "\n", user.strip() + "\n"


def _batches(project, idx: _GraphIndex, tools) -> list[tuple[list[str], list]]:
    """[(files, nodes)] grouping graph items by the file their first evidence cites; <= BATCH_CHARS of code."""
    by_file: dict[str, list] = {}
    for n in sorted(idx.graph.nodes, key=lambda n: n.id):
        if n.type in MODEL_NODE_TYPES and n.evidence:
            by_file.setdefault(n.evidence[0].file, []).append(n)
    out: list[tuple[list[str], list]] = []
    cur_files, cur_nodes, size = [], [], 0
    for f in sorted(by_file):
        try:
            st = tools.file_stat(f)
        except Exception:  # noqa: BLE001 - cited file missing from code/: skip
            continue
        if st.kind != "file" or st.is_binary:
            continue
        if cur_files and size + st.size > BATCH_CHARS:
            out.append((cur_files, cur_nodes))
            cur_files, cur_nodes, size = [], [], 0
        cur_files.append(f)
        cur_nodes.extend(by_file[f])
        size += st.size
    if cur_files:
        out.append((cur_files, cur_nodes))
    return out


def _render_code(tools, files: list[str]) -> str:
    parts, size = [], 0
    for f in files:
        try:
            r = tools.read_file(f)
        except Exception:  # noqa: BLE001
            continue
        lines = r.content.splitlines()
        parts.append(f"=== {f} (lines 1-{len(lines)}) ===")
        for i, ln in enumerate(lines, 1):
            row = f"{i:>5}| {ln}"
            size += len(row) + 1
            if size > BATCH_CHARS * 1.5:
                parts.append("  ...| [truncated]")
                return "\n".join(parts)
            parts.append(row)
    return "\n".join(parts)


def _render_graph(nodes) -> str:
    rows = []
    for n in nodes:
        locs = ", ".join(f"{e.file}:{e.line_start}" + (f"-{e.line_end}" if e.line_end and e.line_end != e.line_start
                                                       else "") if e.line_start else e.file for e in n.evidence[:3])
        rows.append(f"{n.id} | {n.type} | {n.name[:120]} | {locs}")
    return "\n".join(rows)


def _doc_excerpt(doc_text: str, ids: set[str]) -> str:
    if not doc_text:
        return "(no approved document text)"
    paras = [p for p in re.split(r"\n\s*\n", doc_text) if any(i in p for i in ids)]
    text = "\n\n".join(paras) if paras else doc_text
    return text[:DOC_CHARS]


def _migration_line(spec: TargetSpec) -> str:
    s, t = spec.source, spec.target
    return (f"{spec.kind}: {s.language} {s.runtime} {s.framework} -> {t.language} {t.runtime} {t.framework}"
            .replace("  ", " ").strip())


def _check_model_risk(raw, idx: _GraphIndex, files: set[str], code_dir: Path, res: ModelLayerResult
                      ) -> RiskDraft | None:
    from ..analyze.evidence import check_evidence
    try:
        r = ModelRisk.model_validate(raw)
    except ValidationError:
        res.drop("schema")
        return None
    if any(i not in idx.ids for i in r.item_ids):
        res.drop("unknown_item_id")
        return None
    evs: list[Evidence] = []
    for e in r.evidence:
        ev = Evidence(file=e.file, line_start=e.line_start, line_end=e.line_end, quote=e.quote)
        if e.file not in files or check_evidence(ev, code_dir) is not None:
            res.drop("unverified_evidence")
            return None
        evs.append(ev)
    texts = (r.title, r.detail, r.proposed_handling)
    if any(_SUSPICIOUS.search(t) for t in texts):
        res.drop("suspicious_text")
        return None
    first = evs[0]
    key = f"model:{r.kind}:{','.join(sorted(set(r.item_ids)))}:{first.file}:{first.line_start}"
    return RiskDraft(key=key, kind=r.kind, severity=r.severity, title=_clean(r.title, 200),
                     detail=_clean(r.detail, 2000), item_ids=sorted(set(r.item_ids)), evidence=_dedupe_evidence(evs),
                     proposed_handling=_clean(r.proposed_handling, 1000), needs_decision=r.needs_decision,
                     source="model")


def model_risks(project, spec: TargetSpec, graph: Graph, llm, doc_text: str = "",
                log=None) -> ModelLayerResult:
    """Layer 2: one `target_fit` call per batch; only verified risks survive (see module docstring)."""
    from ..analyze.module_analyzer import render
    from ..llm.base import BudgetExceeded, LLMAuthError, LLMError, parse_json
    version, system, user_tpl = load_prompt()
    if version != PROMPT_VERSION:
        raise FitError(f"prompt file version {version!r} does not match PROMPT_VERSION {PROMPT_VERSION!r}")
    tools = _tools(project)
    idx = _GraphIndex(graph)
    res = ModelLayerResult()
    batches = _batches(project, idx, tools)
    res.batches = len(batches)
    for bi, (files, nodes) in enumerate(batches, 1):
        batch_id = f"fit-{bi:03d}"
        prompt = render(user_tpl, MIGRATION=_migration_line(spec), BATCH_ID=batch_id,
                        DOC=_doc_excerpt(doc_text, {n.id for n in nodes}), GRAPH=_render_graph(nodes),
                        CODE=_render_code(tools, files))
        try:
            resp = llm.complete(task=TASK, system=system, prompt=prompt, json_mode=True,
                                prompt_version=PROMPT_VERSION)
        except BudgetExceeded as e:
            res.budget_stopped = True
            if log:
                log(f"budget reached after {res.calls} model call(s): {e}")
            break
        except LLMAuthError:
            raise
        except LLMError as e:
            res.invalid_outputs += 1
            if log:
                log(f"{batch_id}: model call failed: {type(e).__name__}")
            continue
        res.calls += 1
        try:
            data = parse_json(resp.text)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("risks"), list):
            res.invalid_outputs += 1
            continue
        fileset = set(files)
        for raw in data["risks"]:
            d = _check_model_risk(raw, idx, fileset, tools.root, res)
            if d is not None:
                res.drafts.append(d)
    return res


# ------------------------------------------------------------------ orchestration
def _approved_graph(project, contract) -> Graph:
    from ..graph.store import parse_graph
    p = Path(project.root) / contract.graph_snapshot
    return parse_graph(p.read_text(encoding="utf-8"))


def _approved_doc(project, contract) -> str:
    from .contract import approved_doc_body
    try:
        return approved_doc_body((Path(project.root) / contract.approved_doc).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""


def _merge(drafts: list[RiskDraft]) -> list[RiskDraft]:
    out: dict[str, RiskDraft] = {}
    for d in drafts:
        if d.key in out:
            o = out[d.key]
            o.evidence = _dedupe_evidence(o.evidence + d.evidence)
            o.item_ids = sorted(set(o.item_ids) | set(d.item_ids))
        else:
            out[d.key] = d
    return list(out.values())


def run_fit(project, llm=None, *, pin: bool = False, accept_unmapped: bool = False, log=None,
            offline: bool = False) -> FitRun:
    """Gate, both layers, RISK id allocation, write fit_report.json|md. `llm=None` skips the model layer."""
    from ..audit import AuditLog
    from .contract import require_contract
    from .ids import allocator
    from .paths import Phase2Layout
    from .state import atomic_write_json, atomic_write_text, now_iso, sha256_file
    from .target import load_target

    contract = require_contract(project, pin=pin, accept_unmapped=accept_unmapped)
    spec = load_target(project)
    lay = Phase2Layout(project)
    graph = _approved_graph(project, contract)
    audit = AuditLog(Path(project.audit_dir) / "audit.jsonl")
    with audit.span("target_fit", "fit_run", offline=offline, prompt_version=PROMPT_VERSION,
                    graph_version=contract.graph_version) as span:
        det, det_dropped = deterministic_risks(project, spec, graph)
        mres = ModelLayerResult()
        if llm is not None:
            mres = model_risks(project, spec, graph, llm, _approved_doc(project, contract), log=log)
        drafts = _merge(det + mres.drafts)
        ids = allocator(project).allocate_many("RISK", [d.key for d in drafts]) if drafts else {}
        risks = [Risk(id=ids[d.key], kind=d.kind, severity=d.severity, title=d.title, detail=d.detail,
                      item_ids=d.item_ids, evidence=d.evidence, proposed_handling=d.proposed_handling,
                      needs_decision=d.needs_decision, source=d.source) for d in drafts]
        risks.sort(key=lambda r: (_SEV_RANK[r.severity], r.id))
        report = FitReport(graph_sha256=contract.graph_sha256, target_sha256=sha256_file(lay.target_yaml),
                           risks=risks, dropped_unverified=det_dropped + mres.dropped, created=now_iso())
        lay.ensure(lay.target_dir)
        atomic_write_json(lay.fit_report_json, report)
        run = FitRun(report=report, json_path=lay.fit_report_json, md_path=lay.fit_report_md,
                     deterministic=sum(1 for r in risks if r.source == "deterministic"), model=mres, offline=offline)
        atomic_write_text(lay.fit_report_md, render_markdown(report, spec, load_resolutions(project).current(),
                                                             run))
        span.update(risks=len(risks), high=sum(r.severity == "high" for r in risks),
                    dropped_unverified=report.dropped_unverified, model_calls=mres.calls,
                    invalid_outputs=mres.invalid_outputs, budget_stopped=mres.budget_stopped,
                    dropped_reasons=mres.dropped_reasons)
    return run


# ------------------------------------------------------------------ report rendering
def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).replace("|", "\\|")[:160]


def render_markdown(report: FitReport, spec: TargetSpec, resolutions: dict[str, FitResolution],
                    run: FitRun | None = None) -> str:
    L = ["# Target-fit report", "",
         f"Migration: `{_cell(_migration_line(spec))}`  ", f"Created: {report.created}  ",
         f"Graph sha256: `{report.graph_sha256[:12]}`, target.yaml sha256: `{report.target_sha256[:12]}`", ""]
    by = {s: sum(r.severity == s for r in report.risks) for s in ("high", "medium", "low")}
    L.append(f"{len(report.risks)} risk(s): {by['high']} high, {by['medium']} medium, {by['low']} low; "
             f"{sum(r.needs_decision for r in report.risks)} need a decision; "
             f"{report.dropped_unverified} unverified risk(s) dropped.")
    if run is not None:
        m = run.model
        mode = "offline (no model findings)" if run.offline else ("skipped" if not m.batches and not m.calls
                                                                    else f"{m.calls} call(s) over {m.batches} batch(es)")
        L.append(f"Model layer: {mode}" + (f", {m.invalid_outputs} invalid output(s)" if m.invalid_outputs else "")
                 + (", stopped at the budget cap" if m.budget_stopped else "") + ".")
    blockers = [r.id for r in report.risks if r.severity == "high" and r.id not in resolutions]
    L += ["", f"Plan generation blockers (open high-severity risks): {', '.join(blockers) or 'none'}", "",
          "| ID | Severity | Kind | Title | Items | Evidence | Status |", "|---|---|---|---|---|---|---|"]
    for r in report.risks:
        res = resolutions.get(r.id)
        status = res.status if res else ("open, needs decision" if r.needs_decision else "open")
        L.append(f"| {r.id} | {r.severity} | {r.kind} | {_cell(r.title)} | {_cell(', '.join(r.item_ids[:6]))}"
                 f"{' ...' if len(r.item_ids) > 6 else ''} | {len(r.evidence)} | {status} |")
    for r in report.risks:
        L += ["", f"## {r.id}: {_cell(r.title)}", "",
              f"{r.severity} / {r.kind} / {r.source}" + (" / needs decision" if r.needs_decision else ""), ""]
        if r.detail:
            L += [re.sub(r"\s+", " ", r.detail), ""]
        if r.proposed_handling:
            L += ["Proposed handling: " + re.sub(r"\s+", " ", r.proposed_handling), ""]
        for e in r.evidence[:20]:
            loc = f"{e.file}:{e.line_start}" if e.line_start else e.file
            L.append(f"- `{loc}`" + (f" `{_cell(e.quote).replace('`', '')}`" if e.quote else ""))
        if len(r.evidence) > 20:
            L.append(f"- ... and {len(r.evidence) - 20} more (fit_report.json)")
        res = resolutions.get(r.id)
        if res:
            L += ["", f"Resolution: **{res.status}** by {_cell(res.by)} at {res.created}: {_cell(res.note)}"]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ resolutions, gate, UI state
def load_report(project) -> FitReport | None:
    from .paths import Phase2Layout
    p = Phase2Layout(project).fit_report_json
    if not p.exists():
        return None
    return FitReport.model_validate_json(p.read_text())


def load_resolutions(project) -> FitResolutionsFile:
    from .paths import Phase2Layout
    p = Phase2Layout(project).fit_resolutions
    if not p.exists():
        return FitResolutionsFile()
    return FitResolutionsFile.model_validate_json(p.read_text())


def resolve_risk(project, risk_id: str, status: str, note: str, by: str = "user") -> FitResolution:
    """Record a human decision on a risk of the current fit report (FitError if unknown / invalid)."""
    from ..audit import AuditLog
    from .paths import Phase2Layout
    from .state import atomic_write_json, file_lock, now_iso
    report = load_report(project)
    if report is None:
        raise FitError("no fit report yet: run `parity target fit` first")
    risk = next((r for r in report.risks if r.id == risk_id), None)
    if risk is None:
        raise FitError(f"{risk_id} is not in target/fit_report.json")
    if status not in RESOLUTION_STATUSES:
        raise FitError(f"status must be one of {', '.join(RESOLUTION_STATUSES)}")
    if not (note or "").strip():
        raise FitError("a note is required (why this decision)")
    lay = Phase2Layout(project)
    lay.ensure(lay.target_dir)
    res = FitResolution(risk_id=risk_id, status=status, note=note.strip()[:2000], by=(by or "user")[:100],
                        created=now_iso(), risk_title=risk.title[:200])
    with file_lock(lay.fit_resolutions):
        f = load_resolutions(project)
        f.resolutions.append(res)
        atomic_write_json(lay.fit_resolutions, f)
    try:
        AuditLog(Path(project.audit_dir) / "audit.jsonl").record("target_fit", "fit_resolve", risk_id=risk_id,
                                                                 status=status, by=res.by)
    except OSError:
        pass
    return res


def report_staleness(project, report: FitReport) -> list[str]:
    """Reasons the report no longer matches target.yaml / the approved contract ([] = current)."""
    from .contract import ContractError, load_contract
    from .paths import Phase2Layout
    from .state import sha256_file
    out = []
    ty = Phase2Layout(project).target_yaml
    if not ty.exists():
        out.append("target/target.yaml is missing")
    elif sha256_file(ty) != report.target_sha256:
        out.append("target/target.yaml changed since the fit report was written")
    try:
        c = load_contract(project)
        if c.graph_sha256 != report.graph_sha256:
            out.append("the approved contract changed since the fit report was written")
    except ContractError as e:
        out.append(f"contract unreadable: {e}")
    return out


def fit_blockers(project, *, allow_stale: bool = False) -> list[Risk]:
    """Open (unresolved) high-severity risks. The planner refuses to plan while this is non-empty.

    Raises FitError when there is no fit report, or when it is stale (target.yaml or contract changed) unless
    allow_stale=True: a missing or outdated analysis must not read as "no blockers"."""
    report = load_report(project)
    if report is None:
        raise FitError("no target-fit report: run `parity target fit` before planning")
    stale = report_staleness(project, report)
    if stale and not allow_stale:
        raise FitError("target-fit report is stale: " + "; ".join(stale) + " (re-run `target fit`)")
    done = load_resolutions(project).current()
    return [r for r in report.risks if r.severity == "high" and r.id not in done]


def fit_state(project) -> dict:
    """JSON-ready state for a review-UI Target-fit tab. Never raises; problems go into `error` / `stale`."""
    out: dict = {"exists": False, "stale": [], "risks": [], "blockers": [], "statuses": list(RESOLUTION_STATUSES),
                 "counts": {}, "dropped_unverified": 0, "created": "", "error": None}
    try:
        report = load_report(project)
        if report is None:
            return out
        res = load_resolutions(project).current()
        out.update(exists=True, created=report.created, dropped_unverified=report.dropped_unverified,
                   stale=report_staleness(project, report))
        for r in report.risks:
            d = r.model_dump(mode="json")
            rr = res.get(r.id)
            d["resolution"] = rr.model_dump(mode="json") if rr else None
            d["open"] = rr is None
            d["blocking"] = rr is None and r.severity == "high"
            out["risks"].append(d)
        out["blockers"] = [d["id"] for d in out["risks"] if d["blocking"]]
        out["counts"] = {s: sum(r.severity == s for r in report.risks) for s in ("high", "medium", "low")}
        out["counts"]["needs_decision"] = sum(r.needs_decision for r in report.risks)
        out["counts"]["open"] = sum(d["open"] for d in out["risks"])
    except Exception as e:  # noqa: BLE001 - UI state must not crash the server
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ------------------------------------------------------------------ CLI
def _make_llm(project, spec: TargetSpec, offline: bool, config_path, max_usd):
    if offline:
        return OfflineFitLLM()
    import os

    from ..audit import AuditLog
    from ..cli import _budget, _load_cfg
    from ..llm.base import BudgetExceeded
    from ..llm.litellm_client import LiteLLMClient
    from .ledger import budget_for
    if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                                           "OPENAI_API_KEY")):
        raise click.ClickException("No LLM API key found (see .env.example), or run with --offline.")
    cfg = _load_cfg(config_path)
    try:
        budget = budget_for(project, spec.budget_usd, max_usd=_budget(max_usd))
    except BudgetExceeded as e:
        raise click.ClickException(str(e))
    return LiteLLMClient(cfg, budget=budget, cache_dir=Path(project.root) / "llm_cache",
                         audit=AuditLog(Path(project.audit_dir) / "audit.jsonl"), agent="target_fit")


def register(groups) -> None:
    @groups.target.command("fit")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--max-usd", type=float, default=None, help="Cap for this run (also capped by budget_usd).")
    @click.option("--offline", is_flag=True, help="Deterministic layer only; no network, no model findings.")
    @click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None,
                  help="LLM config.json")
    @click.option("--pin-contract", "pin", is_flag=True, help="Accept a contract pinned to an older graph version.")
    @click.option("--accept-unmapped", is_flag=True, help="Accept unmapped inventory items in the contract.")
    def fit_cmd(project: Path, max_usd, offline, config_path, pin, accept_unmapped) -> None:
        """Target-fit analysis: write target/fit_report.json and fit_report.md."""
        from ..llm.base import BudgetExceeded
        from .target import TargetSpecError, _project, load_target, require_gate
        p = _project(project)
        require_gate(p, pin=pin, accept_unmapped=accept_unmapped)
        try:
            spec = load_target(p)
        except TargetSpecError as e:
            raise click.ClickException(str(e))
        llm = _make_llm(p, spec, offline, config_path, max_usd)
        try:
            run = run_fit(p, llm, pin=pin, accept_unmapped=accept_unmapped, log=click.echo, offline=offline)
        except (FitError, BudgetExceeded) as e:
            raise click.ClickException(str(e))
        r = run.report
        by = {s: sum(x.severity == s for x in r.risks) for s in ("high", "medium", "low")}
        click.echo(f"fit: {len(r.risks)} risk(s) ({by['high']} high, {by['medium']} medium, {by['low']} low), "
                   f"{sum(x.needs_decision for x in r.risks)} need a decision, "
                   f"{r.dropped_unverified} unverified dropped; model calls {run.model.calls}"
                   + (" (offline)" if offline else ""))
        blockers = fit_blockers(p, allow_stale=True)
        click.echo(f"blockers: {', '.join(b.id for b in blockers) or 'none'}")
        click.echo(f"report: {run.json_path}\n        {run.md_path}")

    @groups.target.command("resolve")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("risk_id")
    @click.option("--status", type=click.Choice(list(RESOLUTION_STATUSES)), required=True)
    @click.option("--note", required=True, help="Why (stored with the decision).")
    @click.option("--by", default=None, help="Who decides (default: $USER).")
    @click.option("--pin-contract", "pin", is_flag=True)
    @click.option("--accept-unmapped", is_flag=True)
    def resolve_cmd(project: Path, risk_id: str, status: str, note: str, by, pin, accept_unmapped) -> None:
        """Record a decision on a target-fit risk (target/fit_resolutions.json)."""
        import os

        from .paths import Phase2Layout
        from .target import _project, load_target, require_gate
        p = _project(project)
        require_gate(p, pin=pin, accept_unmapped=accept_unmapped)
        try:
            res = resolve_risk(p, risk_id, status, note, by or os.environ.get("USER") or "user")
            report = load_report(p)
            if report is not None:
                from .target import TargetSpecError
                try:
                    spec = load_target(p)
                except TargetSpecError:
                    spec = None
                if spec is not None:
                    from .state import atomic_write_text
                    atomic_write_text(Phase2Layout(p).fit_report_md,
                                      render_markdown(report, spec, load_resolutions(p).current()))
        except FitError as e:
            raise click.ClickException(str(e))
        click.echo(f"{res.risk_id}: {res.status} ({res.note})")
        left = fit_blockers(p, allow_stale=True)
        click.echo(f"blockers: {', '.join(b.id for b in left) or 'none'}")
