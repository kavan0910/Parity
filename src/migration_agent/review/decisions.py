"""Per-quirk / suspected-bug migration decisions: feedback/decisions.json.

Every quirk or suspected-bug node gets a decision for the migration: "preserve" the current behaviour or
"fix" it. Nodes without a recorded decision default to "preserve" (explicit=False), because silently
changing behaviour is the main migration risk.
"""
from __future__ import annotations

from ..audit import AuditLog
from ..graph import load_latest
from ..graph.models import Graph
from ..ingest.secrets import redact_string
from ..project import Project
from ..verify.models import DecisionsFile, EffectiveDecision, QuirkDecision
from ._io import ReviewError, atomic_write, now_iso

DEFAULT_DECISION = "preserve"


class UnknownItem(ReviewError):
    pass


def _path(project: Project):
    return project.root / "feedback" / "decisions.json"


def load_decisions(project: Project) -> DecisionsFile:
    p = _path(project)
    if not p.exists():
        return DecisionsFile()
    return DecisionsFile.model_validate_json(p.read_text())


def add_decision(project: Project, item_id: str, decision: str, note: str = "") -> QuirkDecision:
    """Record (or replace) the decision for one quirk / suspected-bug node of the latest graph."""
    if decision not in ("preserve", "fix"):
        raise ReviewError(f"decision must be 'preserve' or 'fix', not {decision!r}")
    graph = load_latest(project.graph_dir)
    if graph is None:
        raise ReviewError("no graph snapshot; run analyze first")
    node = next((n for n in graph.nodes if n.id == item_id), None)
    if node is None:
        raise UnknownItem(f"unknown graph item: {item_id}")
    if not (node.quirk or node.suspected_bug):
        raise ReviewError(f"{item_id} is not a quirk or suspected bug; decisions apply only to those")
    d = QuirkDecision(item_id=item_id, decision=decision, note=redact_string(note.strip()), created=now_iso())
    df = load_decisions(project)
    df.decisions = [x for x in df.decisions if x.item_id != item_id] + [d]
    atomic_write(_path(project), df.model_dump_json(indent=2) + "\n")
    AuditLog(project.audit_dir / "audit.jsonl").record("reviewer", "decide", item_id=item_id, decision=decision,
                                                       graph_version=graph.version)
    return d


def effective_list(project: Project, graph: Graph) -> list[EffectiveDecision]:
    """One entry per quirk / suspected-bug node of `graph` (sorted by id), defaulting to preserve."""
    explicit = {d.item_id: d for d in load_decisions(project).decisions}
    out = []
    for n in sorted(graph.nodes, key=lambda x: x.id):
        if not (n.quirk or n.suspected_bug):
            continue
        d = explicit.get(n.id)
        out.append(EffectiveDecision(item_id=n.id, name=n.name, quirk=n.quirk, suspected_bug=n.suspected_bug,
                                     decision=d.decision if d else DEFAULT_DECISION, note=d.note if d else "",
                                     explicit=d is not None))
    return out


def effective_decisions(project: Project, graph: Graph) -> dict[str, dict]:
    """{item_id: {"decision", "note", "explicit"}} for every quirk / suspected-bug node of `graph`."""
    return {d.item_id: {"decision": d.decision, "note": d.note, "explicit": d.explicit}
            for d in effective_list(project, graph)}
