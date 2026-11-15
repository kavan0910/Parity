"""M4 code generation: strict edit schema, validation, exact apply, safety checks and the context pack
(docs/phase2-plan.md 5.2-5.3, decision P2-Q6: the model has NO exec or network tools; it returns edits).

Model output (task `codegen`, prompts/codegen.md and prompts/codegen_fix.md), validated by `CodegenOutput`:

    {"files": [{"path": "src/app.py", "op": "create" | "replace", "content": "..."},
               {"path": "src/x.py", "op": "edit", "edits": [{"search": "exact old text", "replace": "new"}]}],
     "notes": "...", "covers": ["EP-0003", ...], "assumptions": ["..."], "need": ["src/routes/orders.js:10-80"]}

Pipeline (`prepare_changes`, pure: nothing is written until every check passed):
  1. parse + schema (extra keys refused, size caps: MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_EDITS)
  2. path jail: target_code/-relative, no absolute paths, no '..', no NUL/backslash, nothing under .git, no
     credential file names; resolved with sandbox ReadOnlyTools._resolve (same rules, symlinks followed)
  3. build/CI files: only the files of the target.yaml build tool are writable (BUILD_FILES); CI, wrappers,
     Dockerfiles and other build logic never are
  4. exact apply: every `search` must occur exactly once in the current text, else the whole output is rejected
     with a message the next attempt receives
  5. secret scan (ingest/secrets.py) of every changed file: a secret line the old file did not already contain
     fails the output
  6. dependency check: dependencies added to build files must match target.yaml `libraries.allow` and not
     `libraries.deny`; repository/index changes are refused. Mirror resolution is a hook (`resolver`); with no
     mirror configured a TODO warning is returned instead.
`write_changes` then writes the files (atomic per file). The orchestrator (slice_runner.py) commits them.

Context pack (`build_context_pack`, plan 5.3), deterministic and size-capped with analyze/chunker token estimates:
  spec        slice nodes, rules with evidence quotes, quirk/bug decisions from the contract, fit-risk resolutions
  legacy      legacy regions from node evidence (review/reanalyze._regions) read with ReadOnlyTools over code/
              (the REDACTED copy; legacy_src/ never reaches a prompt), whole files for small component files
  interfaces  public signatures (tree-sitter, extract/treesitter.extract_file) of files written by already
              approved dependency slices in target_code/, never bodies
  style       target.yaml stack and style rules
  files       target_code/ file list (names only, capped)
  failures    only the relevant failures of the previous iteration (given by the orchestrator)
All untrusted text is neutralised and placed between <<<NAME_BEGIN>>>/<<<NAME_END>>> delimiters by the prompt.

Own models (not in migrate/models.py): FileEdit, FileOp, CodegenOutput, Changes, ContextPack.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROMPTS = Path(__file__).parent / "prompts"
CODEGEN_PROMPT = PROMPTS / "codegen.md"
CODEGEN_FIX_PROMPT = PROMPTS / "codegen_fix.md"

MAX_FILES = 40
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 1_000_000
MAX_EDITS = 60
MAX_NEED = 10
MAX_NEED_LINES = 200
MAX_PATH_CHARS = 300
DEFAULT_PACK_TOKENS = 80_000
LEGACY_MARGIN = 20
WHOLE_FILE_TOKENS = 4_000
TARGET_FILES_CAP = 300

# build files the model may write, per target.yaml target.build
BUILD_FILES: dict[str, frozenset[str]] = {
    "maven": frozenset({"pom.xml"}),
    "gradle": frozenset({"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                         "gradle.properties"}),
    "npm": frozenset({"package.json", "package-lock.json", "npm-shrinkwrap.json"}),
    "yarn": frozenset({"package.json", "yarn.lock"}),
    "pnpm": frozenset({"package.json", "pnpm-lock.yaml"}),
    "pip": frozenset({"requirements.txt", "requirements-dev.txt", "constraints.txt", "pyproject.toml"}),
    "poetry": frozenset({"pyproject.toml", "poetry.lock"}),
    "go-mod": frozenset({"go.mod", "go.sum"}),
    "go": frozenset({"go.mod", "go.sum"}),
}
# build / CI logic that is never model-writable unless listed for the build tool above
PROTECTED_NAMES = frozenset().union(*BUILD_FILES.values()) | frozenset({
    "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "makefile", "gnumakefile", "dockerfile", "containerfile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "jenkinsfile", ".gitlab-ci.yml",
    ".travis.yml", "azure-pipelines.yml", "bitbucket-pipelines.yml", "tox.ini", "noxfile.py", "mvnw", "mvnw.cmd",
    "gradlew", "gradlew.bat", ".gitattributes", ".gitmodules", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc",
    "pip.conf", "settings.xml", "toolchains.xml", "build.sh", "conftest.py",
})
PROTECTED_DIRS = frozenset({".github", ".gitlab", ".circleci", ".mvn", "gradle", ".husky", ".devcontainer",
                            ".buildkite", ".git", ".hg", ".svn"})


class EditRejected(ValueError):
    """The model output cannot be applied. `.errors` is fed back to the next attempt."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileEdit(_Strict):
    search: str = Field(min_length=1, max_length=MAX_FILE_BYTES)
    replace: str = Field(max_length=MAX_FILE_BYTES)


