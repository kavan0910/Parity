"""Shared builders for the Phase 2 tests: an analysed (offline) project, approved inside the test."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from migration_agent.project import Project
from migration_agent.review.approval import approve

from phase1_helpers import make_project


def approved_project(tmp_path: Path, tmp_path_factory, by: str = "alice") -> Project:
    p = make_project(tmp_path, tmp_path_factory)
    approve(p, by)
    return p


def contract_file(p: Project) -> Path:
    return p.docs / "migration_contract.json"


def edit_contract(p: Project, **changes) -> None:
    f = contract_file(p)
    d = json.loads(f.read_text())
    d.update(changes)
    f.write_text(json.dumps(d))


def make_writable(path: Path) -> None:
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
