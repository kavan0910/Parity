"""Codemod adapter interface (docs/phase2-plan.md 3: deterministic codemods run first for version upgrades,
the model fixes what they leave).

A codemod never runs on the host. `run_codemod` copies target_code/ into runs/RUN-NNNN/work/app, runs the
adapter's ExecJob through the exec backend (phase `build`: network `mirror`, because recipe and tool downloads
come from the package mirror), compares the workspace with target_code/ afterwards, writes the patch to
runs/RUN-NNNN/codemod.patch, applies the changed text files into target_code/ and makes one agent commit
(tag codemod/<name>/RUN-NNNN). With the backend unavailable the codemod is skipped with a clear warning.

    class Adapter(Protocol):
        name: str
        def applies(self, spec) -> tuple[bool, str]          # (applies?, why / why not)
        def command(self, spec) -> list[str] | str           # argv, or a reason string = skip
        image: str                                           # container image (pin by digest in production)
        def image_for(self, spec) -> str                     # optional: per-spec image (OpenRewrite + Gradle)
        cache: str                                           # optional: project-relative cache dir mounted at /cache
"""
from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# build outputs and caches never copied back from the workspace
IGNORE_DIRS = frozenset({".git", "target", "build", "node_modules", ".gradle", "__pycache__", ".m2", ".venv",
                         "venv", ".tox", ".mypy_cache", ".pytest_cache", "dist", ".cache", ".npm"})
MAX_PATCH_FILE_BYTES = 2_000_000


class CodemodResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["applied", "no_changes", "skipped", "failed", "not_applicable"]
    reason: str = ""
    run_id: str = ""
    files: list[str] = Field(default_factory=list)
    commit: str = ""
    patch_ref: str = ""


class Adapter(Protocol):
    name: str
    image: str

    def applies(self, spec) -> tuple[bool, str]: ...

    def command(self, spec) -> list[str] | str: ...


def _walk(root: Path) -> dict[str, Path]:
    out = {}
    for dp, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not (Path(dp) / d).is_symlink())
        for n in names:
            p = Path(dp) / n
            if p.is_symlink() or not p.is_file():
                continue
            out[p.relative_to(root).as_posix()] = p
    return out


def _copy_tree(src: Path, dst: Path) -> None:
    for rel, p in _walk(src).items():
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)
    for dp, dirs, names in os.walk(dst):
        os.chmod(dp, 0o777)                     # container uid may differ; disposable per-run copy
        for n in names:
            q = Path(dp) / n
            os.chmod(q, (q.stat().st_mode | 0o666) & 0o777)


def _text(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_PATCH_FILE_BYTES:
            return None
        b = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in b[:8192]:
        return None
    return b.decode("utf-8", errors="replace")


def tree_changes(before: Path, after: Path) -> tuple[dict[str, str | None], str]:
    """({path: new text, or None for deleted}, unified patch) for text files; build outputs ignored."""
    a, b = _walk(before), _walk(after)
    changes: dict[str, str | None] = {}
    patch: list[str] = []
    for rel in sorted(set(a) | set(b)):
        old = _text(a[rel]) if rel in a else ""
        new = _text(b[rel]) if rel in b else None
        if rel in a and old is None:
            continue                            # binary/huge originals are left alone
        if rel in b and new is None:
            continue
        if rel in a and rel not in b:
            changes[rel] = None
            patch += difflib.unified_diff(old.splitlines(True), [], f"a/{rel}", "/dev/null")
        elif new != old:
            changes[rel] = new
            patch += difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                          f"a/{rel}" if rel in a else "/dev/null", f"b/{rel}")
    return changes, "".join(patch)


def run_codemod(project, adapter: Adapter, spec, *, backend=None) -> CodemodResult:
    from ...audit import AuditLog
    from .. import exec as _exec
    from .. import targetrepo
    from ..codegen import EditRejected, jail_path
    from ..ids import allocate
    from ..models import ExecJob, Limits, Mount
    from ..paths import Phase2Layout
    from ..state import atomic_write_text

    audit = AuditLog(Path(project.root) / "audit" / "audit.jsonl")
    ok, why = adapter.applies(spec)
    if not ok:
        return CodemodResult(name=adapter.name, status="not_applicable", reason=why)
    cmd = adapter.command(spec)
    if isinstance(cmd, str):
        return CodemodResult(name=adapter.name, status="skipped", reason=cmd)
    b = backend if backend is not None else _exec.get_backend(project)
    avail, reason = b.available()
    if not avail:
        msg = (f"WARNING: codemod {adapter.name} skipped: exec sandbox unavailable ({getattr(b, 'name', '?')}: "
               f"{reason}). Codemods never run on the host; the model will have to do this work.")
        audit.record("codemods", "codemod_skipped", codemod=adapter.name, reason=reason[:300])
        return CodemodResult(name=adapter.name, status="skipped", reason=msg)
    lay = Phase2Layout(project)
    root = lay.target_code
    run_id = allocate(project, "RUN")
    ws = lay.run_work(run_id) / "app"
    lay.ensure(ws)
    _copy_tree(root, ws)
    image = adapter.image_for(spec) if hasattr(adapter, "image_for") else adapter.image
    mounts = [Mount(source=lay.rel(ws), target="/work/app", read_only=False)]
    cache = getattr(adapter, "cache", "")
    if cache:                                   # shared package cache, same convention as `harness build`
        cdir = lay.ensure(Path(project.root) / cache)
        os.chmod(cdir, 0o777)
        mounts.append(Mount(source=cache, target="/cache", read_only=False))
    job = ExecJob(run_id=run_id, image=image, command=cmd, phase="build", network="mirror",
                  workdir="/work/app", mounts=mounts,
                  limits=Limits(timeout_s=1800, memory_mb=4096), labels={"stage": "codemod", "codemod": adapter.name})
    res = b.run(job)
    if not res.ok:
        audit.record("codemods", "codemod_failed", codemod=adapter.name, run_id=run_id, exit_code=res.exit_code)
        return CodemodResult(name=adapter.name, status="failed", run_id=run_id,
                             reason=(res.error or res.stderr_tail or f"exit code {res.exit_code}")[-500:])
    changes, patch = tree_changes(root, ws)
    pfile = atomic_write_text(lay.run_dir(run_id) / "codemod.patch", patch)
    if not changes:
        return CodemodResult(name=adapter.name, status="no_changes", run_id=run_id, patch_ref=lay.rel(pfile))
    written = []
    for rel, text in sorted(changes.items()):
        try:
            p, norm = jail_path(root, rel)
        except EditRejected:
            continue
        if text is None:
            p.unlink(missing_ok=True)
        else:
            atomic_write_text(p, text)
        written.append(norm)
    commit = targetrepo.commit_all(project, f"codemod {adapter.name} ({run_id})",
                                   tag=f"codemod/{adapter.name}/{run_id}")
    audit.record("codemods", "codemod_applied", codemod=adapter.name, run_id=run_id, files=len(written),
                 commit=commit)
    return CodemodResult(name=adapter.name, status="applied", run_id=run_id, files=written, commit=commit,
                         patch_ref=lay.rel(pfile))