class FileOp(_Strict):
    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)
    op: Literal["create", "replace", "edit"]
    content: str | None = Field(default=None, max_length=MAX_FILE_BYTES)
    edits: list[FileEdit] = Field(default_factory=list, max_length=MAX_EDITS)

    @model_validator(mode="after")
    def _shape(self):
        if self.op == "edit":
            if not self.edits or self.content is not None:
                raise ValueError(f"{self.path}: op 'edit' needs `edits` and no `content`")
        elif self.content is None or self.edits:
            raise ValueError(f"{self.path}: op '{self.op}' needs `content` and no `edits`")
        return self


_NEED = re.compile(r"^[^\s:]{1,300}:\d{1,7}-\d{1,7}$")


class CodegenOutput(_Strict):
    files: list[FileOp] = Field(default_factory=list, max_length=MAX_FILES)
    notes: str = Field(default="", max_length=20_000)
    covers: list[str] = Field(default_factory=list, max_length=500)
    assumptions: list[str] = Field(default_factory=list, max_length=100)
    need: list[str] = Field(default_factory=list, max_length=MAX_NEED)

    @model_validator(mode="after")
    def _need(self):
        for n in self.need:
            if not _NEED.match(n):
                raise ValueError(f"need entry {n!r} must look like 'path:10-80'")
        return self


def parse_output(text: str) -> CodegenOutput:
    from ..llm.base import parse_json
    try:
        data = parse_json(text)
    except (ValueError, json.JSONDecodeError):
        raise EditRejected(["reply is not a JSON object"]) from None
    if not isinstance(data, dict):
        raise EditRejected(["reply must be one JSON object"])
    try:
        return CodegenOutput.model_validate(data)
    except ValidationError as e:
        msgs = []
        for err in e.errors()[:10]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msgs.append(f"schema: {loc or '(root)'}: {err.get('msg')}")
        raise EditRejected(msgs) from None


# ------------------------------------------------------------------ path jail
def jail_path(root: Path, rel: str) -> tuple[Path, str]:
    """(resolved absolute path, normalised relative path) inside `root`, or EditRejected."""
    from ..ingest.secrets import is_secret_filename
    from ..sandbox.tools import ReadOnlyTools, SandboxViolation
    if not isinstance(rel, str) or not rel.strip():
        raise EditRejected(["empty path"])
    if "\x00" in rel or "\\" in rel or any(ord(c) < 32 for c in rel):
        raise EditRejected([f"{rel!r}: control characters and backslashes are not allowed in paths"])
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or rel.startswith("~"):
        raise EditRejected([f"{rel}: absolute paths are not allowed (paths are relative to target_code/)"])
    pp = PurePosixPath(rel)
    if ".." in pp.parts:
        raise EditRejected([f"{rel}: '..' is not allowed"])
    parts = [p for p in pp.parts if p not in ("", ".")]
    if not parts:
        raise EditRejected([f"{rel}: not a file path"])
    if any(p.lower() in (".git", ".hg", ".svn") for p in parts):
        raise EditRejected([f"{rel}: version-control metadata is not writable"])
    if is_secret_filename(parts[-1]):
        raise EditRejected([f"{rel}: credential files (.env, keys, .npmrc ...) are never written by the model"])
    try:
        resolved = ReadOnlyTools(root)._resolve("/".join(parts))
    except SandboxViolation as e:
        raise EditRejected([f"{rel}: {e}"]) from None
    if resolved == Path(root).resolve():
        raise EditRejected([f"{rel}: not a file path"])
    norm = resolved.relative_to(Path(root).resolve()).as_posix()
    if any(p in (".git",) for p in PurePosixPath(norm).parts):
        raise EditRejected([f"{rel}: resolves into .git"])
    return resolved, norm


def build_file_problem(norm: str, build_tool: str) -> str | None:
    """Why the model may not write `norm` (a build/CI file), or None."""
    parts = PurePosixPath(norm).parts
    name = parts[-1].lower()
    allowed = BUILD_FILES.get((build_tool or "").lower(), frozenset())
    is_build = name in PROTECTED_NAMES or name.startswith(("dockerfile.", "requirements")) \
        or any(p.lower() in PROTECTED_DIRS for p in parts[:-1])
    if not is_build:
        return None
    if name in allowed and not any(p.lower() in PROTECTED_DIRS for p in parts[:-1]):
        return None
    hint = ", ".join(sorted(allowed)) if allowed else "none (target.yaml target.build is not set)"
    return (f"{norm}: build/CI file is not writable by the model (allowed for build tool "
            f"{build_tool or '?'}: {hint})")


