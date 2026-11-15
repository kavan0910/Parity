"""Offline (no-LLM) re-render of docs/understanding.latest.md from the latest graph snapshot.

Builds the same inputs `analyze` (analyze/runner.py) and targeted re-analysis (review/reanalyze.py) pass to
`render_understanding`: project name/source, fallback-parsed files, the saved inventory, coverage of the
latest graph (with the gap-fill exclusions analyze persisted in facts/coverage_exclusions.json), and the
effective quirk decisions. When the latest snapshot was produced by targeted re-analysis, the previous
snapshot is passed too, so the changelog section matches what reanalyze wrote.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..analyze.coverage import CoverageReport, project_coverage
from ..extract.treesitter import AstFacts
from ..graph import load_latest, load_snapshot
from ..graph.models import Graph
from ..pipeline import Inventory
from ..project import Project
from ..render import render_understanding
from ._io import ReviewError, atomic_write
from .decisions import effective_decisions

DOC_NAME = "understanding.latest.md"


def doc_path(project: Project) -> Path:
    return project.docs / DOC_NAME


def load_inventory(project: Project) -> Inventory:
    if not project.inventory_path.exists():
        return Inventory(items=[], counts={})
    return Inventory.model_validate_json(project.inventory_path.read_text())


def coverage_dict(cov: CoverageReport) -> dict:
    """The coverage mapping shown in section 15 (same shape as runner.py / reanalyze.py)."""
    return cov.model_dump(exclude={"unmapped_items"}) | {"unmapped_ids": [i.id for i in cov.unmapped_items]}


def doc_meta(project: Project, graph: Graph) -> dict:
    meta = project.meta()
    fallback: list[str] = []
    ast_path = project.facts / "ast.json"
    if ast_path.exists():
        ast = AstFacts.model_validate_json(ast_path.read_text())
        fallback = [f.path for f in ast.files if f.parse_status == "fallback"]
    return {"name": meta.name, "source": meta.source, "fallback_parsed_files": fallback,
            "decisions": effective_decisions(project, graph)}


def _previous(project: Project, graph: Graph) -> Graph | None:
    """The snapshot targeted re-analysis diffed against, if the latest snapshot came from re-analysis."""
    p = project.root / "feedback" / "reanalysis_log.json"
    if not p.exists():
        return None
    runs = json.loads(p.read_text()).get("runs", [])
    run = next((r for r in reversed(runs) if r.get("to_version") == graph.version), None)
    if run is None:
        return None
    return load_snapshot(project.graph_dir, int(run["from_version"]))


def render_latest(project: Project, graph: Graph | None = None) -> str:
    graph = graph or load_latest(project.graph_dir)
    if graph is None:
        raise ReviewError("no graph snapshot; run analyze first")
    inv = load_inventory(project)
    cov = project_coverage(project, inv, graph)
    return render_understanding(graph, doc_meta(project, graph), inv.counts, coverage=coverage_dict(cov),
                                previous=_previous(project, graph))


def write_latest(project: Project) -> Path:
    path = doc_path(project)
    atomic_write(path, render_latest(project))
    return path
