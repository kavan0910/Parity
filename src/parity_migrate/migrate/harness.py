"""Sandbox harness for the legacy (and later new / composite) service: `harness import-source|build|up|down|status`.

Per-project spec `harness/harness.yaml` (falls back to `legacy_src/harness/harness.yaml` or `harness.yaml.in`,
copied into the project on first build). Eval fixtures ship `eval/repos/<repo>/harness/harness.yaml.in`: harness
files that Phase 1 would otherwise analyse as part of the legacy service (YAML config, .py, .sql) carry a `.in`
suffix so adding a harness never changes the Phase 1 graph or eval scores.

    schema_version: 1
    name: express-crud
    legacy:
      image: ma-fixture/express-crud:legacy     # tag built from `dockerfile` (docker build --network none)
      dockerfile: harness/Dockerfile            # relative to the service source tree, else to <project>/harness/
      context: harness
      workdir: /work/app                        # where the writable workspace copy is mounted
      build:                                    # phase "build" exec job in the workspace copy
        command: [sh, -c, "npm install --no-audit --no-fund"]
        network: mirror                         # mirror hosts from harness/mirror.json / MA_MIRROR_HOSTS
        cache: harness/cache/npm                # optional project-relative cache mounted at /cache
        timeout_s: 1800
      start: [node, src/server.js]              # phase "test" service, internal network only
      port: 3000
      healthcheck: {path: /api/products, command: [node, -e, "..."]}   # {port} and {path} are substituted
      env: {PORT: "3000"}
      synthetic_secrets: [API_TOKEN]            # fresh random value per `up`, shared by name across services
    db:
      engine: sqlite | postgres | mysql | none
      image: postgres:16.4-alpine               # server engines only
      command: [docker-entrypoint.sh, postgres]
      tmpfs: [/var/run/postgresql]
      migrate: [python, manage.py, migrate, --noinput]     # run in the service image before start
      seed: [[python, manage.py, loaddata, harness/seed/seed.json]]
    stubs: [{name: payments, image: ..., command: [...], port: 8080}]
    new:                                        # optional (M6): the service in target_code/, same fields as legacy.
      ...                                       # Missing: derived from target/target.yaml (default_new_service):
                                                # build tool defaults, harness/new/Dockerfile from target.base_image
    new_db: {migrate: [...], seed: [[...]]}     # optional: migrate/seed for the NEW service when the DB is private
                                                # to each service (engine sqlite/none). Server engines are migrated
                                                # once with the LEGACY image (legacy schema, decision P2-Q10)
    router: {image: "ma-router:1", port: 8080}  # strangler router (image built from migrate/router/)
    schema_check:                               # `config schema-check` (migrate/schemacompat.py)
      sqlite_path: db.sqlite3                   # workspace-relative SQLite file (engine sqlite)
      new_sqlite_path: db.sqlite3               # where the new service expects it (default: sqlite_path)
      sql_files: []                             # legacy SQL migrations to apply when db.migrate is empty
                                                # (default: Flyway-style .sql files found by Phase 1 extract)
      orm_check: []                             # argv override for the new-service ORM check
      orm_env: {}

Roles: `legacy`, `new` (target_code/), `composite` (legacy + new + router on one internal network, one shared DB
for server engines; with sqlite/none each service keeps its own private DB, recorded as `db_shared: false`).
Strangler routes: `harness/routes.json` is generated from plan/migration_plan.json, the approved graph snapshot
(endpoint nodes) and `harness/flips.json`; `harness flip|unflip PROJECT SLICE-NNNN` manage flips (approved slices
only, `slices/SLICE-NNNN/approval.json`, unless `--force-for-testing`, audited). The router container mounts
`harness/router/live/` read-only and reloads routes.json on change, so a flip applies without a restart.
`service_urls(project)` returns in-network base URLs (`http://legacy:3000`, `http://router:8080`) of running roles.
`run_cases_in_network(project, role, cases)` runs characterization cases against a running role from inside the
sandbox: one test-phase runner job (image RUNNER_IMAGE = python:3.12.7-alpine3.20, the router's pinned base, never
pulled) attached to the role's internal network, with the stdlib-only runner (harness_runner/wire.py +
netrunner.py) and the cases staged read-only under runs/RUN-NNNN/work/{runner,cases}/; results come back as
runs/RUN-NNNN/reports/results.json and are parsed into harness_runner CaseResults on the host. Used by
`tests record --harness`, `tests run --harness --role`, `equivalence` without URLs, `equivalence composite` and the
generation loop's characterization suite. `harness flip|unflip` run the composite suite afterwards when the
composite is up (`--no-verify` skips it).

Files written: harness/harness.yaml (copied spec), harness/state.json, harness/compose.<role>.yml (generated, for
inspection or `docker compose`), harness/legacy_src.json (import manifest), harness/logs/<role>-image.log,
harness/synthetic_secrets.json (0600, random values for the running harness, never real credentials),
harness/new/Dockerfile (default new-service image), harness/router/ (router build context + live/routes.json),
harness/routes.json, harness/flips.json.

`legacy_src/` is the ORIGINAL, unredacted source pinned by the contract `source_hash`. It is mounted only into
exec containers; agent tools (`sandbox/tools.ReadOnlyTools`) are rooted at `code/` and cannot reach it.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Limits

Role = Literal["legacy", "new", "composite"]
_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
                        ".pytest_cache"})


class HarnessError(RuntimeError):
    pass


# ------------------------------------------------------------------ spec
class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _argv(v: list[str]) -> list[str]:
    if not v or not all(isinstance(a, str) and a and "\x00" not in a for a in v):
        raise ValueError("must be a non-empty argv list of strings")
    return v


def _relpath(v: str) -> str:
    s = v.replace("\\", "/")
    if s.startswith("/") or ".." in s.split("/") or (len(s) > 1 and s[1] == ":"):
        raise ValueError(f"path {v!r} must be relative without '..'")
    return s


class BuildSpec(_M):
    command: list[str]
    network: Literal["none", "mirror"] = "mirror"
    cache: str | None = None
    timeout_s: int = Field(default=1800, ge=1)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _c(cls, v):
        return _argv(v)

    @field_validator("cache")
    @classmethod
    def _cache(cls, v):
        if v is None:
            return v
        v = _relpath(v)
        if not v.startswith("harness/cache/"):
            raise ValueError("cache must live under harness/cache/")
        return v


class HealthSpec(_M):
    path: str = "/"
    command: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=180, ge=1)
    interval_s: float = Field(default=2.0, gt=0)


class ServiceSpec(_M):
    image: str = Field(min_length=1)
    dockerfile: str | None = "harness/Dockerfile"
    context: str = "harness"
    workdir: str = "/work/app"
    build: BuildSpec
    start: list[str]
    port: int = Field(ge=1, le=65535)
    healthcheck: HealthSpec = Field(default_factory=HealthSpec)
    env: dict[str, str] = Field(default_factory=dict)
    synthetic_secrets: list[str] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    # Headers the in-network case runner adds to EVERY request it sends to this service (recording, equivalence,
    # seeds, composite), e.g. {"X-API-Token": "{secret:API_TOKEN}"}. `{secret:NAME}` is replaced with the random
    # value generated for NAME at `harness up` (NAME must be in synthetic_secrets); the value never reaches a case
    # file or a report. Without this an authenticated service answers 401 to everything and nothing is tested.
    request_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("request_headers")
    @classmethod
    def _rh(cls, v):
        for k, val in v.items():
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", str(k)):
                raise ValueError(f"invalid header name {k!r}")
            if len(str(val)) > 500 or re.search(r"[\r\n\x00]", str(val)):
                raise ValueError(f"invalid value for header {k!r}")
        return v

    @field_validator("start")
    @classmethod
    def _s(cls, v):
        return _argv(v)

    @field_validator("context")
    @classmethod
    def _ctx(cls, v):
        return _relpath(v)

    @field_validator("dockerfile")
    @classmethod
    def _df(cls, v):
        return None if v is None else _relpath(v)


class DbSpec(_M):
    engine: Literal["none", "sqlite", "postgres", "mysql"] = "none"
    image: str = ""
    command: list[str] = Field(default_factory=list)
    port: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    synthetic_secrets: list[str] = Field(default_factory=list)
    tmpfs: list[str] = Field(default_factory=list)
    healthcheck: list[str] = Field(default_factory=list)
    migrate: list[str] = Field(default_factory=list)
    seed: list[list[str]] = Field(default_factory=list)


class StubSpec(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    image: str = Field(min_length=1)
    command: list[str]
    port: int = Field(ge=1, le=65535)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _c(cls, v):
        return _argv(v)


class NewDbSpec(_M):
    migrate: list[str] = Field(default_factory=list)
    seed: list[list[str]] = Field(default_factory=list)


class RouterSpec(_M):
    image: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9._/-]{0,127}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?)?$")
    port: int = Field(default=8080, ge=1, le=65535)
    limits: Limits = Field(default_factory=lambda: Limits(cpus=0.5, memory_mb=256, pids=64, timeout_s=900,
                                                          disk_mb=64))

    @property
    def image_tag(self) -> str:
        from .router import ROUTER_VERSION
        return self.image or f"ma-router:{ROUTER_VERSION}"


class SchemaCheckSpec(_M):
    sqlite_path: str = "db.sqlite3"
    new_sqlite_path: str | None = None
    sql_files: list[str] = Field(default_factory=list)
    orm_check: list[str] = Field(default_factory=list)
    orm_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("sqlite_path", "new_sqlite_path")
    @classmethod
    def _p(cls, v):
        return None if v is None else _relpath(v)

    @field_validator("sql_files")
    @classmethod
    def _files(cls, v):
        return [_relpath(x) for x in v]


class HarnessSpec(_M):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    legacy: ServiceSpec
    new: ServiceSpec | None = None
    new_db: NewDbSpec | None = None
    db: DbSpec = Field(default_factory=DbSpec)
    stubs: list[StubSpec] = Field(default_factory=list)
    router: RouterSpec = Field(default_factory=RouterSpec)
    schema_check: SchemaCheckSpec = Field(default_factory=SchemaCheckSpec)
    seed_requests: str | None = None     # HTTP seed file (source-relative) for the M3 recorder, e.g. in-memory DBs

    @field_validator("seed_requests")
    @classmethod
    def _sr(cls, v):
        return None if v is None else _relpath(v)

    def service(self, role: str) -> ServiceSpec:
        """The service declared in harness.yaml. `resolve_service` adds target.yaml defaults for `new`."""
        if role == "composite":
            raise HarnessError("composite mode (M6 strangler router) is legacy + new behind the router, not a "
                               "single service")
        svc = self.legacy if role == "legacy" else self.new
        if svc is None:
            raise HarnessError("harness.yaml has no `new:` service (the new service is generated in M4); "
                               "`harness build --new` derives one from target/target.yaml")
        return svc

    @property
    def shared_db(self) -> bool:
        return self.db.engine in ("postgres", "mysql")


# ------------------------------------------------------------------ new-service defaults (target.yaml)
# build tool -> (default base image, build argv, start argv, cache dir, extra env). Images are not pinned here:
# set target.base_image (pinned by digest) or a `new:` section in harness.yaml for anything real.
_BUILD_DEFAULTS: dict[str, dict] = {
    "npm": {"image": "node:20.18.0-bookworm", "cache": "harness/cache/npm",
            "build": ["sh", "-c", "npm ci --no-audit --no-fund --cache /cache --prefer-offline"],
            "start": ["npm", "start", "--silent"], "env": {}},
    "maven": {"image": "maven:3.9.9-eclipse-temurin-21", "cache": "harness/cache/m2",
              "build": ["mvn", "-B", "-q", "-Dmaven.repo.local=/cache/m2", "-DskipTests", "package"],
              "start": ["sh", "-c", "exec java -jar $(ls target/*.jar | grep -v original | head -n 1)"], "env": {}},
    "gradle": {"image": "gradle:8.10.2-jdk21", "cache": "harness/cache/gradle",
               "build": ["gradle", "--no-daemon", "-q", "-g", "/cache/gradle", "build", "-x", "test"],
               "start": ["sh", "-c", "exec java -jar $(ls build/libs/*.jar | grep -v plain | head -n 1)"],
               "env": {}},
    "pip": {"image": "python:3.12.7-slim-bookworm", "cache": "harness/cache/pip",
            "build": ["pip", "install", "--no-cache-dir", "--cache-dir", "/cache", "--target", "/work/app/.deps",
                      "-r", "requirements.txt"],
            "start": ["python", "-m", "service"], "env": {"PYTHONPATH": "/work/app/.deps"}},
    "go-mod": {"image": "golang:1.22.8-bookworm", "cache": "harness/cache/go",
               "build": ["go", "build", "-o", "bin/service", "."],
               "start": ["./bin/service"], "env": {"GOMODCACHE": "/cache/mod", "GOCACHE": "/cache/build",
                                                   "GOFLAGS": "-mod=mod"}},
}
_LANG_TOOL = {"javascript": "npm", "typescript": "npm", "java": "maven", "kotlin": "gradle", "python": "pip",
              "go": "go-mod"}
_BASE_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
_HEALTH_DEFAULTS = {
    "javascript": ["node", "-e", "require('http').get('http://127.0.0.1:{port}{path}', r => process.exit("
                   "r.statusCode < 500 ? 0 : 1)).on('error', () => process.exit(1))"],
    "python": ["python", "-c", "import sys,urllib.request,urllib.error\ntry:\n urllib.request.urlopen("
               "'http://127.0.0.1:{port}{path}',timeout=5)\nexcept urllib.error.HTTPError as e:\n sys.exit(e.code>=500)"
               "\nexcept Exception:\n sys.exit(1)"],
    "tcp": ["bash", "-c", 'exec 3<>/dev/tcp/127.0.0.1/{port} && printf "GET {path} HTTP/1.0\\r\\n\\r\\n" >&3 '
            '&& head -n 1 <&3 | grep -q " [1-4][0-9][0-9] "'],
}


def _tag_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._") or "service"
    return s[:60]


def default_new_service(spec: HarnessSpec, target) -> ServiceSpec:
    """A `new:` service derived from target.yaml (`TargetSpec`): build-tool defaults, legacy port/env/secrets."""
    from .knowledge import norm_language, split_framework
    lang = norm_language(target.target.language)
    raw_lang = (target.target.language or "").lower()
    tool = (target.target.build or _LANG_TOOL.get(raw_lang) or _LANG_TOOL.get(lang) or "").lower()
    if tool == "poetry":
        tool = "pip"
    d = _BUILD_DEFAULTS.get(tool)
    if d is None:
        raise HarnessError(f"no harness defaults for build tool {tool or '?'!r} (target.yaml target.build); add a "
                           "`new:` section to harness/harness.yaml")
    leg = spec.legacy
    port = (target.infra.port if target.infra and target.infra.port else None) or leg.port
    start = list(d["start"])
    fw = split_framework(target.target.framework or "")[0]
    if tool == "pip" and fw == "django":
        start = ["python", "manage.py", "runserver", f"0.0.0.0:{port}", "--noreload"]
    hc_kind = "javascript" if lang == "javascript" else "python" if lang == "python" else "tcp"
    env = {**{k: v for k, v in leg.env.items()}, **d["env"]}
    if "PORT" in env:
        env["PORT"] = str(port)
    return ServiceSpec(
        image=f"ma-{_tag_name(spec.name)}:new", dockerfile="new/Dockerfile", context="new", workdir="/work/app",
        build=BuildSpec(command=d["build"], network="mirror", cache=d["cache"], timeout_s=leg.build.timeout_s),
        start=start, port=port,
        healthcheck=HealthSpec(path=leg.healthcheck.path, command=_HEALTH_DEFAULTS[hc_kind],
                               timeout_s=leg.healthcheck.timeout_s, interval_s=leg.healthcheck.interval_s),
        env=env, synthetic_secrets=list(leg.synthetic_secrets), limits=leg.limits,
        request_headers=dict(leg.request_headers))


def default_new_dockerfile(target) -> str:
    """Dockerfile text for the default new-service image (no RUN steps: built with --network none)."""
    lang = (target.target.language or "").lower()
    tool = (target.target.build or _LANG_TOOL.get(lang) or "").lower()
    tool = "pip" if tool == "poetry" else tool
    base = target.target.base_image or (_BUILD_DEFAULTS.get(tool) or {}).get("image", "")
    if not base or not _BASE_IMAGE.match(base):
        raise HarnessError(f"invalid or missing base image {base!r} (target.yaml target.base_image)")
    pin = "" if "@sha256:" in base else ("# TODO(M6): pin the base image by digest (target.yaml target.base_image: "
                                          "name@sha256:...)\n")
    return ("# GENERATED by `parity harness build --new` from target/target.yaml (edit freely; it is not\n"
            "# overwritten once it exists). Built with `docker build --network none --pull=false`: dependencies\n"
            "# are installed by the harness build job, so there are no RUN steps.\n"
            f"{pin}FROM {base}\nENV HOME=/tmp\nWORKDIR /work/app\nUSER 10001:10001\n")


def resolve_service(project, spec: HarnessSpec, role: str) -> ServiceSpec:
    """legacy -> spec.legacy; new -> spec.new, else defaults from target/target.yaml."""
    if role == "legacy" or spec.new is not None or role == "composite":
        return spec.service(role)
    if project is None:
        return spec.service(role)
    from .target import TargetSpecError, load_target
    try:
        target = load_target(project)
    except TargetSpecError as e:
        raise HarnessError(f"harness.yaml has no `new:` service and target/target.yaml is unusable "
                           f"(the new service is generated in M4): {str(e).splitlines()[0]}") from None
    return default_new_service(spec, target)


def _layout(project):
    from .paths import Phase2Layout
    return Phase2Layout(project)


def spec_path(project) -> Path:
    return _layout(project).harness_dir / "harness.yaml"


def load_spec(project, *, adopt: bool = False) -> HarnessSpec:
    """harness/harness.yaml, else legacy_src/harness/harness.yaml[.in] (copied into the project when `adopt`)."""
    import yaml
    lay = _layout(project)
    p = spec_path(project)
    src = p
    if not p.exists():
        cands = [lay.legacy_src / "harness" / n for n in ("harness.yaml", "harness.yaml.in")]
        src = next((c for c in cands if c.exists()), cands[0])
    if not src.exists():
        raise HarnessError("no harness spec: write harness/harness.yaml (see `migrate/harness.py`) or import a "
                           "source tree that ships harness/harness.yaml")
    try:
        data = yaml.safe_load(src.read_text())
        spec = HarnessSpec.model_validate(data)
    except Exception as e:  # noqa: BLE001
        raise HarnessError(f"{src}: invalid harness spec: {e}") from None
    if adopt and src != p:
        from .state import atomic_write_text
        lay.ensure(lay.harness_dir)
        atomic_write_text(p, f"# copied from {lay.rel(src)} by `harness build`\n" + src.read_text())
    return spec


# ------------------------------------------------------------------ state
def state_path(project) -> Path:
    return _layout(project).harness_dir / "state.json"


def load_state(project) -> dict:
    p = state_path(project)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def save_state(project, state: dict) -> None:
    from .state import atomic_write_json
    lay = _layout(project)
    lay.ensure(lay.harness_dir)
    atomic_write_json(state_path(project), state)


def _audit(project, action: str, **fields) -> None:
    from ..audit import AuditLog
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record("harness", action, **fields)


# ------------------------------------------------------------------ legacy_src import
def _tree_hash(root: Path) -> str:
    """The ingest content tree hash (same function, same exclusions) of a source directory."""
    from ..ingest import MAX_BYTES, MAX_FILES, IngestReport, _normalize
    with tempfile.TemporaryDirectory(prefix="ma-hash-") as tmp:
        rep = IngestReport(source=str(root), source_type="directory", source_hash="")
        return _normalize(Path(root), Path(tmp) / "code", rep, MAX_BYTES, MAX_FILES)


def _copy_original(root: Path, dest: Path) -> dict:
    """Copy the ORIGINAL bytes (no redaction). Symlinks/special files skipped; credential files withheld and
    replaced by empty placeholders (plan section 6); VCS and dependency folders skipped."""
    from ..ingest import MAX_BYTES, MAX_FILES
    from ..ingest.secrets import is_secret_filename
    files = total = 0
    withheld: list[str] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        keep = []
        for n in sorted(dirnames):
            full = d / n
            if full.is_symlink() or n in _SKIP_DIRS:
                skipped.append(full.relative_to(root).as_posix() + "/")
            else:
                keep.append(n)
        dirnames[:] = keep
        for n in sorted(filenames):
            full = d / n
            rel = full.relative_to(root)
            st = full.lstat()
            if not stat.S_ISREG(st.st_mode):
                skipped.append(rel.as_posix())
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if is_secret_filename(n):
                target.write_bytes(b"")
                withheld.append(rel.as_posix())
            else:
                files += 1
                total += st.st_size
                if files > MAX_FILES or total > MAX_BYTES:
                    raise HarnessError(f"source exceeds ingest limits ({MAX_FILES} files / {MAX_BYTES} bytes)")
                shutil.copyfile(full, target, follow_symlinks=False)
            os.chmod(target, 0o444)
    return {"files": files, "bytes": total, "withheld": withheld, "skipped": skipped}


def _make_writable(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path):
        os.chmod(dirpath, 0o755)
        for n in filenames:
            p = Path(dirpath) / n
            if not p.is_symlink():
                os.chmod(p, 0o644)


def import_source(project, source: str | Path | None = None, *, from_original: bool = False, force: bool = False,
                  accept_unpinned: bool = False) -> dict:
    """Copy the original, unredacted source into legacy_src/ after verifying it against the contract
    `source_hash` (ingest tree hash for directories, archive sha256 for zip/tar)."""
    from ..ingest import MAX_BYTES, MAX_FILES, MAX_RATIO, IngestError, _extract_tar, _extract_zip, _is_git_url, _sha_file
    from .contract import ContractError, load_contract
    from .state import atomic_write_json, now_iso

    if (source is None) == (not from_original):
        raise HarnessError("give exactly one of --from PATH or --from-original")
    if from_original:
        meta = json.loads(Path(project.meta_path).read_text())
        source = str(meta.get("source", ""))
        if not source:
            raise HarnessError("project.json has no source; use --from PATH")
    src = str(source)
    if _is_git_url(src):
        raise HarnessError("the original source is a git URL; clone it at the approved commit yourself and pass "
                           "--from <checkout directory> (no network access from this command)")
    try:
        contract = load_contract(project)
    except ContractError as e:
        raise HarnessError(str(e)) from None
    expected = contract.source_hash
    if not expected and not accept_unpinned:
        raise HarnessError("the migration contract has no source_hash, so legacy_src cannot be verified; "
                           "re-approve with an ingest report, or pass --accept-unpinned (audited)")
    lay = _layout(project)
    dest = lay.legacy_src
    if dest.exists() and any(dest.iterdir()) and not force:
        raise HarnessError("legacy_src/ already exists; pass --force to replace it")
    p = Path(src).expanduser().resolve()
    if not p.exists():
        raise HarnessError(f"source not found: {src}")
    if p.is_relative_to(Path(project.root).resolve()):
        raise HarnessError("the source must be outside the project folder")
    with tempfile.TemporaryDirectory(prefix="ma-src-") as tmp:
        if p.is_dir():
            stype, root, actual = "directory", p, _tree_hash(p)
        else:
            low = p.name.lower()
            actual = _sha_file(p)
            root = Path(tmp) / "stage"
            root.mkdir()
            try:
                if low.endswith(".zip"):
                    stype = "zip"
                    _extract_zip(p, root, MAX_BYTES, MAX_FILES, MAX_RATIO)
                elif low.endswith((".tar", ".tar.gz", ".tgz")):
                    stype = "tar"
                    _extract_tar(p, root, MAX_BYTES, MAX_FILES, MAX_RATIO)
                else:
                    raise HarnessError(f"unsupported source type: {p.name}")
            except IngestError as e:
                raise HarnessError(f"archive rejected: {e}") from None
        verified = bool(expected) and actual == expected
        if expected and not verified:
            _audit(project, "import_source_refused", source=src, expected=expected, actual=actual)
            raise HarnessError(f"source hash mismatch: contract source_hash {expected[:16]}..., this source "
                               f"{actual[:16]}... (not the approved legacy source)")
        lay.ensure(lay.harness_dir)
        stage = Path(tempfile.mkdtemp(prefix=".legacy_src.tmp-", dir=project.root))
        try:
            info = _copy_original(root, stage)
        except BaseException:
            _make_writable(stage)
            shutil.rmtree(stage, ignore_errors=True)
            raise
    old = None
    if dest.exists():
        old = Path(tempfile.mkdtemp(prefix=".legacy_src.old-", dir=project.root))
        os.rmdir(old)
        os.replace(dest, old)
    os.replace(stage, dest)
    os.chmod(dest, 0o755)
    if old is not None:
        _make_writable(old)
        shutil.rmtree(old, ignore_errors=True)
    lay.mark_v2()
    manifest = {"source": src, "source_type": stype, "source_hash": actual, "verified": verified,
                "imported_at": now_iso(), **info}
    atomic_write_json(lay.harness_dir / "legacy_src.json", manifest)
    _audit(project, "import_source", source=src, source_type=stype, source_hash=actual, verified=verified,
           files=info["files"], withheld=len(info["withheld"]), accept_unpinned=accept_unpinned and not expected)
    return manifest


# ------------------------------------------------------------------ compose generation
_HARDENING = {"read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
              "init": True, "pull_policy": "never"}


def _limits(lim: Limits) -> dict:
    return {"pids_limit": lim.pids, "mem_limit": f"{lim.memory_mb}m", "memswap_limit": f"{lim.memory_mb}m",
            "cpus": lim.cpus, "tmpfs": [f"/tmp:rw,nosuid,nodev,size={lim.disk_mb}m"]}


def _health_argv(svc: ServiceSpec) -> list[str]:
    return [a.replace("{port}", str(svc.port)).replace("{path}", svc.healthcheck.path)
            for a in svc.healthcheck.command]


def _main_service_doc(svc: ServiceSpec, role: str, workspace: str, user: str, label: str) -> dict:
    main = {"image": svc.image, **copy.deepcopy(_HARDENING), **_limits(svc.limits), "user": user,
            "working_dir": svc.workdir, "entrypoint": svc.start[:1], "command": svc.start[1:],
            "environment": {**svc.env, **{k: f"${{{k}:?synthetic secret}}" for k in svc.synthetic_secrets}},
            "volumes": [f"../{workspace}:{svc.workdir}"], "networks": ["internal"],
            "labels": {"ma.harness": label}}
    if svc.healthcheck.command:
        main["healthcheck"] = {"test": ["CMD", *_health_argv(svc)], "interval": f"{svc.healthcheck.interval_s:g}s",
                               "retries": max(1, int(svc.healthcheck.timeout_s / svc.healthcheck.interval_s))}
    return main


def _support_services(spec: HarnessSpec, lim: Limits, user: str) -> dict[str, dict]:
    services: dict[str, dict] = {}
    if spec.db.engine in ("postgres", "mysql"):
        if not spec.db.image or not spec.db.command:
            raise HarnessError(f"db engine {spec.db.engine} needs db.image and db.command")
        db = {"image": spec.db.image, **copy.deepcopy(_HARDENING), **_limits(lim), "user": user,
              "entrypoint": spec.db.command[:1], "command": spec.db.command[1:],
              "environment": {**spec.db.env, **{k: f"${{{k}:?synthetic secret}}" for k in spec.db.synthetic_secrets}},
              "networks": ["internal"]}
        db["tmpfs"] = db["tmpfs"] + [f"{t}:rw,nosuid,nodev" for t in spec.db.tmpfs]
        if spec.db.healthcheck:
            db["healthcheck"] = {"test": ["CMD", *spec.db.healthcheck], "interval": "2s", "retries": 60}
        services["db"] = db
    for st in spec.stubs:
        services[st.name] = {"image": st.image, **copy.deepcopy(_HARDENING), **_limits(lim), "user": user,
                             "entrypoint": st.command[:1], "command": st.command[1:], "environment": dict(st.env),
                             "networks": ["internal"]}
    return services


def router_argv(spec: HarnessSpec) -> list[str]:
    return ["python", "/app/proxy.py", "--routes", "/routes/routes.json", "--port", str(spec.router.port)]


def router_health_argv(spec: HarnessSpec) -> list[str]:
    return ["python", "/app/proxy.py", "--check", "--port", str(spec.router.port)]


def render_compose(project, spec: HarnessSpec, role: str, workspace: str | dict, *, user: str = "10001:10001",
                   new_service: ServiceSpec | None = None) -> dict:
    """Compose document for one harness role on an internal-only network: legacy or new (+ db + stubs), or
    composite (db + stubs + legacy + new + router). `workspace` is the project-relative build workspace mounted at
    the service workdir; for composite a dict {"legacy": ws, "new": ws}. `new_service` overrides the resolved
    new service (tests / no project)."""
    if role == "composite":
        if not isinstance(workspace, dict) or not {"legacy", "new"} <= set(workspace):
            raise HarnessError("composite compose needs the legacy and new workspaces")
        leg = spec.legacy
        new = new_service or resolve_service(project, spec, "new")
        services = _support_services(spec, leg.limits, user)
        deps = list(services)
        for r, svc in (("legacy", leg), ("new", new)):
            doc_svc = _main_service_doc(svc, r, workspace[r], user, "composite")
            if deps:
                doc_svc["depends_on"] = list(deps)
            services[r] = doc_svc
        router = {"image": spec.router.image_tag, **copy.deepcopy(_HARDENING), **_limits(spec.router.limits),
                  "user": user, "working_dir": "/app", "entrypoint": router_argv(spec)[:1],
                  "command": router_argv(spec)[1:], "volumes": ["./router/live:/routes:ro"],
                  "networks": ["internal"], "labels": {"ma.harness": "composite"}, "depends_on": ["legacy", "new"],
                  "healthcheck": {"test": ["CMD", *router_health_argv(spec)], "interval": "2s", "retries": 30}}
        services["router"] = router
        secrets = sorted(set(leg.synthetic_secrets) | set(new.synthetic_secrets) | set(spec.db.synthetic_secrets))
        doc = {"name": f"ma-{spec.name}-composite".lower().replace("_", "-"), "services": services,
               "networks": {"internal": {"internal": True}},
               "x-db-shared": spec.shared_db}
        if secrets:
            doc["x-synthetic-secrets"] = secrets
        return doc
    if role == "new" and new_service is not None:
        svc = new_service
    else:
        svc = resolve_service(project, spec, role)
    services = _support_services(spec, svc.limits, user)
    main = _main_service_doc(svc, role, workspace, user, role)
    deps = [n for n in services]
    if deps:
        main["depends_on"] = deps
    services[role] = main
    secrets = sorted(set(svc.synthetic_secrets) | set(spec.db.synthetic_secrets))
    doc = {"name": f"ma-{spec.name}-{role}".lower().replace("_", "-"), "services": services,
           "networks": {"internal": {"internal": True}}}
    if secrets:
        doc["x-synthetic-secrets"] = secrets
    return doc


def write_compose(project, spec: HarnessSpec, role: str, workspace: str | dict, *,
                  user: str = "10001:10001") -> Path:
    import yaml
    from .state import atomic_write_text
    doc = render_compose(project, spec, role, workspace, user=user)
    lay = _layout(project)
    lay.ensure(lay.harness_dir)
    head = ("# GENERATED by `parity harness build` - do not edit (regenerated on every build).\n"
            "# Internal network only (no route out); read-only rootfs, cap_drop ALL, no-new-privileges, non-root,\n"
            "# limits from harness.yaml. Synthetic secrets must be supplied as env (random values, never real).\n"
            "# Paths are relative to this harness/ folder. `harness up` drives the same settings via DockerBackend.\n")
    return atomic_write_text(lay.harness_dir / f"compose.{role}.yml",
                             head + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))


# ------------------------------------------------------------------ build / up / down
def _backend(project, backend=None):
    from . import exec as _exec
    b = backend if backend is not None else _exec.get_backend(project)
    ok, reason = b.available()
    if not ok:
        raise HarnessError(f"exec sandbox unavailable ({getattr(b, 'name', '?')}): {reason}")
    return b


def _source_dir(project, role: str) -> Path:
    lay = _layout(project)
    d = lay.legacy_src if role == "legacy" else lay.target_code
    if not d.is_dir() or not any(d.iterdir()):
        raise HarnessError("legacy_src/ is empty: run `parity harness import-source PROJECT --from-original`"
                           if role == "legacy" else "target_code/ is empty: the new service is generated in M4")
    return d


def _resolve(project, src_dir: Path, rel: str) -> Path:
    for base in (src_dir, _layout(project).harness_dir):
        p = (base / rel).resolve()
        if p.exists() and p.is_relative_to(Path(project.root).resolve()):
            return p
    raise HarnessError(f"{rel} not found in the service source tree or the project harness/ folder")


def _copy_workspace(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    for dirpath, dirnames, filenames in os.walk(dst):
        os.chmod(dirpath, 0o777)        # container uid may differ (rootless / Linux); per-run disposable copy
        for n in filenames:
            p = Path(dirpath) / n
            if not p.is_symlink():
                os.chmod(p, (p.stat().st_mode | 0o666) & 0o777)


def _ensure_new_dockerfile(project, spec: HarnessSpec, svc: ServiceSpec) -> None:
    """Default new service (no `new:` in harness.yaml): write harness/new/Dockerfile from target.yaml once."""
    if spec.new is not None or not svc.dockerfile:
        return
    lay = _layout(project)
    df = lay.harness_dir / svc.dockerfile
    tc = lay.target_code / svc.dockerfile
    if df.exists() or tc.exists():
        return
    from .state import atomic_write_text
    from .target import load_target
    atomic_write_text(df, default_new_dockerfile(load_target(project)))


def build(project, role: str = "legacy", *, backend=None) -> dict:
    """Build the service image (network none) and run the build job (deps + compile) in a workspace copy.
    `composite`: build the router image and write the route table + compose file (needs legacy and new builds)."""
    from .ids import allocate
    from .models import ExecJob, Mount
    from .state import now_iso
    spec = load_spec(project, adopt=True)
    if role == "composite":
        return build_composite(project, spec, backend=backend)
    svc = resolve_service(project, spec, role)
    src = _source_dir(project, role)
    b = _backend(project, backend)
    lay = _layout(project)
    if role == "new":
        _ensure_new_dockerfile(project, spec, svc)
    entry: dict = {"image": svc.image, "built_at": now_iso()}
    if svc.dockerfile and hasattr(b, "build_image"):
        ok, digest, log = b.build_image(_resolve(project, src, svc.context), _resolve(project, src, svc.dockerfile),
                                        svc.image, labels={"harness": role})
        from .state import atomic_write_text
        atomic_write_text(lay.ensure(lay.harness_dir / "logs") / f"{role}-image.log", log[-1_000_000:])
        if not ok:
            raise HarnessError(f"image build failed (see harness/logs/{role}-image.log): {log.strip()[-300:]}")
        entry["image_digest"] = digest
    run_id = allocate(project, "RUN")
    ws = lay.run_work(run_id) / "app"
    lay.ensure(ws.parent)
    _copy_workspace(src, ws)
    mounts = [Mount(source=lay.rel(ws), target=svc.workdir, read_only=False)]
    if svc.build.cache:
        cache = lay.ensure(Path(project.root) / svc.build.cache)
        os.chmod(cache, 0o777)
        mounts.append(Mount(source=svc.build.cache, target="/cache", read_only=False))
    lim = svc.limits.model_copy(update={"timeout_s": svc.build.timeout_s})
    job = ExecJob(run_id=run_id, image=svc.image, command=svc.build.command, phase="build",
                  network=svc.build.network, workdir=svc.workdir, mounts=mounts, limits=lim,
                  env=svc.build.env, labels={"harness": role, "stage": "build"})
    res = b.run(job)
    entry.update({"build_run": run_id, "workspace": lay.rel(ws), "ok": res.ok, "exit_code": res.exit_code,
                  "error": res.error, "image_digest": entry.get("image_digest") or res.image_digest,
                  "port": svc.port})
    if res.ok:
        try:                                     # lockfile hashes + CycloneDX SBOM; advisory, never fails a build
            from . import supplychain
            entry["supply_chain"] = supplychain.record_build(project, run_id, ws, src, name=spec.name)
        except Exception as e:  # noqa: BLE001
            entry["supply_chain"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    st = load_state(project)
    prev_up = (st.get(role) or {}).get("up")
    st[role] = {**entry, **({"up": prev_up} if prev_up else {})}
    save_state(project, st)
    write_compose(project, spec, role, entry["workspace"], user=getattr(b, "user", lambda: "10001:10001")())
    _audit(project, "harness_build", role=role, run_id=run_id, ok=res.ok, exit_code=res.exit_code,
           image=svc.image, image_digest=entry["image_digest"])
    return st[role]


def _copy_router_assets(project) -> Path:
    from .router import ASSETS, ROUTER_DIR
    lay = _layout(project)
    dest = lay.ensure(lay.harness_dir / "router")
    for name in ASSETS:
        shutil.copyfile(ROUTER_DIR / name, dest / name)
    lay.ensure(dest / "live")
    return dest


def ensure_router_image(project, spec: HarnessSpec, b) -> dict:
    """Build the router / schema-probe image from migrate/router/ (network none). Backends without
    `build_image` (fakes) skip the build."""
    dest = _copy_router_assets(project)
    out = {"image": spec.router.image_tag, "image_digest": ""}
    if hasattr(b, "build_image"):
        ok, digest, log = b.build_image(dest, dest / "Dockerfile", spec.router.image_tag,
                                        labels={"harness": "router"})
        from .state import atomic_write_text
        lay = _layout(project)
        atomic_write_text(lay.ensure(lay.harness_dir / "logs") / "router-image.log", log[-1_000_000:])
        if not ok:
            raise HarnessError(f"router image build failed (see harness/logs/router-image.log): {log.strip()[-300:]}")
        out["image_digest"] = digest
    return out


def build_composite(project, spec: HarnessSpec | None = None, *, backend=None) -> dict:
    """Composite (strangler) mode: needs successful legacy and new builds; builds the router image, writes
    harness/routes.json + harness/router/live/routes.json and harness/compose.composite.yml."""
    from .state import now_iso
    spec = spec or load_spec(project, adopt=True)
    st = load_state(project)
    missing = [r for r in ("legacy", "new") if not (st.get(r) or {}).get("ok")]
    if missing:
        raise HarnessError("composite mode (M6 strangler router) needs successful legacy and new builds; missing: "
                           + ", ".join(missing) + " (run `harness build PROJECT --legacy` / `--new`; target_code/ "
                           "is generated in M4)")
    b = _backend(project, backend)
    img = ensure_router_image(project, spec, b)
    table = write_routes(project, spec)
    ent = {"ok": True, "built_at": now_iso(), "image": img["image"], "image_digest": img["image_digest"],
           "workspaces": {"legacy": st["legacy"]["workspace"], "new": st["new"]["workspace"]},
           "port": spec.router.port, "db_shared": spec.shared_db,
           "routes_to_new": sum(1 for r in table["routes"] if r["upstream"] == "new")}
    prev_up = (st.get("composite") or {}).get("up")
    st["composite"] = {**ent, **({"up": prev_up} if prev_up else {})}
    save_state(project, st)
    write_compose(project, spec, "composite", ent["workspaces"], user=getattr(b, "user", lambda: "10001:10001")())
    _audit(project, "harness_build", role="composite", ok=True, image=img["image"],
           image_digest=img["image_digest"], routes_to_new=ent["routes_to_new"])
    return st["composite"]


def _secrets_file(project) -> Path:
    return _layout(project).harness_dir / "synthetic_secrets.json"


def _write_secrets(project, role: str, values: dict[str, str]) -> None:
    p = _secrets_file(project)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except ValueError:
            data = {}
    data[role] = {"note": "random values generated for this harness run; not real credentials", "values": values}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    os.chmod(p, 0o600)


class _Stack:
    """One role's containers on one internal network (used by `up`)."""

    def __init__(self, project, b, role: str, spec: HarnessSpec, values: dict[str, str], sleep, clock):
        self.project, self.b, self.role, self.spec, self.values = project, b, role, spec, values
        self.sleep, self.clock = sleep, clock
        self.containers: list[str] = []
        self.net = b.ensure_internal_network(role)

    def job(self, image, command, *, limits, env=None, synth=(), mounts=(), labels=None, workdir="/work"):
        from .ids import allocate
        from .models import ExecJob
        return ExecJob(run_id=allocate(self.project, "RUN"), image=image, command=command, phase="test",
                       network="none", workdir=workdir, mounts=list(mounts), limits=limits,
                       env=dict(env or {}), synthetic_secrets=list(synth),
                       labels={"harness": self.role, **(labels or {})})

    def sv(self, names) -> dict[str, str]:
        return {k: self.values[k] for k in names}

    def wait(self, container, argv, timeout_s, interval_s, what) -> None:
        deadline = self.clock() + timeout_s
        while True:
            rc, last = self.b.exec_in(container, argv, timeout=max(5.0, interval_s * 5))
            if rc == 0:
                return
            if self.clock() >= deadline:
                raise HarnessError(f"{what} health check failed after {timeout_s}s: {last.strip()[-300:]}")
            self.sleep(interval_s)

    def db(self, health: HealthSpec, limits: Limits) -> None:
        spec = self.spec
        if spec.db.engine not in ("postgres", "mysql"):
            return
        if not spec.db.image or not spec.db.command:
            raise HarnessError(f"db engine {spec.db.engine} needs db.image and db.command")
        j = self.job(spec.db.image, spec.db.command, limits=limits, env=spec.db.env,
                     synth=spec.db.synthetic_secrets, labels={"service": "db"}, workdir="/tmp")
        c = self.b.start_service(j, network=self.net, aliases=["db"], secret_values=self.sv(spec.db.synthetic_secrets),
                                 extra_tmpfs=spec.db.tmpfs)
        self.containers.append(c)
        if spec.db.healthcheck:
            self.wait(c, spec.db.healthcheck, health.timeout_s, health.interval_s, "db")

    def migrate(self, svc: ServiceSpec, workspace: str, migrate: list[str], seed: list[list[str]],
                tag: str = "") -> None:
        from .models import Mount
        ws_mount = Mount(source=workspace, target=svc.workdir, read_only=False)
        for cmd, what in ([(migrate, "migrate")] if migrate else []) + [(x, "seed") for x in seed]:
            j = self.job(svc.image, cmd, limits=svc.limits, env=svc.env, synth=svc.synthetic_secrets,
                         mounts=[ws_mount], labels={"stage": what, **({"service": tag} if tag else {})},
                         workdir=svc.workdir)
            res = self.b.run(j, internal_network=self.net, secret_values=self.sv(svc.synthetic_secrets))
            if not res.ok:
                raise HarnessError(f"db {what} failed ({res.run_id}): {(res.error or res.stderr_tail).strip()[-300:]}")

    def stubs(self, limits: Limits) -> None:
        for stub in self.spec.stubs:
            j = self.job(stub.image, stub.command, limits=limits, env=stub.env, labels={"service": stub.name})
            self.containers.append(self.b.start_service(j, network=self.net, aliases=[stub.name]))

    def service(self, svc: ServiceSpec, workspace: str, alias: str) -> str:
        from .models import Mount
        ws_mount = Mount(source=workspace, target=svc.workdir, read_only=False)
        j = self.job(svc.image, svc.start, limits=svc.limits, env=svc.env, synth=svc.synthetic_secrets,
                     mounts=[ws_mount], labels={"service": alias}, workdir=svc.workdir)
        c = self.b.start_service(j, network=self.net, aliases=[alias], secret_values=self.sv(svc.synthetic_secrets))
        self.containers.append(c)
        if svc.healthcheck.command:
            self.wait(c, _health_argv(svc), svc.healthcheck.timeout_s, svc.healthcheck.interval_s, alias)
        return c

    def router(self) -> str:
        from .models import Mount
        spec = self.spec
        j = self.job(spec.router.image_tag, router_argv(spec), limits=spec.router.limits,
                     mounts=[Mount(source="harness/router/live", target="/routes", read_only=True)],
                     labels={"service": "router"}, workdir="/tmp")   # NOT /app: a tmpfs at the workdir would mask /app/proxy.py
        c = self.b.start_service(j, network=self.net, aliases=["router"])
        self.containers.append(c)
        self.wait(c, router_health_argv(spec), 60, 1.0, "router")
        return c

    def teardown(self) -> None:
        self.b.remove_containers(self.containers)
        self.b.remove_network(self.net)


