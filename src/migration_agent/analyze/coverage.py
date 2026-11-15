"""B-06 coverage checker: every inventory item must be mapped to a node or explicitly excluded.

Exclusions (gap-fill "excluded" classifications: inventory item id -> reason) are persisted in
facts/coverage_exclusions.json by `analyze` (analyze/runner.py), so every later consumer (targeted
re-analysis, the offline doc re-render, readiness/approval, the Phase 2 contract) computes the same
coverage as the analysis that produced the doc.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from ..graph.models import Graph
from ..pipeline import Inventory, InventoryItem

EXCLUSIONS_NAME = "coverage_exclusions.json"


class CoverageExclusions(BaseModel):
    """facts/coverage_exclusions.json: the exclusions the last `analyze` applied to its coverage."""
    schema_version: int = 1
    graph_version: int = 0
    excluded: dict[str, str] = Field(default_factory=dict)   # inventory item id -> reason


def exclusions_path(project) -> Path:
    return Path(project.facts) / EXCLUSIONS_NAME


def load_exclusions(project) -> dict[str, str]:
    """Persisted exclusions ({} when analyze never wrote any). Raises ValueError/ValidationError if corrupt."""
    p = exclusions_path(project)
    if not p.exists():
        return {}
    return dict(CoverageExclusions.model_validate_json(p.read_text()).excluded)


def save_exclusions(project, excluded: dict[str, str], graph_version: int = 0) -> Path:
    p = exclusions_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = CoverageExclusions(graph_version=graph_version, excluded=dict(sorted(excluded.items()))) \
        .model_dump_json(indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def project_coverage(project, inventory: Inventory, graph: Graph) -> "CoverageReport":
    """Coverage of `graph` with the persisted exclusions (what analyze rendered)."""
    return check_coverage(inventory, graph, load_exclusions(project))


class CoverageIncomplete(RuntimeError):
    def __init__(self, report: "CoverageReport"):
        super().__init__(f"coverage incomplete: {report.unmapped} unmapped inventory item(s): "
                         + ", ".join(i.id for i in report.unmapped_items[:10]))
        self.report = report


class CoverageReport(BaseModel):
    mapped: int
    unmapped: int
    excluded: int
    unmapped_items: list[InventoryItem] = Field(default_factory=list)
    excluded_items: dict[str, str] = Field(default_factory=dict)
    complete: bool


def check_coverage(inventory: Inventory, graph: Graph, excluded: dict[str, str] | None = None) -> CoverageReport:
    excluded = excluded or {}
    refs = {r for n in graph.nodes for r in n.inventory_refs}
    mapped = 0
    unmapped: list[InventoryItem] = []
    exc: dict[str, str] = {}
    for it in inventory.items:
        if it.id in refs:
            mapped += 1
        elif it.id in excluded:
            exc[it.id] = excluded[it.id]
        else:
            unmapped.append(it)
    return CoverageReport(mapped=mapped, unmapped=len(unmapped), excluded=len(exc), unmapped_items=unmapped,
                          excluded_items=exc, complete=not unmapped)


def assert_complete(report: CoverageReport) -> None:
    if report.unmapped > 0:
        raise CoverageIncomplete(report)
