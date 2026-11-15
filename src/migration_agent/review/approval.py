"""Phase 1 exit gate: readiness check, approval and the migration contract.

  feedback/approvals.json            ApprovalsFile (history; the last approval is the current one)
  docs/understanding.vN.approved.md  immutable copy of the approved doc for graph vN (read-only file)
  docs/migration_contract.json       MigrationContract, the input of Phase 2 (migration)

An approval pins the latest graph snapshot (sha256 of its file bytes) and the understanding doc (sha256 of
its text). Any later snapshot, or any change to the doc (for example a changed quirk decision), makes the
approval "stale" and a new approval is needed.
"""
from __future__ import annotations

import hashlib
import os

from pydantic import BaseModel, Field, ValidationError

from ..analyze.coverage import project_coverage
from ..audit import AuditLog
from ..graph import load_latest
from ..graph.models import Graph
from ..graph.store import GraphStoreError, snapshot_path
from ..ingest import IngestReport
from ..ingest.secrets import redact_string
from ..project import Project
from ..verify.models import (
    Approval, ApprovalsFile, CoverageSummary, DeferredFinding, MigrationContract, Resolution,
)
from ..render import _texts
from . import comments as _comments
from . import resolutions as _resolutions
from ._io import ReviewError, atomic_write, now_iso
from .decisions import effective_list
from .docrender import doc_meta, doc_path, load_inventory, render_latest

CONTRACT_NAME = "migration_contract.json"


class Readiness(BaseModel):
    ready: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _approvals_path(project: Project):
    return project.root / "feedback" / "approvals.json"


def contract_path(project: Project):
    return project.docs / CONTRACT_NAME


def ingest_source_hash(project: Project) -> str | None:
    """`source_hash` from ingest_report.json, or None when the report is missing or unreadable."""
    p = project.root / "ingest_report.json"
    if not p.exists():
        return None
    try:
        return IngestReport.model_validate_json(p.read_text()).source_hash or None
    except (ValidationError, ValueError):
        return None


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def graph_sha256(project: Project, version: int) -> str:
    """sha256 over the snapshot file bytes for `version`."""
    return sha256_bytes(snapshot_path(project.graph_dir, version).read_bytes())


def load_approvals(project: Project) -> ApprovalsFile:
    p = _approvals_path(project)
    if not p.exists():
        return ApprovalsFile()
    return ApprovalsFile.model_validate_json(p.read_text())


def latest_resolutions(project: Project) -> dict[str, Resolution]:
    """finding_id -> its most recent resolution (the review server appends; the CLI replaces)."""
    return {r.finding_id: r for r in _resolutions.load_resolutions(project).resolutions}


def _short(ids: list[str], n: int = 10) -> str:
    return ", ".join(ids[:n]) + (f" (+{len(ids) - n} more)" if len(ids) > n else "")


def _latest_graph(project: Project, blockers: list[str]) -> Graph | None:
    try:
        g = load_latest(project.graph_dir)
    except GraphStoreError as e:
        blockers.append(f"graph snapshot unreadable: {e}")
        return None
    if g is None:
        blockers.append("no graph snapshot: run `migration-agent analyze` first")
    return g