def _new_db_commands(spec: HarnessSpec) -> tuple[list[str], list[list[str]]]:
    if spec.new_db is not None:
        return spec.new_db.migrate, spec.new_db.seed
    return spec.db.migrate, spec.db.seed


def up(project, role: str = "legacy", *, backend=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Start db + stubs + the service on an internal network, run migrations/seeds, wait for the health check.

    new: server-engine DBs are migrated/seeded with the LEGACY image and workspace (legacy schema, P2-Q10, needs a
    legacy build); sqlite/none DBs are private to the service, migrated in the new image (`new_db` overrides).
    composite: db, legacy, new and the router on one network; flipped slices are routed to `new`."""
    from .exec.docker import synthetic_value
    from .state import now_iso
    spec = load_spec(project)
    st = load_state(project)
    ent = st.get(role) or {}
    if not ent.get("ok"):
        raise HarnessError(f"no successful {role} build: run `parity harness build PROJECT --{role}` first")
    if (ent.get("up") or {}).get("containers"):
        raise HarnessError(f"{role} harness is already up; run `harness down` first")
    leg_ent = st.get("legacy") or {}
    new_ent = st.get("new") or {}
    if role == "composite":
        for r, e in (("legacy", leg_ent), ("new", new_ent)):
            if not e.get("ok"):
                raise HarnessError(f"composite mode needs a successful {r} build")
        write_routes(project, spec)
    b = _backend(project, backend)
    if not hasattr(b, "start_service"):
        raise HarnessError(f"exec backend {getattr(b, 'name', '?')} cannot run long-lived services")
    leg = spec.legacy
    new = resolve_service(project, spec, "new") if role in ("new", "composite") else None
    svc = leg if role == "legacy" else new
    if role == "new" and spec.shared_db and spec.db.migrate and not leg_ent.get("ok"):
        raise HarnessError("the new service shares the legacy schema: build legacy first "
                           "(`harness build PROJECT --legacy`), its image runs the legacy migrations")
    names = set(spec.db.synthetic_secrets)
    for s_ in (leg if role in ("legacy", "composite") else None, new):
        if s_ is not None:
            names |= set(s_.synthetic_secrets)
    values = {k: synthetic_value() for k in sorted(names)}
    stack = _Stack(project, b, role, spec, values, sleep, clock)
    services: dict[str, dict] = {}
    try:
        stack.db(svc.healthcheck, svc.limits)
        if role == "legacy":
            stack.migrate(leg, ent["workspace"], spec.db.migrate, spec.db.seed)
        elif spec.shared_db:
            if spec.db.migrate or spec.db.seed:
                stack.migrate(leg, leg_ent["workspace"], spec.db.migrate, spec.db.seed, tag="legacy")
        else:
            if role == "composite":
                stack.migrate(leg, leg_ent["workspace"], spec.db.migrate, spec.db.seed, tag="legacy")
            m, sd = _new_db_commands(spec)
            stack.migrate(new, new_ent["workspace"], m, sd, tag="new")
        stack.stubs(svc.limits)
        if role in ("legacy", "new"):
            c = stack.service(svc, ent["workspace"], role)
            services[role] = {"container": c, "alias": role, "port": svc.port}
        else:
            for r, s_, e in (("legacy", leg, leg_ent), ("new", new, new_ent)):
                c = stack.service(s_, e["workspace"], r)
                services[r] = {"container": c, "alias": r, "port": s_.port}
            c = stack.router()
            services["router"] = {"container": c, "alias": "router", "port": spec.router.port}
    except BaseException:
        stack.teardown()
        raise
    _write_secrets(project, role, values)
    main = services["router" if role == "composite" else role]
    ent["up"] = {"network": stack.net, "containers": stack.containers, "service": main["container"],
                 "port": main["port"], "alias": main["alias"], "started_at": now_iso(),
                 "synthetic_secrets": sorted(values), "services": services,
                 "db_shared": spec.shared_db if role == "composite" else None}
    st[role] = ent
    save_state(project, st)
    _audit(project, "harness_up", role=role, network=stack.net, containers=stack.containers)
    try:
        seed = replay_seeds(project, role, backend=b, spec=spec)
    except HarnessError:
        try:
            down(project, role, backend=b)
        finally:
            pass
        raise
    if seed.get("requests"):
        ent = load_state(project).get(role) or ent
        ent.setdefault("up", {})["seed"] = seed
        st = load_state(project)
        st[role] = ent
        save_state(project, st)
    return ent


def restart(project, role: str, *, backend=None) -> dict:
    """down + up of one role: a fresh start re-runs migrations, seeds and `seed_requests`, so in-memory state from
    earlier recording or equivalence traffic is gone. Needed before comparing two services: both must start from
    the same data, otherwise state-dependent answers (counts, ids, lists) differ for reasons that are not bugs."""
    if (load_state(project).get(role) or {}).get("up"):
        down(project, role, backend=backend)
    return up(project, role, backend=backend)


SEED_MAX_REQUESTS = 200


def load_seed_cases(project, spec: HarnessSpec | None = None) -> list:
    """The harness `seed_requests` file (source-relative, read from legacy_src/) as in-memory HTTP cases. Each item:
    {method, path, body?, headers?, query?, expect_status?: [codes]}. An `auth` key is accepted and ignored: the
    service's `request_headers` are always sent."""
    from .models import CharacterizationCase, HttpRequest
    spec = spec or load_spec(project)
    if not spec.seed_requests:
        return []
    lay = _layout(project)
    path = (lay.legacy_src / spec.seed_requests).resolve()
    if not path.is_file() or not path.is_relative_to(lay.legacy_src.resolve()):
        raise HarnessError(f"seed_requests file {spec.seed_requests} not found in legacy_src/")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["requests"] if isinstance(data, dict) else data
        if not isinstance(items, list) or len(items) > SEED_MAX_REQUESTS:
            raise ValueError(f"expected a list of at most {SEED_MAX_REQUESTS} requests")
    except (OSError, ValueError, KeyError) as e:
        raise HarnessError(f"seed_requests {spec.seed_requests} is not valid: {e}") from None
    cases = []
    for i, it in enumerate(items):
        try:
            req = HttpRequest(method=str(it["method"]).upper(), path=str(it["path"]), headers=dict(it.get("headers") or {}),
                              query=dict(it.get("query") or {}), body=it.get("body"))
            if not req.path.startswith("/"):
                raise ValueError("path must start with /")
            cases.append(CharacterizationCase(id=f"CT-{9001 + i}", kind="http", request=req, tags=["seed"]))
        except (KeyError, TypeError, ValueError) as e:
            raise HarnessError(f"seed_requests item #{i + 1} is invalid: {str(e)[:120]}") from None
    return cases


def replay_seeds(project, role: str, *, backend=None, spec: HarnessSpec | None = None) -> dict:
    """Replay `seed_requests` against a RUNNING role (needed for in-memory databases that start empty). Every
    seed must answer below 400 (or one of its `expect_status` codes): a seed that fails would make every later case
    meaningless, so this raises HarnessError. composite: seeds `legacy` and `new` directly on the composite network."""
    spec = spec or load_spec(project)
    cases = load_seed_cases(project, spec)
    if not cases:
        return {"requests": 0, "ok": True}
    items = json.loads((_layout(project).legacy_src / spec.seed_requests).read_text(encoding="utf-8"))
    items = items["requests"] if isinstance(items, dict) else items
    targets = [(role, None)]
    if role == "composite":
        st = (load_state(project).get("composite") or {}).get("up") or {}
        sv = st.get("services") or {}
        targets = [(r, f"http://{r}:{(sv.get(r) or {}).get('port')}") for r in ("legacy", "new")]
    failed = []
    for r, url in targets:
        res = run_cases_in_network(project, "composite" if role == "composite" else r, cases, backend=backend,
                                   compare=False, base_url=url)
        for it, c, rr in zip(items, cases, res):
            status = rr.observed.status if rr.observed is not None else None
            ok_codes = it.get("expect_status")
            good = (status in ok_codes) if isinstance(ok_codes, list) and ok_codes else (status is not None and status < 400)
            if not good:
                failed.append(f"{r}: {c.request.method} {c.request.path} answered {status if status is not None else rr.error or 'nothing'}")
    _audit(project, "harness_seed", role=role, requests=len(cases), failed=len(failed))
    if failed:
        raise HarnessError(f"{len(failed)} of {len(cases) * len(targets)} seed request(s) failed (first: {failed[0]}). "
                           "If it is 401/403, set request_headers on the service in harness.yaml, e.g. "
                           "{X-API-Token: '{secret:API_TOKEN}'}")
    return {"requests": len(cases), "ok": True, "targets": [t[0] for t in targets]}


def down(project, role: str = "legacy", *, backend=None) -> dict:
    from . import exec as _exec
    st = load_state(project)
    ent = st.get(role) or {}
    upd = ent.get("up") or {}
    b = backend if backend is not None else _exec.get_backend(project)
    ok, reason = b.available()
    if not ok:
        raise HarnessError(f"exec sandbox unavailable: {reason}")
    names = list(upd.get("containers") or [])
    if hasattr(b, "list_managed"):
        names += [c["name"] for c in b.list_managed(harness=role) if c.get("name")]
    names = sorted(set(names))
    if hasattr(b, "remove_containers"):
        b.remove_containers(names)
    net = upd.get("network")
    if net and hasattr(b, "remove_network"):
        b.remove_network(net)
    ent.pop("up", None)
    if ent:
        st[role] = ent
        save_state(project, st)
    sp = _secrets_file(project)
    if sp.exists():
        try:
            data = json.loads(sp.read_text())
            data.pop(role, None)
            _write_secrets_raw(sp, data)
        except ValueError:
            sp.unlink()
    _audit(project, "harness_down", role=role, removed=names, network=net)
    return {"removed": names, "network": net}


def _write_secrets_raw(p: Path, data: dict) -> None:
    if not data:
        p.unlink(missing_ok=True)
        return
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))