# ------------------------------------------------------------------ dependency check
_PEP508 = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _req_names(text: str, errors: list[str], path: str) -> set[str]:
    out = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-") or "://" in line or line.startswith(("git+", "file:")) or " @ " in line:
            errors.append(f"{path}: '{line[:80]}': index options, URLs and direct references are not allowed "
                          "(dependencies resolve from the package mirror only)")
            continue
        m = _PEP508.match(line)
        if m:
            out.add(m.group(1).lower())
    return out


def _pyproject(text: str, errors: list[str], path: str) -> set[str]:
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        errors.append(f"{path}: invalid TOML ({e})")
        return set()
    out: set[str] = set()
    proj = d.get("project") or {}
    specs = list(proj.get("dependencies") or [])
    for v in (proj.get("optional-dependencies") or {}).values():
        specs += list(v or [])
    for s in specs:
        if isinstance(s, str) and (m := _PEP508.match(s)):
            if "://" in s or " @ " in s:
                errors.append(f"{path}: direct reference {s[:80]!r} is not allowed")
            out.add(m.group(1).lower())
    poetry = (d.get("tool") or {}).get("poetry") or {}
    groups = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    groups += [(g or {}).get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for g in groups:
        for k, v in g.items():
            if k.lower() == "python":
                continue
            if isinstance(v, dict) and ({"git", "url", "path"} & set(v)):
                errors.append(f"{path}: {k}: git/url/path dependencies are not allowed")
            out.add(k.lower())
    if poetry.get("source") or (d.get("tool") or {}).get("uv", {}).get("index"):
        errors.append(f"{path}: package sources/indexes are configured by the mirror, not in the build file")
    return out


def _package_json(text: str, errors: list[str], path: str) -> set[str]:
    try:
        d = json.loads(text)
    except ValueError:
        errors.append(f"{path}: invalid JSON")
        return set()
    out = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, ver in ((d.get(key) or {}) if isinstance(d, dict) else {}).items():
            if isinstance(ver, str) and re.match(r"^(git|https?|file|link|github):|^[\w.-]+/[\w.-]+(#|$)", ver):
                errors.append(f"{path}: {name}: git/url/file dependency specs are not allowed")
            out.add(name.lower())
    return out


_POM_BLOCK = re.compile(r"<(dependency|plugin)>(.*?)</\1>", re.S)
_TAG = {t: re.compile(rf"<{t}>\s*([^<\s]+)\s*</{t}>") for t in ("groupId", "artifactId")}
_POM_REPO = re.compile(r"<(?:repository|pluginRepository)>.*?<url>\s*([^<\s]+)\s*</url>", re.S)


def _pom(text: str) -> set[str]:
    out = set()
    for kind, body in _POM_BLOCK.findall(text):
        g = _TAG["groupId"].search(body)
        a = _TAG["artifactId"].search(body)
        if a:
            gid = g.group(1) if g else ("org.apache.maven.plugins" if kind == "plugin" else "")
            out.add(f"{gid}:{a.group(1)}".lower())
    return out


_GRADLE_DEP = re.compile(r"""['"]([\w.\-]+):([\w.\-]+)(?::[^'"\s]*)?['"]""")
_GRADLE_PLUGIN = re.compile(r"""\bid\s*\(?\s*['"]([\w.\-]+)['"]""")
_GRADLE_REPO = re.compile(r"""\burl\s*[=(]?\s*(?:uri\()?\s*['"]([^'"]+)['"]""")


def _gradle(text: str) -> set[str]:
    out = {f"{g}:{a}".lower() for g, a in _GRADLE_DEP.findall(text)}
    out |= {f"plugin:{p}".lower() for p in _GRADLE_PLUGIN.findall(text)}
    return out


def _gomod(text: str) -> set[str]:
    out = set()
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if line.startswith("require "):
            line = line[len("require "):]
        elif not in_block:
            continue
        if line:
            out.add(line.split()[0].lower())
    return out


def _repos(name: str, text: str) -> set[str]:
    if name == "pom.xml":
        return set(_POM_REPO.findall(text))
    if name.startswith(("build.gradle", "settings.gradle")):
        return set(_GRADLE_REPO.findall(text))
    return set()


def dependencies_of(path: str, text: str, errors: list[str] | None = None) -> tuple[str, set[str]]:
    """(ecosystem, dependency coordinates) declared in a build file; ("", set()) for other files."""
    errors = errors if errors is not None else []
    name = PurePosixPath(path).name.lower()
    if name == "package.json":
        return "npm", _package_json(text, errors, path)
    if name.startswith("requirements") and name.endswith(".txt") or name == "constraints.txt":
        return "pypi", _req_names(text, errors, path)
    if name == "pyproject.toml":
        return "pypi", _pyproject(text, errors, path)
    if name == "pom.xml":
        return "maven", _pom(text)
    if name.startswith(("build.gradle", "settings.gradle")):
        return "maven", _gradle(text)
    if name == "go.mod":
        return "go", _gomod(text)
    return "", set()


Resolver = Callable[[str, str], bool]   # (ecosystem, coordinate) -> resolvable in the package mirror


def check_dependencies(path: str, old: str | None, new: str, spec, *,
                       resolver: Resolver | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for dependencies ADDED to one build file (old -> new)."""
    from .knowledge import dep_glob_match
    errors: list[str] = []
    warnings: list[str] = []
    eco, new_deps = dependencies_of(path, new, errors)
    if not eco:
        return errors, warnings
    _, old_deps = dependencies_of(path, old or "", [])
    name = PurePosixPath(path).name.lower()
    added_repos = _repos(name, new) - _repos(name, old or "")
    for r in sorted(added_repos):
        errors.append(f"{path}: new package repository {r!r} is not allowed (dependencies come from the mirror)")
    allow = list(spec.libraries.allow) if spec is not None else []
    deny = list(spec.libraries.deny) if spec is not None else []
    for dep in sorted(new_deps - old_deps):
        if any(dep_glob_match(g, dep) for g in deny):
            errors.append(f"{path}: dependency {dep} matches target.yaml libraries.deny")
            continue
        if not any(dep_glob_match(g, dep) for g in allow):
            errors.append(f"{path}: new dependency {dep} is not in target.yaml libraries.allow "
                          "(add it there after review, or avoid the dependency)")
            continue
        if resolver is None:
            warnings.append(f"TODO: {eco} dependency {dep} not checked against a package mirror "
                            "(no mirror resolver configured)")
        elif not resolver(eco, dep):
            errors.append(f"{path}: dependency {dep} does not resolve in the package mirror "
                          "(hallucinated or typosquatted name?)")
    return errors, warnings


# ------------------------------------------------------------------ exact apply
@dataclass
class Changes:
    """Validated, not yet written. `files`: normalised path -> new text; `old`: previous text or None."""
    files: dict[str, str] = field(default_factory=dict)
    old: dict[str, str | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return sorted(self.files)


def apply_search_replace(text: str, edits: list[FileEdit], path: str) -> tuple[str, list[str]]:
    """Apply edits in order; each search must match exactly once in the current text."""
    errors = []
    for i, e in enumerate(edits, 1):
        n = text.count(e.search)
        if n == 0:
            errors.append(f"{path}: edit {i}: search text not found (copy it exactly from the current file, "
                          "including whitespace)")
            continue
        if n > 1:
            errors.append(f"{path}: edit {i}: search text matches {n} times; include more surrounding lines "
                          "so it is unique")
            continue
        text = text.replace(e.search, e.replace, 1)
    return text, errors


def _new_secret_lines(path: str, old: str | None, new: str) -> list[str]:
    from ..ingest.secrets import scan_text
    old_lines = set((old or "").splitlines())
    lines = new.splitlines()
    out = []
    for f in scan_text(path, new):
        if f.kind == "private_key" or (0 < f.line <= len(lines) and lines[f.line - 1] not in old_lines):
            out.append(f"{path}:{f.line}: looks like a secret ({f.kind}); never put credentials in code "
                       "(read them from configuration at runtime)")
    return out


def prepare_changes(output: CodegenOutput, target_root: Path, spec=None, *,
                    resolver: Resolver | None = None) -> Changes:
    """Validate + compute every change in memory (see module docstring). Raises EditRejected with ALL errors."""
    root = Path(target_root)
    build_tool = spec.target.build if spec is not None else ""
    errors: list[str] = []
    ch = Changes()
    total = 0
    for fo in output.files:
        try:
            resolved, norm = jail_path(root, fo.path)
        except EditRejected as e:
            errors.extend(e.errors)
            continue
        prob = build_file_problem(norm, build_tool)
        if prob:
            errors.append(prob)
            continue
        if norm in ch.files:
            cur: str | None = ch.files[norm]
        elif resolved.exists():
            if not resolved.is_file():
                errors.append(f"{norm}: exists and is not a regular file")
                continue
            if resolved.stat().st_size > MAX_FILE_BYTES:
                errors.append(f"{norm}: existing file exceeds {MAX_FILE_BYTES} bytes; cannot be edited")
                continue
            raw = resolved.read_bytes()
            if b"\x00" in raw[:8192]:
                errors.append(f"{norm}: binary file cannot be edited")
                continue
            cur = raw.decode("utf-8", errors="replace")
        else:
            cur = None
        if norm not in ch.old:
            ch.old[norm] = cur
        if fo.op == "create":
            if cur is not None:
                errors.append(f"{norm}: already exists; use op 'edit' (or 'replace' for a full rewrite)")
                continue
            new = fo.content or ""
        elif fo.op == "replace":
            if cur is None:
                errors.append(f"{norm}: does not exist; use op 'create'")
                continue
            new = fo.content or ""
        else:
            if cur is None:
                errors.append(f"{norm}: does not exist; use op 'create'")
                continue
            new, errs = apply_search_replace(cur, fo.edits, norm)
            if errs:
                errors.extend(errs)
                continue
        if "\x00" in new:
            errors.append(f"{norm}: NUL bytes are not allowed in generated files")
            continue
        size = len(new.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            errors.append(f"{norm}: {size} bytes exceeds the {MAX_FILE_BYTES}-byte file cap")
            continue
        total += size
        ch.files[norm] = new
    if total > MAX_TOTAL_BYTES:
        errors.append(f"output writes {total} bytes in total (cap {MAX_TOTAL_BYTES}); split the work")
    for norm, new in ch.files.items():
        errors.extend(_new_secret_lines(norm, ch.old.get(norm), new))
        derr, dwarn = check_dependencies(norm, ch.old.get(norm), new, spec, resolver=resolver)
        errors.extend(derr)
        ch.warnings.extend(dwarn)
    if errors:
        raise EditRejected(errors)
    return ch


def write_changes(target_root: Path, ch: Changes) -> list[str]:
    """Write validated changes (each file atomically). Returns the written relative paths."""
    from .state import atomic_write_text
    root = Path(target_root).resolve()
    for norm, text in sorted(ch.files.items()):
        p = (root / norm).resolve()
        if not p.is_relative_to(root):                     # re-check right before the write
            raise EditRejected([f"{norm}: escapes target_code/"])
        atomic_write_text(p, text)
    return ch.paths


def restore_files(target_root: Path, ch_old: dict[str, str | None]) -> None:
    """Undo a partially written change set (old text back, created files removed)."""
    from .state import atomic_write_text
    root = Path(target_root).resolve()
    for norm, text in ch_old.items():
        p = (root / norm).resolve()
        if not p.is_relative_to(root):
            continue
        if text is None:
            p.unlink(missing_ok=True)
        else:
            atomic_write_text(p, text)


# ------------------------------------------------------------------ prompts
def load_prompt(path: Path) -> tuple[str, str, str]:
    """(version, system, user template) from a prompt file (`<!-- prompt_version: X -->`, split at `<!-- user -->`)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"<!-- prompt_version: (\S+) -->", text)
    version = m.group(1) if m else "v1"
    system, _, user = text.partition("<!-- user -->")
    system = re.sub(r"<!-- prompt_version: \S+ -->\n?", "", system, count=1)
    return version, system.strip() + "\n", user.strip() + "\n"


def tokens(text: str) -> int:
    from ..analyze.chunker import _tokens
    return _tokens(len(text))


def cap_text(text: str, max_tokens: int) -> tuple[str, bool]:
    """Keep whole lines up to ~max_tokens (chunker estimate); mark the cut."""
    if tokens(text) <= max_tokens:
        return text, False
    budget = max(0, (max_tokens - 1) * 4 - 120)
    out, used = [], 0
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > budget:
            out.append(f"[... truncated: {len(lines) - i} more line(s) not shown to stay within the context cap ...]")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out), True