def readiness(project: Project) -> Readiness:
    blockers: list[str] = []
    warnings: list[str] = []
    graph = _latest_graph(project, blockers)
    doc = doc_path(project)
    if not doc.exists():
        blockers.append("no docs/understanding.latest.md: run `migration-agent analyze` first")
    inv = None
    if graph is not None:
        try:
            inv = load_inventory(project)
        except (ValidationError, ValueError) as e:
            blockers.append(f"inventory.json unreadable: {type(e).__name__}")
        if not project.inventory_path.exists():
            blockers.append("no inventory.json: run `migration-agent extract` first")
        elif inv is not None:
            try:
                cov = project_coverage(project, inv, graph)
            except (ValidationError, ValueError) as e:
                blockers.append(f"facts/coverage_exclusions.json unreadable: {type(e).__name__}")
                cov = None
            if cov is not None and cov.unmapped:
                blockers.append(f"coverage incomplete: {cov.unmapped} unmapped inventory item(s): "
                                + _short([i.id for i in cov.unmapped_items]))

    findings_file = project.root / "feedback" / "findings.json"
    findings = []
    if not findings_file.exists():
        warnings.append("Verifier never run (no feedback/findings.json): the doc was not checked against "
                        "user documents")
    else:
        try:
            findings = _resolutions.load_findings(project).findings
        except (ValidationError, ValueError) as e:
            blockers.append(f"feedback/findings.json unreadable: {type(e).__name__}")
    try:
        latest = latest_resolutions(project)
        pending = _resolutions.pending_reanalysis(project)
    except (ValidationError, ValueError) as e:
        blockers.append(f"feedback/resolutions.json unreadable: {type(e).__name__}")
        latest, pending = {}, []
    unanswered = [f.id for f in findings if f.id not in latest or latest[f.id].status == "open"]
    if unanswered:
        blockers.append(f"{len(unanswered)} Verifier finding(s) not resolved: {_short(unanswered)} "
                        "(use `resolve` or the review server)")
    if pending:
        blockers.append(f"{len(pending)} resolution(s) waiting for re-analysis: "
                        f"{_short(sorted({r.finding_id for r in pending}))} (run `resolve` with --reanalyze)")
    try:
        unprocessed = _comments.unprocessed(project)
    except (ValidationError, ValueError) as e:
        blockers.append(f"feedback/comments.json unreadable: {type(e).__name__}")
        unprocessed = []
    if unprocessed:
        blockers.append(f"{len(unprocessed)} comment(s) not yet used by re-analysis: "
                        f"{_short([c.id for c in unprocessed])} (run `resolve` with --reanalyze)")

    meta: dict = {}
    if graph is not None and doc.exists() and inv is not None:
        try:
            fresh = render_latest(project, graph)
            meta = doc_meta(project, graph)
        except (ValidationError, ValueError, OSError, GraphStoreError) as e:
            blockers.append(f"cannot re-render the understanding doc: {type(e).__name__}: {str(e)[:200]}")
        else:
            if doc.read_text() != fresh:
                blockers.append(f"docs/understanding.latest.md is out of date with graph v{graph.version} "
                                "(re-run `analyze`, which reuses cached chunks, or record a decision with "
                                "`decide` to re-render it)")

    deferred = [fid for fid, r in latest.items() if r.status == "deferred" and fid in {f.id for f in findings}]
    if deferred:
        warnings.append(f"{len(deferred)} Verifier finding(s) deferred to the migration: {_short(sorted(deferred))}")
    questions = _texts(meta.get("open_questions"))
    if questions:
        warnings.append(f"{len(questions)} open question(s) in the doc")
    if graph is not None:
        try:
            defaults = [d.item_id for d in effective_list(project, graph) if not d.explicit]
        except (ValidationError, ValueError) as e:
            blockers.append(f"feedback/decisions.json unreadable: {type(e).__name__}")
            defaults = []
        if defaults:
            warnings.append(f"{len(defaults)} quirk/bug item(s) on the default decision (preserve): {_short(defaults)}")
        low = [n.id for n in graph.nodes if n.confidence in ("none", "low")]
        if low:
            warnings.append(f"{len(low)} node(s) with low or no confidence (see section 13)")
    if ingest_source_hash(project) is None:
        warnings.append("no readable ingest_report.json: the migration contract will have an empty source_hash "
                        "(Phase 2 cannot detect legacy source changes; run `migration-agent ingest`)")
    return Readiness(ready=not blockers, blockers=blockers, warnings=warnings)


def approval_status(project: Project) -> dict:
    """{"state": "none"|"approved"|"stale", "approval": dict|None, "reason": str}."""
    af = load_approvals(project)
    if not af.approvals:
        return {"state": "none", "approval": None, "reason": "not approved yet"}
    a = af.approvals[-1]

    def out(state: str, reason: str) -> dict:
        return {"state": state, "approval": a.model_dump(), "reason": reason}

    try:
        g = load_latest(project.graph_dir)
    except GraphStoreError as e:
        return out("stale", f"graph snapshot unreadable: {e}")
    if g is None:
        return out("stale", "no graph snapshot")
    if g.version != a.graph_version:
        return out("stale", f"graph changed: approved v{a.graph_version}, latest is v{g.version}")
    if graph_sha256(project, g.version) != a.graph_sha256:
        return out("stale", f"graph snapshot v{g.version} changed on disk since approval")
    doc = doc_path(project)
    if not doc.exists() or sha256_text(doc.read_text()) != a.doc_sha256:
        return out("stale", "understanding doc changed since approval (for example a quirk decision changed)")
    return out("approved", f"approved by {a.approved_by} at {a.approved_at} (graph v{a.graph_version})")