def status(project, role: str = "legacy", *, backend=None) -> dict:
    from . import exec as _exec
    st = load_state(project)
    ent = st.get(role) or {}
    hd = _layout(project).legacy_src / "harness"
    out = {"role": role, "spec": spec_path(project).exists() or (hd / "harness.yaml").exists()
           or (hd / "harness.yaml.in").exists(),
           "legacy_src": (_layout(project).harness_dir / "legacy_src.json").exists(),
           "build": {k: v for k, v in ent.items() if k != "up"} or None, "up": ent.get("up"), "containers": [],
           "sandbox": ""}
    b = backend if backend is not None else _exec.get_backend(project)
    ok, reason = b.available()
    out["sandbox"] = f"{b.name}: {'available' if ok else reason}"
    if ok and hasattr(b, "list_managed"):
        out["containers"] = b.list_managed(harness=role)
    if role == "composite":
        rp = routes_path(project)
        try:
            t = json.loads(rp.read_text()) if rp.exists() else None
        except ValueError:
            t = None
        out["routes"] = None if t is None else {
            "flipped": t.get("flipped", []), "stale_flips": t.get("stale_flips", []),
            "to_new": sum(1 for r in t.get("routes", []) if r.get("upstream") == "new"),
            "total": len(t.get("routes", []))}
    out["urls"] = service_urls(project)
    return out