class ContextPack(_Strict):
    slice_id: str
    sections: dict[str, str] = Field(default_factory=dict)       # spec, legacy, interfaces, style, files, failures
    tokens: dict[str, int] = Field(default_factory=dict)
    truncated: list[str] = Field(default_factory=list)
    legacy_files: list[str] = Field(default_factory=list)         # code/-relative files quoted in `legacy`
    max_tokens: int = DEFAULT_PACK_TOKENS

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())


# share of the cap each section may take (legacy gets what is left)
SECTION_SHARE = {"spec": 0.30, "failures": 0.20, "style": 0.05, "interfaces": 0.15, "files": 0.05}


def _quote(tools, ev) -> str:
    q = (ev.quote or "").strip()
    if not q and ev.line_start:
        try:
            q = tools.read_file(ev.file, ev.line_start, ev.line_end or ev.line_start).content.strip()
        except Exception:  # noqa: BLE001
            q = ""
    q = " ".join(q.split())
    return q[:200] + ("..." if len(q) > 200 else "")


def _loc(ev) -> str:
    if ev.line_start:
        return f"{ev.file}:{ev.line_start}" + (f"-{ev.line_end}" if ev.line_end and ev.line_end != ev.line_start
                                              else "")
    return ev.file


def render_spec(slice_, graph, contract, *, tools=None, risks=None, resolutions=None, dep_status=None) -> str:
    by_id = {n.id: n for n in graph.nodes}
    mine = set(slice_.node_ids)
    lines = [f'Slice {slice_.id} "{slice_.name}" ({slice_.kind}), order {slice_.order}']
    if slice_.depends_on:
        lines.append("Depends on: " + ", ".join(f"{d} ({(dep_status or {}).get(d, '?')})" for d in slice_.depends_on))
    lines.append("Nodes:")
    for nid in slice_.node_ids:
        n = by_id.get(nid)
        if n is None:
            lines.append(f"- {nid} (not in the approved graph)")
            continue
        flags = [f for f, on in (("quirk", n.quirk), ("suspected bug", n.suspected_bug)) if on]
        lines.append(f"- {n.id} [{n.type}] {n.name}" + (f" ({', '.join(flags)})" if flags else ""))
        if n.responsibility:
            lines.append(f"  responsibility: {' '.join(n.responsibility.split())[:400]}")
        if n.evidence:
            lines.append("  defined at: " + ", ".join(_loc(e) for e in n.evidence[:4]))
    linked: set[str] = set()
    for e in graph.edges:
        if e.source in mine:
            linked.add(e.target)
        if e.target in mine:
            linked.add(e.source)
    rules = [by_id[i] for i in sorted(mine | linked) if i in by_id and by_id[i].type == "business_rule"]
    lines.append("Business rules (must be preserved):")
    if not rules:
        lines.append("- (none recorded in the graph)")
    for r in rules:
        where = "" if r.id in mine else " [rule owned by another slice; respect it]"
        lines.append(f"- {r.id} {r.name}{where}: {' '.join((r.responsibility or '').split())[:400]}")
        for ev in r.evidence[:3]:
            q = _quote(tools, ev) if tools is not None else (ev.quote or "")
            lines.append(f"  evidence {_loc(ev)}" + (f': "{q}"' if q else ""))
    decs = [d for d in contract.decisions if d.item_id in mine]
    lines.append("Quirk / suspected-bug decisions (approved contract):")
    if not decs:
        lines.append("- (none: preserve legacy behaviour)")
    for d in decs:
        kind = "suspected bug" if d.suspected_bug else "quirk"
        lines.append(f"- {d.item_id} {d.name} ({kind}): {d.decision.upper()}"
                     + (f" - {' '.join(d.note.split())[:300]}" if d.note else "")
                     + (" (preserve = keep the legacy behaviour exactly, even if it looks wrong)"
                        if d.decision == "preserve" else ""))
    lines.append("Target-fit risks and resolutions:")
    rs = [r for r in (risks or []) if r.id in set(slice_.risks)]
    if not rs:
        lines.append("- (none)")
    for r in rs:
        res = (resolutions or {}).get(r.id)
        lines.append(f"- {r.id} [{r.kind}, {r.severity}] {r.title}"
                     + (f"; handling: {' '.join(r.proposed_handling.split())[:300]}" if r.proposed_handling else "")
                     + (f"; resolution: {res.status} - {' '.join(res.note.split())[:300]}" if res else
                        "; resolution: (none yet)"))
    oq = [q for q in contract.open_questions + contract.assumptions if any(i in q for i in mine)]
    if oq:
        lines.append("Open questions / assumptions touching this slice:")
        lines += [f"- {' '.join(q.split())[:300]}" for q in oq[:20]]
    return "\n".join(lines)


