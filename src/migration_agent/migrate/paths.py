"""Phase 2 project-folder layout (docs/phase2-plan.md section 12), as properties over a Project.

Properties only compute paths; nothing is created on access. Writers call `layout.ensure(dir)` (or use
`migrate/state.atomic_write_*`, which creates parent folders) right before the first write. `ensure` also bumps
project.json `schema_version` 1 -> 2 the first time a Phase 2 folder is created, so a project that never runs
Phase 2 keeps exactly the Phase 1 layout (`Project.init` never creates these folders).

<project>/
  docs/migration_contract.json      Phase 1 output, read-only for Phase 2 (migrate/contract.py)
  legacy_src/                       original unredacted source pinned by source_hash; mounted ONLY into exec jobs
  inputs/traffic/                   optional user-supplied recorded requests (redacted at ingest)
  target/                           target.yaml, fit_report.json|md, fit_resolutions.json
  plan/                             migration_plan.json|md, versions/vNNN.json
  characterization/                 cases/SLICE-NNNN/CT-NNNN.json, fixtures/, stubs/, recordings/, coverage.json
                                    (named characterization/ instead of the plan's tests/ to avoid confusion
                                    with a repository's own tests)
  harness/                          generated compose files for legacy, new, db, stubs, router
  target_code/                      the new service, a git repo (agent and human commits)
  slices/SLICE-NNNN/                state.json, context/, iterations/, review.json, decisions.json,
                                    equivalence.json, approval.json
  runs/RUN-NNNN/                    meta.json, logs/, reports/, work/
  migration/                        ids.json, comments.json, runtime_findings.json, contract.schema.json
  config/                           config_map.json|md
  data/                             mapping.yaml, schema_compat.json
  equivalence/                      report.md
  cutover/                          plan.md, approval.json
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..project import Project
from .models import is_id

PHASE2_SCHEMA_VERSION = 2

PHASE2_DIRS = ("legacy_src", "inputs/traffic", "target", "plan", "characterization", "harness", "target_code",
               "slices", "runs", "migration", "config", "data", "equivalence", "cutover")


def _id(value: str, prefix: str) -> str:
    """Validate an ID before it becomes a path component (no traversal through crafted IDs)."""
    if not is_id(value, prefix):
        raise ValueError(f"not a valid {prefix} id: {value!r}")
    return value


class Phase2Layout:
    def __init__(self, project: Project | str | Path):
        self.project = project if isinstance(project, Project) else Project(Path(project))
        self.root = self.project.root

    # ---- creation
    def ensure(self, path: Path) -> Path:
        """mkdir -p `path` (must be inside the project) and mark the project as schema v2."""
        path = Path(path)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError(f"{path} is outside the project folder")
        path.mkdir(parents=True, exist_ok=True)
        self.mark_v2()
        return path

    def mark_v2(self) -> None:
        mp = self.project.meta_path
        if not mp.exists():
            return
        data = json.loads(mp.read_text())
        if int(data.get("schema_version", 1)) >= PHASE2_SCHEMA_VERSION:
            return
        data["schema_version"] = PHASE2_SCHEMA_VERSION
        fd, tmp = tempfile.mkstemp(dir=mp.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp, mp)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def rel(self, path: Path) -> str:
        """Project-relative POSIX path (what models store in *_ref fields)."""
        return Path(path).resolve().relative_to(self.root).as_posix()

    # ---- Phase 1 inputs
    contract_file = property(lambda s: s.project.docs / "migration_contract.json")
    ingest_report = property(lambda s: s.root / "ingest_report.json")

    # ---- legacy source and inputs
    legacy_src = property(lambda s: s.root / "legacy_src")
    inputs_traffic = property(lambda s: s.root / "inputs" / "traffic")

    # ---- target spec and fit (plan 2)
    target_dir = property(lambda s: s.root / "target")
    target_yaml = property(lambda s: s.target_dir / "target.yaml")
    fit_report_json = property(lambda s: s.target_dir / "fit_report.json")
    fit_report_md = property(lambda s: s.target_dir / "fit_report.md")
    fit_resolutions = property(lambda s: s.target_dir / "fit_resolutions.json")

    # ---- plan (plan 3)
    plan_dir = property(lambda s: s.root / "plan")
    plan_json = property(lambda s: s.plan_dir / "migration_plan.json")
    plan_md = property(lambda s: s.plan_dir / "migration_plan.md")
    plan_versions = property(lambda s: s.plan_dir / "versions")

    def plan_version(self, n: int) -> Path:
        if n < 1:
            raise ValueError("plan versions start at 1")
        return self.plan_versions / f"v{n:03d}.json"

    # ---- characterization tests (plan 4)
    characterization = property(lambda s: s.root / "characterization")
    cases_dir = property(lambda s: s.characterization / "cases")
    fixtures_dir = property(lambda s: s.characterization / "fixtures")
    stubs_dir = property(lambda s: s.characterization / "stubs")
    recordings_dir = property(lambda s: s.characterization / "recordings")
    test_coverage = property(lambda s: s.characterization / "coverage.json")

    def slice_cases_dir(self, slice_id: str) -> Path:
        return self.cases_dir / _id(slice_id, "SLICE")

    def case_file(self, slice_id: str, case_id: str) -> Path:
        return self.slice_cases_dir(slice_id) / f"{_id(case_id, 'CT')}.json"

    # ---- harness, new code
    harness_dir = property(lambda s: s.root / "harness")
    target_code = property(lambda s: s.root / "target_code")

    # ---- slices (plan 5)
    slices_dir = property(lambda s: s.root / "slices")

    def slice_dir(self, slice_id: str) -> Path:
        return self.slices_dir / _id(slice_id, "SLICE")

    def slice_state(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "state.json"

    def slice_context(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "context"

    def slice_iterations(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "iterations"

    def slice_review(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "review.json"

    def slice_decisions(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "decisions.json"

    def slice_equivalence(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "equivalence.json"

    def slice_approval(self, slice_id: str) -> Path:
        return self.slice_dir(slice_id) / "approval.json"

    # ---- exec runs (plan 6)
    runs_dir = property(lambda s: s.root / "runs")

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / _id(run_id, "RUN")

    def run_meta(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "meta.json"

    def run_logs(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "logs"

    def run_reports(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "reports"

    def run_work(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "work"

    # ---- migration bookkeeping
    migration = property(lambda s: s.root / "migration")
    ids_file = property(lambda s: s.migration / "ids.json")
    comments = property(lambda s: s.migration / "comments.json")
    runtime_findings = property(lambda s: s.migration / "runtime_findings.json")
    contract_schema = property(lambda s: s.migration / "contract.schema.json")

    # ---- config, data, equivalence, cutover (plan 7-9)
    config_dir = property(lambda s: s.root / "config")
    config_map_json = property(lambda s: s.config_dir / "config_map.json")
    config_map_md = property(lambda s: s.config_dir / "config_map.md")
    data_dir = property(lambda s: s.root / "data")
    data_mapping = property(lambda s: s.data_dir / "mapping.yaml")
    schema_compat = property(lambda s: s.data_dir / "schema_compat.json")
    equivalence_dir = property(lambda s: s.root / "equivalence")
    equivalence_report_md = property(lambda s: s.equivalence_dir / "report.md")
    cutover_dir = property(lambda s: s.root / "cutover")
    cutover_plan = property(lambda s: s.cutover_dir / "plan.md")
    cutover_approval = property(lambda s: s.cutover_dir / "approval.json")


def layout(project: Project | str | Path) -> Phase2Layout:
    return Phase2Layout(project)