# ------------------------------------------------------------------ strangler routes and flips
def approved_graph(project):
    """The approved graph snapshot named in the migration contract (never `latest`)."""
    from ..graph.store import parse_graph
    from .contract import ContractError, load_contract
    try:
        contract = load_contract(project)
    except ContractError as e:
        raise HarnessError(str(e)) from None
    snap = (Path(project.root) / contract.graph_snapshot).resolve()
    if not snap.is_relative_to(Path(project.root).resolve()) or not snap.is_file():
        raise HarnessError(f"approved graph snapshot {contract.graph_snapshot!r} not found in the project")
    return parse_graph(snap.read_text())


def load_plan(project):
    from .models import MigrationPlan
    p = _layout(project).plan_json
    if not p.exists():
        return None
    try:
        return MigrationPlan.model_validate_json(p.read_text())
    except ValueError as e:
        raise HarnessError(f"plan/migration_plan.json is invalid: {str(e).splitlines()[0]}") from None


def slice_approved(project, slice_id: str) -> bool:
    """slices/SLICE-NNNN/approval.json exists and is a valid SliceApproval for this slice."""
    from .models import SliceApproval
    try:
        p = _layout(project).slice_approval(slice_id)
    except ValueError:
        return False
    if not p.exists():
        return False
    try:
        return SliceApproval.model_validate_json(p.read_text()).slice_id == slice_id
    except ValueError:
        return False