def legacy_regions(code_dir: Path, slice_, graph, *, margin: int = LEGACY_MARGIN) -> tuple[str, list[str]]:
    """Legacy code for the slice from REDACTED code/ (never legacy_src/): evidence windows of every node
    (review/reanalyze._regions), whole files for small component files."""
    from ..review.reanalyze import _regions
    from ..sandbox.tools import ReadOnlyTools
    from ..graph.models import Evidence
    code_dir = Path(code_dir)
    if not code_dir.is_dir():
        return "", []
    tools = ReadOnlyTools(code_dir)
    by_id = {n.id: n for n in graph.nodes}
    evs = []
    for nid in slice_.node_ids:
        n = by_id.get(nid)
        if n is None:
            continue
        for ev in n.evidence:
            evs.append(ev)
            if n.type == "component":
                try:
                    st = tools.read_file(ev.file)
                except Exception:  # noqa: BLE001
                    continue
                if tokens(st.content) <= WHOLE_FILE_TOKENS:
                    evs.append(Evidence(file=ev.file, line_start=1, line_end=max(1, st.total_lines)))
    text = _regions(SimpleNamespace(evidence=evs), tools, margin)
    files = sorted({ev.file for ev in evs})
    return text, files


def slice_written_files(project, slice_id: str) -> list[str]:
    """target_code/ paths written by the slice's applied iterations (from iterations/K/edits.json)."""
    from .paths import Phase2Layout
    d = Phase2Layout(project).slice_iterations(slice_id)
    out: set[str] = set()
    if not d.is_dir():
        return []
    for f in sorted(d.glob("*/edits.json")):
        try:
            rec = json.loads(f.read_text())
        except ValueError:
            continue
        if rec.get("status") == "applied":
            out.update(rec.get("changed") or [])
    return sorted(out)


