"""Shared builders for the Phase 1 decision / approval tests (offline LLM only, no network)."""
from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from migration_agent.analyze.runner import run_analyze
from migration_agent.cli import main
from migration_agent.graph import load_latest, save_snapshot
from migration_agent.llm.offline import OfflineLLM
from migration_agent.project import Project
from migration_agent.review.docrender import write_latest
from migration_agent.verify.models import Finding, FindingsFile

REPO = Path(__file__).parent.parent / "eval" / "repos" / "express-crud"
_TEMPLATE: dict[str, Path] = {}


def _template(base: Path) -> Path:
    """Analyse express-crud once per session (offline), then copy the folder per test."""
    if "root" not in _TEMPLATE:
        root = base / "template.migration"
        r = CliRunner()
        for args in (["init", str(REPO), "--project", str(root)], ["ingest", str(root)], ["extract", str(root)]):
            res = r.invoke(main, args)
            assert res.exit_code == 0, res.output
        p = Project.load(root)
        run_analyze(p, OfflineLLM())
        g = load_latest(p.graph_dir)
        comps = [n for n in sorted(g.nodes, key=lambda n: n.id) if n.type == "component"]
        quirk, bug = comps[0], comps[1]
        nodes = [n.model_copy(update={"quirk": True}) if n.id == quirk.id else
                 n.model_copy(update={"suspected_bug": True}) if n.id == bug.id else n for n in g.nodes]
        save_snapshot(p.graph_dir, g.model_copy(update={"nodes": nodes}))
        write_latest(p)
        _TEMPLATE["root"] = root
    return _TEMPLATE["root"]


def make_project(tmp_path: Path, tmp_path_factory) -> Project:
    src = _template(tmp_path_factory.getbasetemp() / "phase1")
    dst = tmp_path / "p.migration"
    shutil.copytree(src, dst)
    p = Project(dst)
    g = load_latest(p.graph_dir)
    p.quirk_id = next(n.id for n in g.nodes if n.quirk)
    p.bug_id = next(n.id for n in g.nodes if n.suspected_bug)
    p.plain_id = next(n.id for n in g.nodes if not n.quirk and not n.suspected_bug)
    return p


def write_findings(p: Project, item_id: str) -> None:
    g = load_latest(p.graph_dir)
    ff = FindingsFile(graph_version=g.version, findings=[
        Finding(id="F-0001", kind="contradiction", title="refund window", item_ids=[item_id]),
        Finding(id="F-0002", kind="doc_only", title="audit trail", item_ids=[])])
    (p.root / "feedback" / "findings.json").write_text(ff.model_dump_json())