def flips_path(project) -> Path:
    return _layout(project).harness_dir / "flips.json"


def routes_path(project) -> Path:
    return _layout(project).harness_dir / "routes.json"


def load_flips(project) -> dict[str, dict]:
    p = flips_path(project)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
    except ValueError:
        raise HarnessError("harness/flips.json is corrupt; fix or delete it") from None
    flips = d.get("flips") if isinstance(d, dict) else None
    return dict(flips) if isinstance(flips, dict) else {}


def _route_specificity(path: str) -> tuple:
    segs = [x for x in path.strip("/").split("/") if x]
    params = sum(1 for x in segs if re.fullmatch(r":\w+\??|\{[^}/]+\}|<[^>/]+>|\*", x))
    return (-(len(segs) - params), -len(segs), params)


def build_route_table(plan, graph, flips: dict[str, dict], approved: set[str], upstreams: dict[str, str]) -> dict:
    """Pure: every planned HTTP endpoint gets a route; its upstream is `new` when its slice is flipped AND
    (approved or force-flipped for testing), else `legacy`. Unplanned paths fall through to `default` legacy.
    Routes are ordered most specific first (literal segments, then length)."""
    from .casestore import parse_endpoint, route_regex
    by_id = {n.id: n for n in graph.nodes}
    routes, stale, skipped = [], [], []
    for sl in (plan.slices if plan is not None else []):
        fl = flips.get(sl.id)
        live = bool(fl) and (sl.id in approved or bool(fl.get("forced")))
        if fl and not live:
            stale.append(sl.id)
        for nid in sl.node_ids:
            n = by_id.get(nid)
            if n is None or n.type != "endpoint":
                continue
            parsed = parse_endpoint(n.name)
            if parsed is None or parsed[0] != "http":
                skipped.append(nid)
                continue
            method, path = parsed[1].upper(), parsed[2]
            if method in ("ALL", "USE", "ANY"):
                method = "*"
            if not path.startswith("/"):
                path = "/" + path
            routes.append({"method": method, "path": path, "regex": route_regex(path).pattern,
                           "upstream": "new" if live else "legacy", "slice": sl.id, "endpoint": nid})
    routes.sort(key=lambda r: (_route_specificity(r["path"]), r["path"], r["method"] == "*", r["method"]))
    flipped = sorted(sid for sid, f in flips.items()
                     if plan is not None and any(s.id == sid for s in plan.slices) and sid not in stale)
    return {"schema_version": 1, "default": "legacy", "upstreams": dict(upstreams),
            "plan_version": plan.version if plan is not None else None, "flipped": flipped,
            "stale_flips": sorted(stale), "non_http_endpoints": sorted(skipped), "routes": routes}