def interface_summaries(project, slice_, *, approved: set[str]) -> str:
    """Public signatures of files written by approved dependency slices (tree-sitter), never bodies."""
    from ..extract.treesitter import extract_file
    from ..ingest.secrets import redact_string
    from .paths import Phase2Layout
    root = Phase2Layout(project).target_code
    if not root.is_dir():
        return ""
    blocks = []
    for dep in slice_.depends_on:
        if dep not in approved:
            continue
        for rel in slice_written_files(project, dep):
            try:
                p, norm = jail_path(root, rel)
            except EditRejected:
                continue
            if not p.is_file():
                continue
            ff = extract_file(p, norm)
            if ff is None:
                blocks.append(f"{dep} {norm}: (no parser for this file type)")
                continue
            sig = [f"{dep} {norm} ({ff.language})"]
            sig += [f"  {c.kind} {c.name}" + (f"  @{', @'.join(c.decorators)}" if c.decorators else "")
                    for c in ff.classes]
            sig += [f"  def {f.qualified_name}({', '.join(f.params)})" for f in ff.functions]
            blocks.append("\n".join(sig))
    return redact_string("\n".join(blocks))


def render_style(spec) -> str:
    if spec is None:
        return "(no target.yaml)"
    t = spec.target
    lines = [f"Migration: {spec.kind}, strategy {spec.strategy}",
             f"Source: {spec.source.language} {spec.source.runtime} {spec.source.framework}".rstrip(),
             f"Target: {t.language} {t.runtime} {t.framework}".rstrip() + (f", build {t.build}" if t.build else "")]
    s = spec.style
    if s.formatter:
        lines.append(f"Formatter: {s.formatter}")
    if s.linters:
        lines.append(f"Linters: {', '.join(s.linters)}")
    if s.package_root:
        lines.append(f"Package root: {s.package_root}")
    lines += [f"Rule: {r}" for r in s.rules]
    if spec.libraries.allow:
        lines.append("Allowed libraries: " + ", ".join(spec.libraries.allow[:50]))
    if spec.libraries.deny:
        lines.append("Denied libraries: " + ", ".join(spec.libraries.deny[:50]))
    lines.append("Writable build files: " + (", ".join(sorted(BUILD_FILES.get(t.build.lower(), ()))) or "none"))
    return "\n".join(lines)


