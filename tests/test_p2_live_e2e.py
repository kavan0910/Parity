"""Opt-in live end-to-end trial on a REAL Docker daemon (MA_LIVE_E2E=1): the express-crud fixture goes through
approve, target fit, plan, legacy build and start, characterization recording, an identity "migration"
(legacy code copied into target_code), new build and start, equivalence, differential fuzzing, the composite
(strangler router: flip, route, unflip), and finally a seeded regression that must be caught.

Needs, present locally (Phase 2 never pulls): node:20.18.0-bookworm, ubuntu/squid:6.6-24.04_beta,
python:3.12.7-alpine3.20, plus outbound access to registry.npmjs.org (the build goes through the allowlisting
proxy). No model, no API key, no cost. About 3 minutes. Run:

    MA_LIVE_E2E=1 .venv/bin/python -m pytest -q tests/test_p2_live_e2e.py
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from parity_migrate.cli import main
from parity_migrate.migrate import equivalence as eq, targetrepo
from parity_migrate.migrate.differential import load_fuzz_reports
from parity_migrate.migrate.paths import Phase2Layout
from parity_migrate.migrate.target import load_target
from parity_migrate.project import Project

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ("node:20.18.0-bookworm", "ubuntu/squid:6.6-24.04_beta", "python:3.12.7-alpine3.20")


def _images_present() -> bool:
    try:
        return all(subprocess.run(["docker", "image", "inspect", i], capture_output=True, timeout=30).returncode == 0
                   for i in IMAGES)
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(os.environ.get("MA_LIVE_E2E") != "1" or not _images_present(),
                                reason="set MA_LIVE_E2E=1 with a Docker daemon and the images present locally")


def _run(*args, ok=True):
    r = CliRunner().invoke(main, [str(a) for a in args])
    if ok:
        assert r.exit_code == 0, f"{args}\n{r.output}"
    return r


def test_express_crud_identity_port_then_seeded_regression(tmp_path, monkeypatch):
    monkeypatch.setenv("MA_MIRROR_HOSTS", "registry.npmjs.org")
    monkeypatch.chdir(ROOT)                                   # the project source path is relative to the repo
    pdir = tmp_path / "ex.migration"
    try:
        _run("init", "eval/repos/express-crud", "--project", pdir, "--name", "express-crud")
        for step in (("ingest",), ("extract",), ("analyze", "--offline")):
            _run(step[0], pdir, *step[1:])
        _run("approve", pdir, "--by", "live-e2e")
        _run("target", "init", pdir, "--runtime", "node@20", "--framework", "fastify@4", "--build", "npm",
             "--base-image", "node:20.18.0-bookworm", "--kind", "framework_move", "--budget-usd", "1")
        _run("target", "fit", pdir, "--offline")
        _run("plan", pdir, "--offline")
        _run("harness", "import-source", pdir, "--from-original")

        # ---- legacy: build in the sandbox (npm through the allowlisting proxy), start, record twice
        _run("harness", "build", pdir)
        _run("harness", "up", pdir)
        _run("tests", "generate", pdir, "--offline")
        rec = _run("tests", "record", pdir, "--harness")
        assert "recorded 24/24" in rec.output, rec.output
        import glob
        import json
        from collections import Counter
        st = Counter(json.load(open(f))["expect"].get("status") for f in glob.glob(str(pdir / "characterization/cases/*/CT-*.json")))
        assert st[200] >= 10 and st[401] == 0, st             # authenticated and seeded: not a wall of 401s

        # ---- identity migration: legacy code as the "new" service
        p = Project.load(pdir)
        lay = Phase2Layout(p)
        targetrepo.init_repo(p, load_target(p))
        for rel in ("package.json", "src"):
            s, d = lay.legacy_src / rel, lay.target_code / rel
            shutil.copytree(s, d, dirs_exist_ok=True) if s.is_dir() else shutil.copyfile(s, d)
        targetrepo.commit_all(p, "identity port")
        import yaml
        h = pdir / "harness" / "harness.yaml"
        d = yaml.safe_load(h.read_text())
        d["new"] = {**d["legacy"], "image": "ma-fixture/express-crud:new", "dockerfile": "new/Dockerfile", "context": "new"}
        h.write_text(yaml.safe_dump(d, sort_keys=False))
        (pdir / "harness" / "new").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "eval/repos/express-crud/harness/Dockerfile", pdir / "harness/new/Dockerfile")
        _run("harness", "build", pdir, "--new")
        _run("harness", "up", pdir, "--new")

        for sid in ("SLICE-0006", "SLICE-0007"):
            r = _run("equivalence", pdir, "--slice", sid, "--offline")
            assert "100.00%" in r.output and "0 regression(s)" in r.output, r.output
        assert eq.load_run_meta(p, "SLICE-0007").latency["cases"] > 0            # latency measured on both sides
        _run("equivalence", "fuzz", pdir, "--slice", "SLICE-0006", "--max-variants", "4")   # identical => exit 0

        # ---- composite (strangler router): legacy + new + router; flip a slice; every case passes through the router
        _run("harness", "build", pdir, "--composite")
        _run("harness", "up", pdir, "--composite")
        _run("harness", "flip", pdir, "SLICE-0007", "--by", "live-e2e", "--reason", "trial", "--force-for-testing")
        r = _run("equivalence", "composite", pdir)
        assert "flipped: SLICE-0007" in r.output and "pass 15" in r.output, r.output
        assert "-> new" in _run("harness", "routes", pdir).output
        _run("harness", "unflip", pdir, "SLICE-0007", "--by", "live-e2e", "--reason", "rollback")
        _run("harness", "down", pdir, "--composite")

        # ---- seeded regression must be caught
        prod = lay.target_code / "src" / "routes" / "products.js"
        prod.chmod(0o644)                                     # target_code files are committed read-only
        prod.write_text(prod.read_text().replace("res.status(404).end()", "res.status(410).end()"))
        targetrepo.commit_all(p, "seeded regression")
        _run("harness", "down", pdir, "--new")
        _run("harness", "build", pdir, "--new")
        _run("harness", "up", pdir, "--new")
        r = _run("equivalence", pdir, "--slice", "SLICE-0007", "--offline", ok=False)
        assert r.exit_code == 1 and "1 regression(s)" in r.output, r.output
        rep = eq.load_report(p, "SLICE-0007")
        assert {(d.field, d.legacy, d.new) for d in rep.diffs if d.classification == "regression"} == {("status", 404, 410)}
        _run("equivalence", "fuzz", pdir, "--slice", "SLICE-0007", "--max-variants", "4", ok=False)
        assert any(fz.slice == "SLICE-0007" and fz.divergences for fz in load_fuzz_reports(p))
    finally:
        for flag in ("--composite", "--new", ""):
            CliRunner().invoke(main, ["harness", "down", str(pdir)] + ([flag] if flag else []))