def _upstreams(project, spec: HarnessSpec) -> dict[str, str]:
    try:
        new_port = resolve_service(project, spec, "new").port
    except HarnessError:
        new_port = spec.legacy.port
    return {"legacy": f"http://legacy:{spec.legacy.port}", "new": f"http://new:{new_port}"}


def generate_routes(project, spec: HarnessSpec | None = None) -> dict:
    spec = spec or load_spec(project)
    plan = load_plan(project)
    graph = approved_graph(project) if plan is not None else None
    approved = {s.id for s in plan.slices if slice_approved(project, s.id)} if plan is not None else set()
    from ..graph.models import Graph
    return build_route_table(plan, graph or Graph(), load_flips(project), approved, _upstreams(project, spec))


def write_routes(project, spec: HarnessSpec | None = None) -> dict:
    """Regenerate harness/routes.json and the router's live copy (harness/router/live/routes.json)."""
    from .router.proxy import RouteTable
    from .state import atomic_write_json, now_iso
    table = generate_routes(project, spec)
    RouteTable(table)                      # never write a table the router would reject
    table["generated_at"] = now_iso()
    lay = _layout(project)
    lay.ensure(lay.harness_dir)
    atomic_write_json(routes_path(project), table)
    atomic_write_json(lay.ensure(lay.harness_dir / "router" / "live") / "routes.json", table)
    return table