def target_file_list(project, cap: int = TARGET_FILES_CAP) -> str:
    from ..extract.treesitter import SKIP_DIRS
    from ..ingest.secrets import is_secret_filename
    from .paths import Phase2Layout
    root = Phase2Layout(project).target_code
    if not root.is_dir():
        return "(empty)"
    out: list[str] = []
    for dp, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d != ".git")
        for n in sorted(names):
            if is_secret_filename(n):
                continue
            out.append((Path(dp) / n).relative_to(root).as_posix())
    out.sort()
    more = len(out) - cap
    return "\n".join(out[:cap] + ([f"[... {more} more file(s) ...]"] if more > 0 else [])) or "(empty)"


def build_context_pack(project, slice_, *, graph, contract, spec=None, risks=None, resolutions=None,
                       dep_status: dict[str, str] | None = None, failures: str = "",
                       max_tokens: int = DEFAULT_PACK_TOKENS) -> ContextPack:
    from ..ingest.secrets import redact_string
    from ..sandbox.tools import ReadOnlyTools
    code = Path(project.code)
    tools = ReadOnlyTools(code) if code.is_dir() else None
    dep_status = dep_status or {}
    approved = {d for d, s in dep_status.items() if s == "approved"}
    raw = {
        "spec": render_spec(slice_, graph, contract, tools=tools, risks=risks, resolutions=resolutions,
                            dep_status=dep_status),
        "failures": redact_string(failures) if failures else "",
        "style": render_style(spec),
        "interfaces": interface_summaries(project, slice_, approved=approved) or "(none)",
        "files": target_file_list(project),
    }
    legacy, files = legacy_regions(code, slice_, graph)
    pack = ContextPack(slice_id=slice_.id, max_tokens=max_tokens, legacy_files=files)
    used = 0
    for name, share in SECTION_SHARE.items():
        text, cut = cap_text(raw[name], max(1, int(max_tokens * share)))
        pack.sections[name] = text
        pack.tokens[name] = tokens(text)
        used += pack.tokens[name]
        if cut:
            pack.truncated.append(name)
    text, cut = cap_text(legacy or "(no legacy code regions found in code/)", max(1, max_tokens - used))
    pack.sections["legacy"] = text
    pack.tokens["legacy"] = tokens(text)
    if cut:
        pack.truncated.append("legacy")
    return pack


