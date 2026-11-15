"""M2 target specification: `target init` and `target show` (docs/phase2-plan.md section 2).

`target init PROJECT` pre-fills `source` from facts/manifests.json (falling back to a scan of code/ when the
repository has no manifest), takes the target from flags, validates the result against
`migrate.models.TargetSpec` and writes target/target.yaml. It refuses to overwrite an existing spec without
--force and, like every Phase 2 command that writes, requires a passing contract check first.

    from migration_agent.migrate.target import detect_source, load_target
    det = detect_source(project)        # Detection(source=SourceStack, build="maven", notes=[...])
    spec = load_target(project)         # TargetSpec, or TargetSpecError with a readable message

Only code/ (the redacted copy) is read, through sandbox.tools.ReadOnlyTools; never legacy_src/.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import click

from .models import SourceStack, TargetSpec

FRAMEWORKS: dict[str, tuple[tuple[str, str], ...]] = {
    # ecosystem -> ((dependency glob, framework name), ...) in priority order
    "java": (("org.springframework.boot:*", "spring-boot"), ("io.quarkus:*", "quarkus"),
             ("io.micronaut:*", "micronaut"), ("org.springframework:spring-webmvc", "spring")),
    "javascript": (("@nestjs/core", "nestjs"), ("fastify", "fastify"), ("express", "express"), ("koa", "koa"),
                   ("@hapi/hapi", "hapi")),
    "python": (("django", "django"), ("flask", "flask"), ("fastapi", "fastapi")),
    "go": (("github.com/gin-gonic/gin", "gin"), ("github.com/labstack/echo*", "echo"),
           ("github.com/go-chi/chi*", "chi"), ("github.com/gofiber/fiber*", "fiber")),
}
# code-scan fallbacks: (language, regex, framework)
_CODE_FRAMEWORKS = (
    ("python", r"^\s*(from|import)\s+django\b", "django"),
    ("python", r"^\s*(from|import)\s+flask\b", "flask"),
    ("python", r"^\s*(from|import)\s+fastapi\b", "fastapi"),
    ("javascript", r"""require\(\s*['"]express['"]\s*\)|from\s+['"]express['"]""", "express"),
    ("javascript", r"""require\(\s*['"]fastify['"]\s*\)|from\s+['"]fastify['"]""", "fastify"),
    ("java", r"\borg\.springframework\.boot\b", "spring-boot"),
)
_EXT_LANG = {".java": "java", ".kt": "kotlin", ".py": "python", ".js": "javascript", ".mjs": "javascript",
             ".cjs": "javascript", ".ts": "typescript", ".go": "go", ".rb": "ruby", ".cs": "csharp", ".php": "php"}
_ECO_LANG = {"maven": "java", "gradle": "java", "npm": "javascript", "pip": "python", "python": "python",
             "go": "go", "cargo": "rust", "nuget": "csharp"}
_ECO_BUILD = {"maven": "maven", "gradle": "gradle", "npm": "npm", "pip": "pip", "python": "pip", "go": "go-mod",
              "cargo": "cargo", "nuget": "dotnet"}
_ECO_PRIORITY = ("maven", "gradle", "npm", "python", "pip", "go", "cargo", "nuget")
_LANG_BUILD = {"java": "maven", "kotlin": "gradle", "javascript": "npm", "typescript": "npm", "python": "pip",
               "go": "go-mod", "rust": "cargo", "csharp": "dotnet"}
_SKIP = {".git", "node_modules", "target", "build", "dist", ".venv", "venv", "vendor", "__pycache__"}


class TargetSpecError(ValueError):
    """target/target.yaml missing or invalid; str(e) is a readable, multi-line message."""


@dataclass
class Detection:
    source: SourceStack
    build: str = ""
    notes: list[str] = field(default_factory=list)     # where each value came from (shown by `target init`)


# ------------------------------------------------------------------ version helpers
def major_minor(spec: str | None) -> str:
    """'^4.18.2' -> '4.18', '==3.2.18' -> '3.2', '>=3.2,<4' -> '3.2', '2.7.18' -> '2.7', '5' -> '5'."""
    m = re.search(r"(\d+)(?:\.(\d+))?", spec or "")
    if not m or "${" in (spec or ""):
        return ""
    return m.group(1) + (f".{m.group(2)}" if m.group(2) is not None else "")


def java_version(v: str | None) -> str:
    """'1.8' / 'JavaVersion.VERSION_1_8' -> '8'; '17' / 'VERSION_17' -> '17'; '${java.version}' -> ''."""
    s = (v or "").strip()
    if not s or "${" in s:
        return ""
    s = s.split("VERSION_", 1)[-1].replace("_", ".")
    m = re.match(r"^1\.(\d+)", s)
    if m:
        return m.group(1)
    m = re.match(r"^(\d+)", s)
    return m.group(1) if m else ""


def _fw(name: str, ver: str) -> str:
    return f"{name}@{ver}" if ver else name


# ------------------------------------------------------------------ detection
def _manifests(project) -> list[dict]:
    p = Path(project.facts) / "manifests.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    ms = data.get("manifests") if isinstance(data, dict) else None
    return [m for m in ms or [] if isinstance(m, dict) and not m.get("parse_error")]


def _tools(project):
    from ..sandbox.tools import ReadOnlyTools
    code = Path(project.code)
    return ReadOnlyTools(code) if code.is_dir() else None


def _dep_version(deps: list[dict], glob: str) -> tuple[bool, str]:
    from .knowledge import dep_glob_match
    for d in deps:
        if dep_glob_match(glob, str(d.get("name", ""))):
            return True, major_minor(d.get("version"))
    return False, ""


def _primary(ms: list[dict]) -> dict | None:
    """Root-level manifest of the highest-priority ecosystem, else the shallowest one."""
    def rank(m):
        eco = m.get("ecosystem", "")
        return (str(m.get("path", "")).count("/"),
                _ECO_PRIORITY.index(eco) if eco in _ECO_PRIORITY else len(_ECO_PRIORITY), str(m.get("path")))
    ms = [m for m in ms if m.get("ecosystem") in _ECO_LANG]
    return min(ms, key=rank) if ms else None


def _java_runtime(m: dict) -> tuple[str, str]:
    rt = m.get("runtime") or {}
    for key in ("maven.compiler.release", "java.version", "maven.compiler.source", "maven.compiler.target",
                "java.toolchain", "sourceCompatibility", "targetCompatibility", "jvmTarget"):
        v = java_version(rt.get(key))
        if v:
            return v, key
    return "", ""


def _gradle_boot_version(project, path: str) -> str:
    tools = _tools(project)
    if tools is None:
        return ""
    try:
        text = tools.read_file(path).content
    except Exception:  # noqa: BLE001 - unreadable build file: version stays unknown
        return ""
    m = (re.search(r"""org\.springframework\.boot['"]\s*\)?\s*version\s*['"]([\d.]+)""", text)
         or re.search(r"spring-boot-gradle-plugin:([\d.]+)", text))
    return major_minor(m.group(1)) if m else ""


def _scan_code(project) -> tuple[str, str, list[str]]:
    """(language, framework, notes) from file extensions and import statements in code/."""
    code = Path(project.code)
    counts: dict[str, int] = {}
    if code.is_dir():
        for dirpath, dirnames, filenames in os.walk(code):
            dirnames[:] = [d for d in dirnames if d not in _SKIP]
            for fn in filenames:
                lang = _EXT_LANG.get(Path(fn).suffix.lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return "", "", []
    lang = max(sorted(counts), key=lambda k: counts[k])
    notes = [f"language {lang}: {counts[lang]} source file(s) in code/ (no manifest)"]
    tools = _tools(project)
    base = {"typescript": "javascript", "kotlin": "java"}.get(lang, lang)
    for clang, rx, fw in _CODE_FRAMEWORKS:
        if clang != base or tools is None:
            continue
        try:
            hit = tools.grep(rx).matches
        except Exception:  # noqa: BLE001
            hit = []
        if hit:
            notes.append(f"framework {fw}: import in {hit[0].path}:{hit[0].line} (version unknown)")
            return lang, fw, notes
    return lang, "", notes


def detect_source(project) -> Detection:
    """Best-effort SourceStack from facts/manifests.json, then code/ (language by extension, framework imports)."""
    ms = _manifests(project)
    m = _primary(ms)
    notes: list[str] = []
    if m is None:
        lang, fw, notes = _scan_code(project)
        return Detection(source=SourceStack(language=lang or "unknown", framework=fw),
                         build=_LANG_BUILD.get(lang, ""), notes=notes or ["nothing detected: set --source-* flags"])
    eco, path = m["ecosystem"], m.get("path", "")
    deps = [d for mm in ms if mm.get("ecosystem") == eco for d in (mm.get("dependencies") or [])]
    lang = _ECO_LANG[eco]
    build = _ECO_BUILD.get(eco, "")
    runtime, framework = "", ""
    rt = m.get("runtime") or {}
    notes.append(f"build {build}: {path}")
    if lang == "java":
        if path.endswith(".kts") or _dep_version(deps, "org.jetbrains.kotlin:kotlin-stdlib*")[0]:
            lang = "kotlin"
        runtime, key = _java_runtime(m)
        if runtime:
            notes.append(f"runtime {runtime}: {path} {key}={rt.get(key)}")
        parent = rt.get("parent", "")
        if parent.startswith("org.springframework.boot:"):
            framework = _fw("spring-boot", major_minor(parent.rsplit(":", 1)[-1]))
            notes.append(f"framework {framework}: {path} parent {parent}")
    elif lang == "javascript":
        if _dep_version(deps, "typescript")[0]:
            lang = "typescript"
        node = rt.get("engines.node", "")
        runtime = major_minor(node)
        if runtime:
            notes.append(f"runtime node {runtime}: {path} engines.node={node}")
        tools = _tools(project)
        if tools is not None:
            for lock, b in (("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm")):
                try:
                    tools.file_stat(lock)
                    build = b
                    break
                except Exception:  # noqa: BLE001 - lockfile absent
                    continue
    elif lang == "python":
        py = rt.get("requires-python") or rt.get("python") or ""
        runtime = major_minor(py)
        if runtime:
            notes.append(f"runtime python {runtime}: {path} {py}")
        if eco == "python" and "python" in rt:
            build = "poetry"
    elif lang == "go":
        runtime = major_minor(rt.get("go"))
        if runtime:
            notes.append(f"runtime go {runtime}: {path} go directive")
    if not framework:
        for glob, name in FRAMEWORKS.get({"kotlin": "java", "typescript": "javascript"}.get(lang, lang), ()):
            found, ver = _dep_version(deps, glob)
            if found:
                if not ver and eco == "gradle" and name == "spring-boot":
                    ver = _gradle_boot_version(project, path)
                framework = _fw(name, ver)
                notes.append(f"framework {framework}: dependency {glob} in {eco} manifest(s)")
                break
    if not framework:
        _, fw, more = _scan_code(project)
        if fw:
            framework = fw
            notes.extend(n for n in more if n.startswith("framework"))
    return Detection(source=SourceStack(language=lang, runtime=runtime, framework=framework), build=build,
                     notes=notes)


# ------------------------------------------------------------------ spec building / loading
def infer_kind(source: SourceStack, language: str, framework: str) -> str:
    from .knowledge import norm_language, split_framework
    if norm_language(language) != norm_language(source.language):
        return "language_move"
    a, b = split_framework(source.framework)[0], split_framework(framework)[0]
    if b and a and a != b:
        return "framework_move"
    return "version_upgrade"


def build_spec(det: Detection, *, kind: str | None = None, language: str | None = None,
               runtime: str | None = None, framework: str | None = None, build: str | None = None,
               strategy: str | None = None, budget_usd: float | None = None, base_image: str | None = None,
               source_language: str | None = None, source_runtime: str | None = None,
               source_framework: str | None = None) -> TargetSpec:
    """A validated TargetSpec from a detection plus flags (pydantic ValidationError on invalid values)."""
    src = det.source.model_copy(update={k: v for k, v in (("language", source_language),
                                                          ("runtime", source_runtime),
                                                          ("framework", source_framework)) if v is not None})
    lang = language if language is not None else src.language
    fw = framework if framework is not None else ""
    kind = kind or infer_kind(src, lang, fw)
    if build is None:
        build = det.build if lang == src.language and det.build else _LANG_BUILD.get(lang, "")
    if strategy is None:
        strategy = "in_place" if kind == "version_upgrade" else "strangler"
    data = {"kind": kind, "source": src.model_dump(),
            "target": {"language": lang, "runtime": runtime or "", "framework": fw, "build": build,
                       "base_image": base_image or ""},
            "strategy": strategy, "budget_usd": budget_usd}
    return TargetSpec.model_validate(data)


def format_validation_error(e, where: str = "target.yaml") -> str:
    """pydantic ValidationError -> '<where> is invalid:' plus one '  - field.path: message' line per error."""
    lines = [f"{where} is invalid:"]
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid")
        if err.get("type") == "extra_forbidden":
            msg = "unknown field (remove it or check the spelling)"
        elif err.get("type") == "missing":
            msg = "required field is missing"
        elif err.get("type") == "literal_error" and err.get("ctx", {}).get("expected"):
            msg = f"must be one of {err['ctx']['expected']}"
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def parse_target_text(text: str, where: str = "target.yaml") -> TargetSpec:
    import yaml
    from pydantic import ValidationError
    try:
        return TargetSpec.from_yaml(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        pos = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        raise TargetSpecError(f"{where} is not valid YAML{pos}: {getattr(e, 'problem', None) or e}") from e
    except ValidationError as e:
        raise TargetSpecError(format_validation_error(e, where)) from e
    except ValueError as e:
        raise TargetSpecError(f"{where} is invalid: {e}") from e


def load_target(project) -> TargetSpec:
    """The validated target/target.yaml, or TargetSpecError (missing / invalid, with a readable message)."""
    from .paths import Phase2Layout
    p = Phase2Layout(project).target_yaml
    if not p.exists():
        raise TargetSpecError(f"no target spec at {p}: run `migration-agent target init {Path(project.root)}`")
    return parse_target_text(p.read_text(encoding="utf-8"), "target/target.yaml")


def _one_line(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", s)[:300]


def write_target(project, spec: TargetSpec, *, force: bool = False, notes: list[str] | None = None) -> Path:
    """Write target/target.yaml (FileExistsError unless force). Validates by round-tripping first."""
    from .paths import Phase2Layout
    from .state import atomic_write_text, now_iso
    lay = Phase2Layout(project)
    if lay.target_yaml.exists() and not force:
        raise FileExistsError(f"{lay.target_yaml} already exists (use --force to overwrite)")
    body = spec.to_yaml()
    parse_target_text(body)            # never write something `target show` would reject
    head = [f"# target/target.yaml, written by `migration-agent target init` at {now_iso()}.",
            "# Edit freely; `migration-agent target show` validates it. Hashed into every later artifact."]
    head += [f"# detected: {_one_line(n)}" for n in (notes or [])]
    lay.ensure(lay.target_dir)
    atomic_write_text(lay.target_yaml, "\n".join(head) + "\n" + body)
    return lay.target_yaml


def _audit(project, action: str, **fields) -> None:
    from ..audit import AuditLog
    try:
        AuditLog(Path(project.audit_dir) / "audit.jsonl").record("target", action, **fields)
    except OSError:
        pass


# ------------------------------------------------------------------ CLI
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def require_gate(project, pin: bool = False, accept_unmapped: bool = False):
    """require_contract as a ClickException (shared by target/fit commands)."""
    from .contract import ContractError, require_contract
    try:
        return require_contract(project, pin=pin, accept_unmapped=accept_unmapped)
    except ContractError as e:
        raise click.ClickException(f"{e}\n(run `migration-agent contract check {project.root}` for details)")


def register(groups) -> None:
    @groups.target.command("init")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--kind", type=click.Choice(["version_upgrade", "framework_move", "language_move"]), default=None,
                  help="Migration kind (default: inferred from source vs target).")
    @click.option("--language", default=None, help="Target language (default: the source language).")
    @click.option("--runtime", default=None, help='Target runtime version, e.g. "21", "3.12", "1.22".')
    @click.option("--framework", default=None, help='Target framework, e.g. "spring-boot@3.3", "fastify@4".')
    @click.option("--build", default=None, help="Target build tool (maven, gradle, pip, poetry, npm, go-mod ...).")
    @click.option("--base-image", default=None, help="Target base image, pinned by digest (name@sha256:...).")
    @click.option("--strategy", type=click.Choice(["strangler", "big_bang", "in_place"]), default=None,
                  help="Default: in_place for version upgrades, strangler otherwise.")
    @click.option("--budget-usd", type=float, default=None, help="Project-level model budget across sessions.")
    @click.option("--source-language", default=None, help="Override the detected source language.")
    @click.option("--source-runtime", default=None, help="Override the detected source runtime.")
    @click.option("--source-framework", default=None, help='Override the detected source framework ("name@x.y").')
    @click.option("--force", is_flag=True, help="Overwrite an existing target/target.yaml.")
    @click.option("--pin-contract", "pin", is_flag=True, help="Accept a contract pinned to an older graph version.")
    @click.option("--accept-unmapped", is_flag=True, help="Accept unmapped inventory items in the contract.")
    def target_init(project: Path, kind, language, runtime, framework, build, base_image, strategy, budget_usd,
                    source_language, source_runtime, source_framework, force, pin, accept_unmapped) -> None:
        """Write target/target.yaml: source detected from the manifests, target from the flags."""
        from pydantic import ValidationError

        from .paths import Phase2Layout
        p = _project(project)
        require_gate(p, pin=pin, accept_unmapped=accept_unmapped)
        lay = Phase2Layout(p)
        if lay.target_yaml.exists() and not force:
            raise click.ClickException(f"{lay.target_yaml} already exists; edit it, or pass --force to overwrite")
        det = detect_source(p)
        try:
            spec = build_spec(det, kind=kind, language=language, runtime=runtime, framework=framework, build=build,
                              strategy=strategy, budget_usd=budget_usd, base_image=base_image,
                              source_language=source_language, source_runtime=source_runtime,
                              source_framework=source_framework)
        except ValidationError as e:
            raise click.ClickException(format_validation_error(e, "target spec from the flags"))
        out = write_target(p, spec, force=force, notes=det.notes)
        _audit(p, "target_init", kind=spec.kind, source=spec.source.model_dump(), target=spec.target.model_dump(),
               forced=force)
        for n in det.notes:
            click.echo(f"detected: {n}")
        if spec.source.language == spec.target.language and not spec.target.runtime and not spec.target.framework:
            click.echo("warning: no target runtime/framework given; edit target.yaml before `target fit`")
        click.echo(f"written: {out}")
        click.echo(spec.to_yaml().rstrip())

    @groups.target.command("show")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--format", "fmt", type=click.Choice(["yaml", "json"]), default="yaml")
    def target_show(project: Path, fmt: str) -> None:
        """Validate and print target/target.yaml (exit 1 with readable errors if it is invalid)."""
        from .paths import Phase2Layout
        from .state import sha256_file
        p = _project(project)
        try:
            spec = load_target(p)
        except TargetSpecError as e:
            raise click.ClickException(str(e))
        if fmt == "json":
            click.echo(spec.model_dump_json(indent=2))
            return
        click.echo(f"# {Phase2Layout(p).target_yaml} (sha256 {sha256_file(Phase2Layout(p).target_yaml)[:12]})")
        click.echo(spec.to_yaml().rstrip())