def flip(project, slice_id: str, *, force_for_testing: bool = False, actor: str = "human", reason: str = "") -> dict:
    """Route an approved slice's endpoints to the new service. Unapproved slices are refused unless
    `force_for_testing` (recorded as forced in flips.json and audited)."""
    from .models import is_id
    from .state import atomic_write_json, file_lock, now_iso
    if not is_id(slice_id, "SLICE"):
        raise HarnessError(f"invalid slice id {slice_id!r}")
    plan = load_plan(project)
    if plan is None:
        raise HarnessError("no plan/migration_plan.json: run `parity plan PROJECT` first")
    sl = next((s for s in plan.slices if s.id == slice_id), None)
    if sl is None:
        raise HarnessError(f"{slice_id} is not in the current plan (v{plan.version})")
    spec = load_spec(project)
    graph = approved_graph(project)
    probe = build_route_table(plan, graph, {slice_id: {"forced": True}}, set(), _upstreams(project, spec))
    if not any(r["slice"] == slice_id for r in probe["routes"]):
        raise HarnessError(f"{slice_id} has no HTTP endpoint routes to flip ({sl.kind} slice); foundation code "
                           "ships with the entry slices that use it")
    approved = slice_approved(project, slice_id)
    if not approved and not force_for_testing:
        raise HarnessError(f"{slice_id} is not approved (no valid slices/{slice_id}/approval.json); approve it "
                           "first or pass --force-for-testing (audited, sandbox only)")
    warnings = [f"depends on {d}, which is not approved" for d in sl.depends_on if not slice_approved(project, d)]
    with file_lock(flips_path(project)):
        flips = load_flips(project)
        flips[slice_id] = {"at": now_iso(), "by": actor, "forced": not approved, "reason": reason[:500]}
        _layout(project).ensure(_layout(project).harness_dir)
        atomic_write_json(flips_path(project), {"schema_version": 1, "flips": flips})
    table = write_routes(project, spec)
    _audit(project, "harness_flip_forced" if not approved else "harness_flip", slice_id=slice_id, actor=actor,
           forced=not approved, reason=reason[:500], routes_to_new=sum(r["upstream"] == "new" for r in table["routes"]))
    return {"slice_id": slice_id, "forced": not approved, "warnings": warnings,
            "routes": [r for r in table["routes"] if r["slice"] == slice_id]}


def unflip(project, slice_id: str, *, actor: str = "human", reason: str = "") -> dict:
    from .state import atomic_write_json, file_lock
    with file_lock(flips_path(project)):
        flips = load_flips(project)
        if slice_id not in flips:
            raise HarnessError(f"{slice_id} is not flipped")
        flips.pop(slice_id)
        atomic_write_json(flips_path(project), {"schema_version": 1, "flips": flips})
    table = write_routes(project)
    _audit(project, "harness_unflip", slice_id=slice_id, actor=actor, reason=reason[:500])
    return {"slice_id": slice_id, "flipped": table["flipped"]}


def service_urls(project) -> dict[str, str | None]:
    """In-network base URLs of running roles (reachable from containers on that role's internal network, e.g. the
    harness runner), None when the role is not up: {"legacy", "new", "composite"} (composite = the router)."""
    st = load_state(project)
    out: dict[str, str | None] = {}
    for role in ("legacy", "new", "composite"):
        u = (st.get(role) or {}).get("up") or {}
        out[role] = f"http://{u['alias']}:{u['port']}" if u.get("containers") and u.get("alias") and u.get("port") \
            else None
    return out


# ------------------------------------------------------------------ in-network case execution
RUNNER_IMAGE = "python:3.12.7-alpine3.20"    # same pinned base as migrate/router/Dockerfile; never pulled
RUNNER_FILES = ("wire.py", "netrunner.py")    # stdlib-only runner code staged into the run workspace
RUNNER_RESULTS = "results.json"
RUNNER_LIMITS = Limits(cpus=0.5, memory_mb=256, pids=64, timeout_s=900, disk_mb=64)
MAX_RUNNER_TIMEOUT_S = 6 * 3600


def _runner_cfg(timeout_s: float, job_endpoint: str | None, header_allowlist: list[str] | None, base_url: str,
                extra_headers: dict[str, str] | None = None):
    from .harness_runner.models import DEFAULT_HEADER_ALLOWLIST, DEFAULT_JOB_ENDPOINT, RunnerConfig
    return RunnerConfig(base_url=base_url, timeout_s=timeout_s,
                        header_allowlist=list(header_allowlist or DEFAULT_HEADER_ALLOWLIST),
                        job_endpoint=DEFAULT_JOB_ENDPOINT if job_endpoint is None else (job_endpoint or None),
                        extra_headers=dict(extra_headers or {}))


_SECRET_REF = re.compile(r"\{secret:([A-Z][A-Z0-9_]{0,63})\}")


def request_headers_for(project, role: str, spec: HarnessSpec | None = None) -> dict[str, str]:
    """The request headers the runner adds for `role`, with `{secret:NAME}` resolved from the values generated at
    `harness up`. composite uses the legacy service's headers (secrets are shared by name across its services)."""
    try:
        spec = spec or load_spec(project)
        svc = spec.legacy if role in ("legacy", "composite") else resolve_service(project, spec, "new")
    except HarnessError:
        return {}               # no harness spec / no target spec (a hand-made harness): nothing to inject
    if not svc.request_headers:
        return {}
    vals: dict[str, str] = {}
    sp = _secrets_file(project)
    if sp.exists():
        try:
            vals = (json.loads(sp.read_text()).get(role) or {}).get("values") or {}
        except ValueError:
            vals = {}
    out = {}
    for k, v in svc.request_headers.items():
        def sub(m):
            name = m.group(1)
            if name not in svc.synthetic_secrets:
                raise HarnessError(f"request_headers[{k}] uses {{secret:{name}}}, which is not in synthetic_secrets")
            if name not in vals:
                raise HarnessError(f"request_headers[{k}]: no value for secret {name} (is the {role} harness up?)")
            return vals[name]
        out[k] = _SECRET_REF.sub(sub, v)
    return out


def _supports_internal_network(b) -> bool:
    import inspect
    try:
        params = inspect.signature(b.run).parameters
    except (TypeError, ValueError):
        return False
    return "internal_network" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def stage_runner(project, run_id: str, cases, config: dict) -> tuple[str, str]:
    """Copy the stdlib runner code and write cases.json + config.json into runs/RUN-NNNN/work/{runner,cases}/.
    Returns the project-relative (runner_dir, cases_dir); both are mounted read-only."""
    from .harness_runner import netrunner
    from .state import atomic_write_json
    lay = _layout(project)
    work = lay.ensure(lay.run_work(run_id))
    rdir, cdir = lay.ensure(work / "runner"), lay.ensure(work / "cases")
    src = Path(netrunner.__file__).parent
    for name in RUNNER_FILES:
        shutil.copyfile(src / name, rdir / name)
        os.chmod(rdir / name, 0o644)
    # only what the runner needs: expectations and provenance never enter the container
    atomic_write_json(cdir / "cases.json", [c.model_dump(mode="json", include={"id", "kind", "request"})
                                            for c in cases])
    atomic_write_json(cdir / "config.json", config)
    for p in (cdir / "cases.json", cdir / "config.json"):
        os.chmod(p, 0o644)
    for d in (work, rdir, cdir):
        os.chmod(d, 0o755)      # container uid may differ (rootless / Linux); read-only for it anyway
    return lay.rel(rdir), lay.rel(cdir)