def answer_needs(project, needs: list[str]) -> str:
    """Answer `need` requests: code/ (redacted legacy) by default, `target_code/<path>` for the new tree
    (redacted too: in_place trees are seeded from the unredacted legacy_src/)."""
    from ..analyze.chunker import LINE_FMT
    from ..ingest.secrets import redact_string
    from ..sandbox.tools import ReadOnlyTools
    from .paths import Phase2Layout
    blocks = []
    for n in needs[:MAX_NEED]:
        path, _, rng = n.rpartition(":")
        a, _, b = rng.partition("-")
        start, end = int(a), int(b)
        if end < start:
            start, end = end, start
        end = min(end, start + MAX_NEED_LINES - 1)
        root = Path(project.code)
        if path.startswith("target_code/"):
            root, path = Phase2Layout(project).target_code, path[len("target_code/"):]
        try:
            r = ReadOnlyTools(root).read_file(path, max(1, start), end)
        except Exception as e:  # noqa: BLE001
            blocks.append(f"=== {n} === unavailable: {type(e).__name__}: {str(e)[:120]}")
            continue
        body = [LINE_FMT.format(r.start + i, ln) for i, ln in enumerate(r.content.splitlines())]
        blocks.append("\n".join([f"=== {n} (lines {r.start}-{r.end}) ===", *body]))
    return redact_string("\n".join(blocks))


def render_prompt(template: str, pack: ContextPack, *, slice_id: str, iteration: int, node_ids: list[str],
                  need_answers: str = "", rejection: str = "") -> str:
    from ..analyze.module_analyzer import render
    s = pack.sections
    return render(template, SLICE_ID=slice_id, ITERATION=str(iteration), NODE_IDS=", ".join(node_ids),
                  SPEC=s.get("spec", ""), LEGACY=s.get("legacy", ""), INTERFACES=s.get("interfaces", ""),
                  STYLE=s.get("style", ""), TARGET_FILES=s.get("files", ""), FAILURES=s.get("failures", "") or
                  "(none)", NEED_ANSWERS=need_answers or "(none requested)", REJECTION=rejection or "(none)")


# ------------------------------------------------------------------ offline model
class OfflineCodegenLLM:
    """Deterministic, no-network stand-in for `codegen` and `code_review` (CI and `--offline`). It does not
    understand code: each iteration it writes one small, valid placeholder file per slice
    (`migration/SLICE-NNNN.md`), so the loop (apply, commit, build, tests, review) runs end to end."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @staticmethod
    def _field(prompt: str, name: str) -> str:
        m = re.search(rf"^{name}: (.*)$", prompt, re.M)
        return m.group(1).strip() if m else ""

    def complete(self, *, task, system, prompt, json_mode=True, prompt_version="v1"):
        from ..llm.base import LLMResponse
        self.calls.append({"task": task, "prompt_version": prompt_version})
        if task == "code_review":
            text = json.dumps({"findings": []})
        else:
            sid = self._field(prompt, "SLICE_ID") or "SLICE-0000"
            it = self._field(prompt, "ITERATION") or "1"
            nodes = [n.strip() for n in self._field(prompt, "NODE_IDS").split(",") if n.strip()]
            path = f"migration/{sid}.md"
            files_block = prompt.split("<<<TARGET_FILES_BEGIN>>>", 1)[-1].split("<<<TARGET_FILES_END>>>", 1)[0]
            exists = any(ln.strip() == path for ln in files_block.splitlines())
            content = (f"# {sid}\n\nIteration {it}: offline placeholder (no model was called).\n"
                       f"Covers: {', '.join(nodes) or '(none)'}\n")
            text = json.dumps({"files": [{"path": path, "op": "replace" if exists else "create", "content": content}],
                               "notes": "offline mode: placeholder only", "covers": nodes,
                               "assumptions": ["offline mode does not migrate code"], "need": []})
        return LLMResponse(text=text, model="offline", tokens_in=len(prompt) // 4, tokens_out=len(text) // 4)