def _approved_doc_name(project: Project, version: int, previous: int) -> str:
    k = previous + 1
    while True:
        name = f"understanding.v{version}.approved.md" if k == 1 else f"understanding.v{version}.r{k}.approved.md"
        if not (project.docs / name).exists():
            return name
        k += 1


def approve(project: Project, approved_by: str, note: str = "") -> Approval:
    """Approve the current understanding. Refuses (ReviewError listing blockers) unless `readiness` is ready."""
    by = redact_string(approved_by.strip())
    if not by:
        raise ReviewError("approved_by is required")
    r = readiness(project)
    if not r.ready:
        raise ReviewError("not ready for approval:\n- " + "\n- ".join(r.blockers))
    st = approval_status(project)
    if st["state"] == "approved":
        raise ReviewError(f"already approved: {st['reason']}")
    graph = load_latest(project.graph_dir)
    doc_text = doc_path(project).read_text()
    gsha, dsha = graph_sha256(project, graph.version), sha256_text(doc_text)
    af = load_approvals(project)
    at = now_iso()
    note = redact_string(note.strip())
    decisions = effective_list(project, graph)
    findings = {f.id: f for f in _resolutions.load_findings(project).findings}
    latest = latest_resolutions(project)
    deferred = [DeferredFinding(id=f.id, kind=f.kind, title=f.title, item_ids=f.item_ids, note=latest[fid].note)
                for fid, f in sorted(findings.items()) if fid in latest and latest[fid].status == "deferred"]
    meta = doc_meta(project, graph)
    questions, assumptions = _texts(meta.get("open_questions")), _texts(meta.get("assumptions"))
    cov = project_coverage(project, load_inventory(project), graph)

    previous = sum(1 for x in af.approvals if x.graph_version == graph.version)
    name = _approved_doc_name(project, graph.version, previous)
    rel_doc = f"docs/{name}"
    rel_graph = snapshot_path(project.graph_dir, graph.version).relative_to(project.root).as_posix()
    header = "\n".join([
        "> **Approved understanding (Phase 1 exit gate).** Immutable copy: do not edit.",
        f"> Approved by: {by}",
        f"> Approved at: {at}",
        f"> Graph version: v{graph.version} (`{rel_graph}`)",
        f"> Graph sha256: `{gsha}`",
        f"> Doc sha256: `{dsha}` (of the text below this header)",
    ] + ([f"> Note: {' '.join(note.split())}"] if note else [])) + "\n\n"
    out = project.docs / name
    atomic_write(out, header + doc_text)
    os.chmod(out, 0o444)

    contract = MigrationContract(
        project_name=project.meta().name, project_source=project.meta().source, graph_version=graph.version,
        graph_sha256=gsha, doc_sha256=dsha, approved_by=by, approved_at=at, note=note, approved_doc=rel_doc,
        graph_snapshot=rel_graph, source_hash=ingest_source_hash(project) or "", decisions=decisions,
        deferred_findings=deferred, open_questions=questions,
        assumptions=assumptions,
        coverage=CoverageSummary(mapped=cov.mapped, unmapped=cov.unmapped, excluded=cov.excluded, complete=cov.complete),
        warnings=r.warnings)
    atomic_write(contract_path(project), contract.model_dump_json(indent=2) + "\n")

    a = Approval(graph_version=graph.version, graph_sha256=gsha, doc_sha256=dsha, approved_by=by, approved_at=at,
                 note=note, deferred_finding_ids=[d.id for d in deferred], decisions=decisions,
                 open_questions=len(questions), warnings=r.warnings, approved_doc=rel_doc)
    af.approvals.append(a)
    atomic_write(_approvals_path(project), af.model_dump_json(indent=2) + "\n")
    AuditLog(project.audit_dir / "audit.jsonl").record(
        "reviewer", "approve", graph_version=graph.version, graph_sha256=gsha, doc_sha256=dsha,
        approved_by=by, warnings=len(r.warnings), approved_doc=rel_doc)
    return a
