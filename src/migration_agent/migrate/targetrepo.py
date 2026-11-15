"""target_code/ as a local git repository (docs/phase2-plan.md 5.1, 12; P2-Q9: humans may commit too).

Hardening (same as ingest._git_clone): fixed argv (never a shell), GIT_* variables dropped from the
environment, no system/global config (GIT_CONFIG_NOSYSTEM, GIT_CONFIG_GLOBAL=/dev/null), no hook templates
(GIT_TEMPLATE_DIR="", `init --template=`), core.hooksPath=/dev/null on every call and in the repo config,
no fsmonitor, no LFS filters, no signing prompts, no network (only local commands are used).

    init_repo(project, spec)                 create (and for strategy in_place, seed) target_code/
    commit_iteration(project, sid, k, msg)   one agent commit per iteration, tag slice/SLICE-NNNN/iter-K
    commit_all(project, msg, tag=None)       agent commit of the whole tree (codemods, seed)
    dirty_paths(project)                     uncommitted changes (porcelain)
    diff(project, base, head)                unified diff (no external diff / textconv)
    human_commits(project, sid)              commits by anyone but the agent author touching the slice

Seeding for in_place: the UNREDACTED legacy_src/ copy is preferred because builds need the real files
(withheld credential files stay out); code/ (redacted) is used only when legacy_src/ is empty, with a warning.
Prompt context never comes from target_code/ contents except tree-sitter signatures and redacted reads.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AGENT_NAME = "migration-agent"
AGENT_EMAIL = "agent@localhost"
AGENT_AUTHOR = f"{AGENT_NAME} <{AGENT_EMAIL}>"
_SKIP_SEED = frozenset({".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox",
                        ".mypy_cache", ".pytest_cache"})


class RepoError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_TEMPLATE_DIR": "",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_AUTHOR_NAME": AGENT_NAME, "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
        "GIT_COMMITTER_NAME": AGENT_NAME, "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
    })
    return env


_BASE = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=",
         "-c", "filter.lfs.required=false", "-c", "core.autocrlf=false", "-c", "protocol.allow=never",
         "-c", "submodule.recurse=false", "-c", "init.defaultBranch=main", "-c", f"user.name={AGENT_NAME}",
         "-c", f"user.email={AGENT_EMAIL}"]


def git(root: Path, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    if shutil.which("git") is None:
        raise RepoError("git not found on PATH (target_code/ is a git repository)")
    argv = [*_BASE, "-C", str(root), *args]
    try:
        r = subprocess.run(argv, env=_env(), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RepoError(f"git {args[0] if args else ''} failed: {e}") from e
    if check and r.returncode != 0:
        raise RepoError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def _root(project) -> Path:
    from .paths import Phase2Layout
    return Phase2Layout(project).target_code


def is_repo(project) -> bool:
    return (_root(project) / ".git").is_dir()


def _seed(src: Path, dst: Path) -> int:
    """Copy regular files (no symlinks, no VCS/dependency folders, no credential files)."""
    from ..ingest.secrets import is_secret_filename
    n = 0
    for dp, dirs, names in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_SEED and not (Path(dp) / d).is_symlink())
        for name in sorted(names):
            p = Path(dp) / name
            if p.is_symlink() or not p.is_file() or is_secret_filename(name):
                continue
            rel = p.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, out)
            n += 1
    return n


def init_repo(project, spec=None) -> dict:
    """Create target_code/ as a git repo if needed. Returns {created, seeded_from, files, warning, head}."""
    from .paths import Phase2Layout
    lay = Phase2Layout(project)
    root = lay.target_code
    info = {"created": False, "seeded_from": "", "files": 0, "warning": "", "head": ""}
    if (root / ".git").is_dir():
        git(root, "config", "core.hooksPath", "/dev/null")
        info["head"] = head(project)
        return info
    lay.ensure(root)
    if any(p.name != ".git" for p in root.iterdir()):
        raise RepoError("target_code/ exists, is not empty and is not a git repository; move it away first")
    git(root, "init", "-q", "--template=")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "config", "core.fsmonitor", "false")
    info["created"] = True
    if spec is not None and spec.strategy == "in_place":
        src = lay.legacy_src
        if src.is_dir() and any(src.iterdir()):
            info["seeded_from"] = "legacy_src"
        else:
            src = Path(project.code)
            info["seeded_from"] = "code"
            info["warning"] = ("legacy_src/ is empty: seeded target_code/ from the REDACTED code/ copy; builds may "
                               "fail on [REDACTED] placeholders (run `harness import-source --from-original`)")
        info["files"] = _seed(src, root)
        msg = f"seed target_code from {info['seeded_from']}/ (strategy in_place)"
    else:
        msg = "initialize target_code"
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "--allow-empty", "-m", msg)
    git(root, "tag", "-f", "seed")
    info["head"] = head(project)
    return info


def head(project) -> str:
    r = git(_root(project), "rev-parse", "--verify", "-q", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def dirty_paths(project) -> list[str]:
    r = git(_root(project), "status", "--porcelain=v1", "-z", "--untracked-files=all")
    out = []
    for ent in r.stdout.split("\0"):
        if len(ent) > 3:
            out.append(ent[3:])
    return sorted(set(out))


def restore_paths(project, paths: list[str]) -> None:
    """Put `paths` back to HEAD (tracked) or remove them (untracked): undo a half-applied iteration."""
    root = _root(project).resolve()
    tracked = set(git(root, "ls-files", "-z").stdout.split("\0"))
    for rel in paths:
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or ".git" in Path(rel).parts:
            continue
        if rel in tracked:
            git(root, "checkout", "-q", "HEAD", "--", rel)
        else:
            p.unlink(missing_ok=True)


def iter_tag(slice_id: str, k: int) -> str:
    from .models import is_id
    if not is_id(slice_id, "SLICE") or k < 1:
        raise ValueError("bad slice id or iteration")
    return f"slice/{slice_id}/iter-{k}"


def commit_all(project, message: str, tag: str | None = None) -> str:
    root = _root(project)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "--allow-empty", f"--author={AGENT_AUTHOR}", "-m", message)
    if tag:
        git(root, "tag", "-f", tag)
    return head(project)


def commit_iteration(project, slice_id: str, k: int, message: str) -> str:
    """One agent commit per iteration (empty allowed), tagged slice/SLICE-NNNN/iter-K."""
    return commit_all(project, f"{slice_id} iteration {k}: {message}"[:2000], tag=iter_tag(slice_id, k))


def tag_commit(project, tag: str) -> str:
    r = git(_root(project), "rev-list", "-n", "1", tag, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def diff(project, base: str, tip: str = "HEAD", context: int = 3) -> str:
    if not base:
        return ""
    r = git(_root(project), "diff", "--no-color", "--no-ext-diff", "--no-textconv", f"-U{context}", base, tip,
            "--", check=False)
    return r.stdout


def _slice_tags(project, slice_id: str) -> list[str]:
    r = git(_root(project), "tag", "--list", f"slice/{slice_id}/iter-*", check=False)
    return sorted(r.stdout.split(), key=lambda t: int(t.rsplit("-", 1)[1]) if t.rsplit("-", 1)[1].isdigit() else 0)


def human_commits(project, slice_id: str) -> list[dict]:
    """Commits after the slice's first agent iteration whose AUTHOR is not the agent and that touch files the
    slice wrote (all files when the slice wrote none). [{sha, author, email, date, subject, files}]."""
    from .codegen import slice_written_files
    if not is_repo(project):
        return []
    tags = _slice_tags(project, slice_id)
    if not tags:
        return []
    first = tag_commit(project, tags[0])
    files = set(slice_written_files(project, slice_id))
    r = git(_root(project), "log", "--no-color", "--format=%x1e%H%x00%an%x00%ae%x00%aI%x00%s", "--name-only",
            f"{first}^..HEAD" if git(_root(project), "rev-parse", "-q", "--verify", f"{first}^",
                                     check=False).returncode == 0 else "HEAD", check=False)
    out = []
    for rec in r.stdout.split("\x1e"):
        if not rec.strip():
            continue
        head_line, _, rest = rec.partition("\n")
        parts = head_line.split("\x00")
        if len(parts) < 5:
            continue
        sha, name, email, date, subject = parts[:5]
        touched = [ln for ln in rest.splitlines() if ln.strip()]
        if email.strip().lower() == AGENT_EMAIL:
            continue
        if files and not (files & set(touched)):
            continue
        out.append({"sha": sha, "author": name, "email": email, "date": date, "subject": subject[:200],
                    "files": touched})
    return out