def run_cases_in_network(project, role: str, cases, *, backend=None, compare: bool = True, timeout_s: float = 10.0,
                         job_endpoint: str | None = None, header_allowlist: list[str] | None = None,
                         image: str = RUNNER_IMAGE, base_url: str | None = None) -> list:
    """Run characterization cases against a RUNNING harness role from inside the sandbox.

    One test-phase ExecJob (image `RUNNER_IMAGE`, never pulled) attached to the role's `--internal` network
    (`internal_network=...`; the job itself declares network "none", the policy for test jobs). The stdlib-only
    runner (wire.py + netrunner.py) and the cases are staged under runs/RUN-NNNN/work/ and mounted read-only;
    the runner writes /reports/results.json, which the backend copies to runs/RUN-NNNN/reports/results.json.
    Observations are parsed back on the host into harness_runner CaseResults (`compare=True`: compared with
    case.expect like run_case, else observations only like observe). Raises HarnessError when the role is not
    up, the backend cannot attach jobs to internal networks, or the run produced no usable results."""
    from .harness_runner import result_from_raw, wire_config
    from .ids import allocate
    from .models import ExecJob, Mount
    if role not in ("legacy", "new", "composite"):
        raise HarnessError(f"unknown harness role {role!r}")
    url = base_url or service_urls(project).get(role)      # base_url: a service alias on the role's network
    net = ((load_state(project).get(role) or {}).get("up") or {}).get("network")
    if not url or not net:
        flag = f"--{role}"
        raise HarnessError(f"the {role} harness is not up: run `parity harness build PROJECT {flag}` and "
                           f"`harness up PROJECT {flag}` (or pass an explicit --base-url)")
    cases = list(cases)
    if not cases:
        return []
    b = _backend(project, backend)
    if not _supports_internal_network(b):
        raise HarnessError(f"exec backend {getattr(b, 'name', '?')} cannot attach jobs to a harness internal network")
    cfg = _runner_cfg(timeout_s, job_endpoint, header_allowlist, url, request_headers_for(project, role))
    wcfg = {k: v for k, v in wire_config(cfg).items() if k != "base_url"}
    run_id = allocate(project, "RUN")
    rdir, cdir = stage_runner(project, run_id, cases, wcfg)
    per_case = cfg.timeout_s * (1 + (cfg.max_redirects if cfg.follow_redirects else 0))
    lim = RUNNER_LIMITS.model_copy(update={"timeout_s": int(min(MAX_RUNNER_TIMEOUT_S,
                                                                60 + per_case * len(cases)))})
    job = ExecJob(run_id=run_id, image=image, phase="test", network="none", workdir="/runner",
                  command=["python", "-E", "-s", "-B", "/runner/netrunner.py", "--base-url", url,
                           "--cases", "/cases/cases.json", "--config", "/cases/config.json",
                           "--out", f"/reports/{RUNNER_RESULTS}"],
                  mounts=[Mount(source=rdir, target="/runner", read_only=True),
                          Mount(source=cdir, target="/cases", read_only=True)],
                  limits=lim, env={"HOME": "/tmp"},
                  labels={"harness": role, "stage": "cases", "mode": "run" if compare else "observe"})
    res = b.run(job, internal_network=net)
    _audit(project, "harness_cases", role=role, run_id=run_id, cases=len(cases), exit_code=res.exit_code,
           network=net, error=(res.error or "")[:300])
    if res.exit_code is None:
        raise HarnessError(f"runner job {run_id} did not run: {res.error or 'backend error'}"[:500])
    if res.exit_code not in (0, 1):
        raise HarnessError(f"runner job {run_id} failed (exit {res.exit_code}): "
                           f"{(res.stderr_tail or res.stdout_tail).strip()[-300:]}")
    lay = _layout(project)
    ref = next((r for r in res.reports if r.rsplit("/", 1)[-1] == RUNNER_RESULTS), None)
    path = Path(project.root) / ref if ref else lay.run_reports(run_id) / RUNNER_RESULTS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["results"] if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ValueError("`results` is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise HarnessError(f"runner job {run_id} wrote no usable {RUNNER_RESULTS}: {type(e).__name__}: {e}"[:500])
    by_id: dict[str, dict] = {}
    for r in raw:
        if isinstance(r, dict) and isinstance(r.get("case_id"), str):
            by_id.setdefault(r["case_id"], r)
    return [result_from_raw(c, by_id.get(c.id, None), cfg, compare_expect=compare) for c in cases]


# ------------------------------------------------------------------ CLI
def _role_options(f):
    f = click.option("--composite", "role", flag_value="composite", help="legacy + new behind the router (M6).")(f)
    f = click.option("--new", "role", flag_value="new", help="The generated service in target_code/.")(f)
    f = click.option("--legacy", "role", flag_value="legacy", default=True, help="The legacy service (default).")(f)
    return f


def register(groups) -> None:
    def _project(path: Path):
        from ..project import Project
        try:
            return Project.load(path)
        except (FileNotFoundError, ValueError) as e:
            raise click.ClickException(str(e))

    def _gate(p):
        from .contract import ContractError, require_contract
        try:
            require_contract(p)
        except ContractError as e:
            raise click.ClickException(f"contract gate: {e}")

    @groups.harness.command("import-source")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--from", "src", type=click.Path(exists=True, path_type=Path), default=None,
                  help="Original source directory or archive (must match the contract source_hash).")
    @click.option("--from-original", is_flag=True, help="Use the source recorded in project.json.")
    @click.option("--force", is_flag=True, help="Replace an existing legacy_src/.")
    @click.option("--accept-unpinned", is_flag=True,
                  help="Allow a contract without source_hash (no verification; audited).")
    def import_cmd(project: Path, src: Path | None, from_original: bool, force: bool, accept_unpinned: bool):
        """Copy the ORIGINAL (unredacted) legacy source into legacy_src/ (exec sandbox only)."""
        p = _project(project)
        _gate(p)
        try:
            m = import_source(p, src, from_original=from_original, force=force, accept_unpinned=accept_unpinned)
        except HarnessError as e:
            raise click.ClickException(str(e))
        click.echo(f"legacy_src: {m['files']} files ({m['bytes']} bytes), {len(m['withheld'])} credential file(s) "
                   f"withheld, source_hash {m['source_hash'][:16]}... "
                   f"{'verified' if m['verified'] else 'NOT verified (--accept-unpinned)'}")

    @groups.harness.command("build")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @_role_options
    def build_cmd(project: Path, role: str):
        """Build the image (no network) and run the dependency/compile job in the sandbox."""
        p = _project(project)
        _gate(p)
        try:
            ent = build(p, role)
        except HarnessError as e:
            raise click.ClickException(str(e))
        if role == "composite":
            click.echo(f"composite: router image {ent['image']} OK, {ent['routes_to_new']} route(s) to new, "
                       f"db {'shared' if ent['db_shared'] else 'NOT shared (sqlite/none: one private DB per service)'}"
                       "\n  routes:  harness/routes.json\n  compose: harness/compose.composite.yml")
            return
        click.echo(f"{role}: build {ent['build_run']} {'OK' if ent['ok'] else 'FAILED'}"
                   + (f" (exit {ent['exit_code']})" if ent["exit_code"] not in (0, None) else "")
                   + (f": {ent['error']}" if ent["error"] else "")
                   + f"\n  workspace: {ent['workspace']}\n  compose:   harness/compose.{role}.yml")
        if not ent["ok"]:
            raise SystemExit(1)

    @groups.harness.command("up")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @_role_options
    def up_cmd(project: Path, role: str):
        """Start the built service (+ db, stubs) on an internal network and wait for its health check."""
        p = _project(project)
        _gate(p)
        try:
            ent = up(p, role)
        except HarnessError as e:
            raise click.ClickException(str(e))
        u = ent["up"]
        click.echo(f"{role}: up on {u['network']} as {u['alias']}:{u['port']} ({len(u['containers'])} container(s))")

    @groups.harness.command("down")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @_role_options
    def down_cmd(project: Path, role: str):
        """Remove the harness containers and network."""
        p = _project(project)
        try:
            r = down(p, role)
        except HarnessError as e:
            raise click.ClickException(str(e))
        click.echo(f"{role}: removed {len(r['removed'])} container(s)" + (f", network {r['network']}"
                                                                          if r["network"] else ""))

    @groups.harness.command("status")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @_role_options
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def status_cmd(project: Path, role: str, fmt: str):
        """Show the harness build/up state and running containers."""
        s = status(_project(project), role)
        if fmt == "json":
            click.echo(json.dumps(s, indent=2))
            return
        bld = s["build"] or {}
        click.echo(f"role:       {role}\nsandbox:    {s['sandbox']}\nspec:       {'yes' if s['spec'] else 'missing'}"
                   f"\nlegacy_src: {'imported' if s['legacy_src'] else 'not imported'}")
        click.echo(f"build:      {bld.get('build_run', '-')} {'OK' if bld.get('ok') else ('FAILED' if bld else '')}")
        click.echo(f"up:         {(s['up'] or {}).get('network', 'down')}")
        for c in s["containers"]:
            click.echo(f"  {c['name']}: {c['state']} ({c['status']})")

    @groups.harness.command("flip")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.option("--force-for-testing", is_flag=True,
                  help="Flip an unapproved slice (sandbox testing only; recorded as forced and audited).")
    @click.option("--by", "actor", default="human", show_default=True)
    @click.option("--reason", default="")
    @click.option("--no-verify", is_flag=True, help="Do not run the composite suite after the flip.")
    def flip_cmd(project: Path, slice_id: str, force_for_testing: bool, actor: str, reason: str, no_verify: bool):
        """Route an approved slice's endpoints to the new service (strangler router)."""
        p = _project(project)
        _gate(p)
        try:
            r = flip(p, slice_id, force_for_testing=force_for_testing, actor=actor, reason=reason)
        except HarnessError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: {len(r['routes'])} route(s) -> new" + (" (FORCED, unapproved)" if r["forced"] else ""))
        for w in r["warnings"]:
            click.echo(f"  warning: {w}")
        _verify_composite(p, no_verify)

    def _verify_composite(p, no_verify: bool) -> None:
        """Full suite against the composite after a flip/unflip (equivalence.run_composite), when it is up."""
        if no_verify:
            return
        if not service_urls(p).get("composite"):
            click.echo("  composite harness not up: run `harness up PROJECT --composite` then "
                       "`equivalence composite PROJECT`")
            return
        from .equivalence import EquivalenceError, echo_composite, run_composite
        try:
            rep = run_composite(p)
        except EquivalenceError as e:
            raise click.ClickException(f"composite suite: {e}")
        echo_composite(rep)
        if not rep.ok:
            raise SystemExit(1)

    @groups.harness.command("unflip")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.option("--by", "actor", default="human", show_default=True)
    @click.option("--reason", default="")
    @click.option("--no-verify", is_flag=True, help="Do not run the composite suite after the unflip.")
    def unflip_cmd(project: Path, slice_id: str, actor: str, reason: str, no_verify: bool):
        """Route a slice's endpoints back to the legacy service."""
        p = _project(project)
        _gate(p)
        try:
            r = unflip(p, slice_id, actor=actor, reason=reason)
        except HarnessError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: routed back to legacy; flipped now: {', '.join(r['flipped']) or 'none'}")
        _verify_composite(p, no_verify)

    @groups.harness.command("routes")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def routes_cmd(project: Path, fmt: str):
        """Regenerate and show the strangler route table (harness/routes.json)."""
        p = _project(project)
        try:
            t = write_routes(p)
        except HarnessError as e:
            raise click.ClickException(str(e))
        if fmt == "json":
            click.echo(json.dumps(t, indent=2))
            return
        click.echo(f"default: legacy; flipped: {', '.join(t['flipped']) or 'none'}"
                   + (f"; stale (approval missing): {', '.join(t['stale_flips'])}" if t["stale_flips"] else ""))
        for r in t["routes"]:
            click.echo(f"  {r['method']:6} {r['path']:40} -> {r['upstream']:6} ({r['slice']} {r['endpoint']})")
