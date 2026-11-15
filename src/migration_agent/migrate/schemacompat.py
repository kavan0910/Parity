"""Schema compatibility, declared schema changes and data blind spots (docs/phase2-plan.md section 9, M6).

Default (decision P2-Q10): NO schema change. The new service must run on the legacy schema as-is.

`migration-agent config schema-check PROJECT [--static]` (on the `config` group, next to `config map`):

1. Legacy schema. Sandbox (default): the legacy migrations are applied to an EPHEMERAL database inside the exec
   sandbox (ExecJob, phase test, network none; engine from harness.yaml `db.engine`):
   - sqlite: in a copy of the legacy build workspace, run `db.migrate` (legacy image) or apply the legacy SQL
     migrations (`schema_check.sql_files`, default the Flyway-style files Phase 1 extracted) with the schema probe
     (`migrate/router/schema_probe.py`, router image), which also applies declared expand SQL and dumps the schema;
   - postgres / mysql: a throw-away db container on an `--internal` network, `db.migrate` in the legacy image,
     introspection with psql / mysql inside the db container;
   - none: nothing to introspect (in-process / ORM-created schema): falls back to the static view.
   `--static`: no sandbox; the legacy schema comes from Phase 1 facts (schema_entity items, data_entity nodes).
2. Expected schema. Static scan of target_code/ (Flyway/Alembic/Prisma/TypeORM via extract/schemas.py plus light
   JPA @Entity and Django models scanners) and, when a successful `harness build --new` exists, the ORM's own check
   run in the new image against a copy of the migrated database: Hibernate `ddl-auto=validate` (Spring),
   `manage.py makemigrations --check --dry-run` (Django), `prisma migrate diff --exit-code` (Prisma, sqlite), or
   `schema_check.orm_check` from harness.yaml. Otherwise only the generic table/column diff.
3. Declared changes (opt-in): data/schema_changes.yaml (`SchemaChangesFile`; target.yaml cannot carry them because
   the shared models are frozen). Each change becomes an expand/contract spec (generic SQL, validated identifiers);
   expand SQL is applied only to the ephemeral, synthetic sandbox DB; contract steps run after every slice touching
   the table is migrated and are listed as cut-over steps. data/mapping.yaml (`MappingFile`, old column -> new
   column + transform) is checked on its synthetic fixtures only (`fixtures_are_synthetic: true` is required).

Output data/schema_compat.json (`SchemaCompatReport`) + data/schema_compat.md. Status: compatible | incompatible |
unverified (static only / nothing to compare) | skipped (no database) | invalid (bad declarations) | error.

`config schema-blockers PROJECT` / `schema_blockers(project) -> {slice_id: [reasons]}`: Phase 1 blind spots.
Stored procedures / triggers that the code calls or references but whose definition is not in the repository (nor
in a user-supplied export listed in `schema_export:`) block every slice whose nodes contain the calling code;
tables declared in `blind_spots:` and tables with blocking schema-compat diffs block every slice touching them
(data_entity nodes in the slice, or reads/writes edges from its nodes). `accepted:` entries suppress a name.

data/schema_changes.yaml:

    schema_version: 1
    changes:
      - {id: SC-001, kind: add_column, table: loans, column: due_reminder_sent, type: BOOLEAN, default: "FALSE",
         nullable: false, reason: "reminder job"}
      - {id: SC-002, kind: rename_column, table: members, column: mail, new_name: email, type: VARCHAR(200),
         reason: naming}
    blind_spots: [{table: ledger, reason: "trigger trg_ledger_audit lives only in production"}]
    schema_export: [data/prod_schema.sql]          # project-relative, under data/
    accepted: [{name: sp_refresh_totals, reason: "no-op in the sandbox", by: alice}]
"""
from __future__ import annotations

import json
import os
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SQL_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,30}(\(\s*\d{1,5}(\s*,\s*\d{1,5})?\s*\))?$")
_SQL_DEFAULT = re.compile(r"^(NULL|TRUE|FALSE|-?\d{1,18}(\.\d{1,18})?|'[^'\\;\n]{0,100}'|CURRENT_TIMESTAMP)$", re.I)
BOOKKEEPING = frozenset({"flyway_schema_history", "django_migrations", "alembic_version", "_prisma_migrations",
                         "sequelizemeta", "sqlite_sequence", "schema_migrations", "knex_migrations",
                         "knex_migrations_lock", "typeorm_metadata", "migrations", "databasechangelog",
                         "databasechangeloglock", "goose_db_version"})
_SKIP_DIRS = frozenset({".git", "node_modules", "target", "build", "dist", ".venv", "venv", "vendor", "__pycache__",
                        ".gradle", ".idea", "bin", "obj", ".tox"})
MAX_FILE = 1_000_000


class SchemaCompatError(RuntimeError):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ declared changes (data/schema_changes.yaml)
ChangeKind = Literal["add_column", "rename_column", "drop_column", "change_type", "add_table", "drop_table"]


def _ident(v: str | None) -> str | None:
    if v is not None and not _IDENT.match(v):
        raise ValueError(f"invalid SQL identifier {v!r} (letters, digits, _; max 63)")
    return v


class SchemaChange(_M):
    id: str = Field(pattern=r"^SC-\d{3,}$")
    kind: ChangeKind
    table: str
    column: str | None = None
    new_name: str | None = None
    type: str | None = None
    columns: dict[str, str] = Field(default_factory=dict)      # add_table: column -> type
    nullable: bool = True
    default: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("table", "column", "new_name")
    @classmethod
    def _names(cls, v):
        return _ident(v)

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        if v is not None and not _SQL_TYPE.match(v):
            raise ValueError(f"invalid SQL type {v!r}")
        return v

    @field_validator("columns")
    @classmethod
    def _cols(cls, v):
        for k, t in v.items():
            _ident(k)
            if not _SQL_TYPE.match(t):
                raise ValueError(f"invalid SQL type {t!r} for column {k}")
        return v

    @field_validator("default")
    @classmethod
    def _default(cls, v):
        if v is not None and not _SQL_DEFAULT.match(v.strip()):
            raise ValueError(f"default {v!r} must be NULL, TRUE, FALSE, a number, 'text' or CURRENT_TIMESTAMP")
        return v if v is None else v.strip()

    @model_validator(mode="after")
    def _shape(self):
        k = self.kind
        if k in ("add_column", "rename_column", "drop_column", "change_type") and not self.column:
            raise ValueError(f"{self.id}: {k} needs column")
        if k in ("add_column", "change_type", "rename_column") and not self.type:
            raise ValueError(f"{self.id}: {k} needs type")
        if k == "rename_column" and not self.new_name:
            raise ValueError(f"{self.id}: rename_column needs new_name")
        if k == "add_table" and not self.columns:
            raise ValueError(f"{self.id}: add_table needs columns")
        if k == "add_column" and not self.nullable and self.default is None:
            pass   # expand adds it nullable; contract sets NOT NULL after the backfill
        return self


class BlindSpot(_M):
    table: str
    reason: str = Field(min_length=1)


class Accepted(_M):
    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    by: str = "human"


class SchemaChangesFile(_M):
    schema_version: Literal[1] = 1
    changes: list[SchemaChange] = Field(default_factory=list)
    blind_spots: list[BlindSpot] = Field(default_factory=list)
    schema_export: list[str] = Field(default_factory=list)
    accepted: list[Accepted] = Field(default_factory=list)

    @field_validator("schema_export")
    @classmethod
    def _exports(cls, v):
        for p in v:
            s = p.replace("\\", "/")
            if not s.startswith("data/") or ".." in s.split("/"):
                raise ValueError(f"schema_export {p!r} must be a project-relative path under data/")
        return v

    @model_validator(mode="after")
    def _unique(self):
        ids = [c.id for c in self.changes]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate change id")
        return self


# ------------------------------------------------------------------ data mapping (data/mapping.yaml)
Transform = Literal["identity", "lower", "upper", "trim", "to_int", "to_float", "to_str", "concat",
                    "split_first", "split_last", "cents_to_units", "units_to_cents", "default_if_null"]


class MappingFixture(_M):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    input: dict[str, Any] = Field(alias="in")
    out: Any = None


class DataMapping(_M):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    table: str
    from_columns: list[str] = Field(alias="from")
    to: str
    transform: Transform = "identity"
    params: dict[str, Any] = Field(default_factory=dict)
    change: str | None = None
    fixtures: list[MappingFixture] = Field(default_factory=list)

    @field_validator("from_columns", mode="before")
    @classmethod
    def _list(cls, v):
        return [v] if isinstance(v, str) else v

    @field_validator("table", "to")
    @classmethod
    def _n(cls, v):
        return _ident(v)

    @field_validator("from_columns")
    @classmethod
    def _ns(cls, v):
        if not v:
            raise ValueError("from needs at least one column")
        return [_ident(x) for x in v]

    @model_validator(mode="after")
    def _params(self):
        if self.transform in ("concat", "split_first", "split_last"):
            sep = self.params.get("sep", " ")
            if not isinstance(sep, str) or not re.fullmatch(r"[^'\\;\n]{0,5}", sep):
                raise ValueError(f"{self.id}: sep must be a short string without quotes")
        if self.transform != "concat" and len(self.from_columns) != 1:
            raise ValueError(f"{self.id}: {self.transform} takes exactly one source column")
        if self.transform == "default_if_null" and "value" not in self.params:
            raise ValueError(f"{self.id}: default_if_null needs params.value")
        return self


class MappingFile(_M):
    schema_version: Literal[1] = 1
    fixtures_are_synthetic: Literal[True]
    mappings: list[DataMapping] = Field(default_factory=list)


# ------------------------------------------------------------------ report
class ColumnInfo(_M):
    type: str = ""
    nullable: bool | None = None
    pk: bool = False


class TableInfo(_M):
    columns: dict[str, ColumnInfo] = Field(default_factory=dict)
    columns_known: bool = True
    source: str = ""


class SchemaDiff(_M):
    kind: Literal["missing_table", "missing_column", "extra_table", "extra_column", "type_mismatch",
                  "unmapped_not_null"]
    table: str
    column: str = ""
    legacy: str = ""
    expected: str = ""
    severity: Literal["blocker", "warning", "info"]
    note: str = ""


class OrmCheck(_M):
    tool: str = ""
    command: list[str] = Field(default_factory=list)
    run_id: str | None = None
    ok: bool | None = None
    exit_code: int | None = None
    output_tail: str = ""
    skipped: str = ""


class ChangeSpec(_M):
    id: str
    kind: ChangeKind
    table: str
    expand: list[str] = Field(default_factory=list)
    backfill: list[str] = Field(default_factory=list)
    contract: list[str] = Field(default_factory=list)
    contract_after: list[str] = Field(default_factory=list)
    cutover_step: str = ""


class MappingCheck(_M):
    id: str
    table: str
    to: str
    transform: str
    sql: str = ""
    fixtures: int = 0
    passed: int = 0
    failures: list[str] = Field(default_factory=list)
    status: Literal["passed", "failed", "untested"]


class SchemaCompatReport(_M):
    schema_version: Literal[1] = 1
    created: str = ""
    engine: str = "none"
    mode: Literal["sandbox", "static"] = "sandbox"
    status: Literal["compatible", "incompatible", "unverified", "skipped", "invalid", "error"]
    legacy_source: str = ""
    expected_source: str = ""
    legacy_tables: dict[str, TableInfo] = Field(default_factory=dict)
    expected_tables: dict[str, TableInfo] = Field(default_factory=dict)
    diffs: list[SchemaDiff] = Field(default_factory=list)
    orm_check: OrmCheck = Field(default_factory=OrmCheck)
    runs: list[str] = Field(default_factory=list)
    changes: list[ChangeSpec] = Field(default_factory=list)
    mappings: list[MappingCheck] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def blockers(self) -> list[SchemaDiff]:
        return [d for d in self.diffs if d.severity == "blocker"]


# ------------------------------------------------------------------ loading
def _layout(project):
    from .paths import Phase2Layout
    return Phase2Layout(project)


def schema_changes_path(project) -> Path:
    return _layout(project).data_dir / "schema_changes.yaml"


def _load_yaml_model(path: Path, model, what: str):
    import yaml
    from pydantic import ValidationError
    if path.stat().st_size > MAX_FILE:
        raise SchemaCompatError(f"{what} is larger than {MAX_FILE} bytes")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SchemaCompatError(f"{what} is not valid YAML: {e}") from None
    try:
        return model.model_validate(data if data is not None else {})
    except ValidationError as e:
        raise SchemaCompatError(f"{what} is invalid: " + "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())) from None


def load_schema_changes(project) -> SchemaChangesFile:
    p = schema_changes_path(project)
    return SchemaChangesFile() if not p.exists() else _load_yaml_model(p, SchemaChangesFile,
                                                                       "data/schema_changes.yaml")


def load_mapping(project) -> MappingFile | None:
    p = _layout(project).data_mapping
    return None if not p.exists() else _load_yaml_model(p, MappingFile, "data/mapping.yaml")


def _norm(name: str) -> str:
    return re.sub(r'["`\[\]]', "", str(name)).split(".")[-1].strip().lower()


def _inventory(project) -> list[dict]:
    p = Path(project.inventory_path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("items", [])
    except ValueError:
        return []


def _merge_found(tables: dict[str, TableInfo], name: str, cols: list, source: str) -> None:
    t = tables.setdefault(_norm(name), TableInfo(source=source))
    for c in cols or []:
        cn = c.get("name") if isinstance(c, dict) else str(c)
        if cn:
            t.columns.setdefault(_norm(cn), ColumnInfo(type=str((c.get("type") if isinstance(c, dict) else "") or "")))


def legacy_static_schema(project, graph=None) -> dict[str, TableInfo]:
    """Phase 1 facts: schema_entity items (create/alter merged) + data_entity nodes (columns unknown)."""
    tables: dict[str, TableInfo] = {}
    for it in _inventory(project):
        if it.get("kind") == "schema_entity" and (it.get("attrs") or {}).get("source") != "proto":
            _merge_found(tables, it.get("name", ""), (it.get("attrs") or {}).get("columns"),
                         f"facts:{(it.get('attrs') or {}).get('source', '')}")
    for n in (graph.nodes if graph is not None else []):
        if n.type == "data_entity" and _norm(n.name) not in tables:
            tables[_norm(n.name)] = TableInfo(columns_known=False, source="graph:data_entity")
    return tables


def _flyway_files(project) -> list[str]:
    files = {it["file"] for it in _inventory(project) if it.get("kind") == "schema_entity" and it.get("file")
             and (it.get("attrs") or {}).get("source") == "flyway"}

    def key(f: str):
        m = re.search(r"V(\d+(?:[._]\d+)*)__", Path(f).name)
        return (0, [int(x) for x in re.split(r"[._]", m.group(1))], f) if m else (1, [], f)
    return sorted(files, key=key)


# ------------------------------------------------------------------ target ORM expectations (static)
def _snake(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", s).lower()


_JAVA_TYPES = {"long": "bigint", "integer": "integer", "int": "integer", "short": "smallint", "string": "varchar",
               "boolean": "boolean", "localdate": "date", "localdatetime": "timestamp", "instant": "timestamp",
               "offsetdatetime": "timestamp", "bigdecimal": "numeric", "double": "double", "float": "real",
               "uuid": "uuid", "byte[]": "blob"}
_DJANGO_TYPES = {"CharField": "varchar", "TextField": "text", "EmailField": "varchar", "SlugField": "varchar",
                 "URLField": "varchar", "IntegerField": "integer", "PositiveIntegerField": "integer",
                 "BigIntegerField": "bigint", "SmallIntegerField": "smallint", "AutoField": "integer",
                 "BigAutoField": "bigint", "BooleanField": "bool", "DateTimeField": "datetime", "DateField": "date",
                 "DecimalField": "decimal", "FloatField": "real", "UUIDField": "char(32)", "JSONField": "text",
                 "PositiveSmallIntegerField": "smallint", "TimeField": "time"}


def scan_jpa(text: str) -> dict[str, TableInfo]:
    out: dict[str, TableInfo] = {}
    if "@Entity" not in text:
        return out
    m = re.search(r"\bclass\s+(\w+)", text)
    if not m:
        return out
    tm = re.search(r'@Table\s*\([^)]*name\s*=\s*"([^"]+)"', text)
    table = _norm(tm.group(1)) if tm else _snake(m.group(1))
    t = TableInfo(source="target:jpa")
    pending: list[str] = []
    for line in text[m.end():].splitlines():
        s = line.strip()
        if s.startswith("@"):
            pending.append(s)
            continue
        fm = re.match(r"(?:private|protected|public)\s+(?!static\b)(?:final\s+)?([\w.<>\[\], ?]+?)\s+(\w+)\s*[;=]", s)
        if fm:
            ann = " ".join(pending)
            pending = []
            if re.search(r"@(Transient|OneToMany|ManyToMany|Embedded)\b", ann):
                continue
            cm = re.search(r'@(?:Column|JoinColumn)\s*\([^)]*name\s*=\s*"([^"]+)"', ann)
            if cm:
                col = _norm(cm.group(1))
            elif re.search(r"@(ManyToOne|OneToOne)\b", ann):
                col = _snake(fm.group(2)) + "_id"
            else:
                col = _snake(fm.group(2))
            typ = _JAVA_TYPES.get(fm.group(1).split(".")[-1].lower(), "")
            t.columns[col] = ColumnInfo(type=typ, pk="@Id" in ann)
        elif s and not s.startswith(("//", "*", "/*")):
            pending = []
    out[table] = t
    return out


def scan_django_models(rel: str, text: str) -> dict[str, TableInfo]:
    out: dict[str, TableInfo] = {}
    if "models.Model" not in text:
        return out
    app = Path(rel).parent.name or "app"
    for cm in re.finditer(r"^class\s+(\w+)\s*\(([^)]*Model[^)]*)\):\s*$", text, re.M):
        body_start = cm.end()
        nxt = re.search(r"^\S", text[body_start:], re.M)
        body = text[body_start: body_start + (nxt.start() if nxt else len(text))]
        if re.search(r"^\s+abstract\s*=\s*True", body, re.M):
            continue
        dm = re.search(r"""^\s+db_table\s*=\s*['"]([^'"]+)['"]""", body, re.M)
        table = _norm(dm.group(1)) if dm else f"{app}_{cm.group(1).lower()}"
        t = TableInfo(source="target:django")
        if not re.search(r"primary_key\s*=\s*True", body):
            t.columns["id"] = ColumnInfo(type="integer", pk=True)
        for fm in re.finditer(r"^\s{4}(\w+)\s*=\s*models\.(\w+)\(([^\n]*)", body, re.M):
            name, kind, args = fm.groups()
            if kind == "ManyToManyField":
                continue
            if kind in ("ForeignKey", "OneToOneField"):
                cmn = re.search(r"""db_column\s*=\s*['"](\w+)['"]""", args)
                t.columns[cmn.group(1).lower() if cmn else f"{name.lower()}_id"] = ColumnInfo(type="integer")
                continue
            if kind not in _DJANGO_TYPES:
                continue
            cmn = re.search(r"""db_column\s*=\s*['"](\w+)['"]""", args)
            t.columns[cmn.group(1).lower() if cmn else name.lower()] = ColumnInfo(
                type=_DJANGO_TYPES[kind], pk="primary_key=True" in args.replace(" ", ""))
        out[table] = t
    return out


def target_expected_schema(project) -> dict[str, TableInfo]:
    """Static scan of target_code/ (never executed): migrations/ORM schemas the new service declares."""
    from ..extract.schemas import extract_schemas
    root = _layout(project).target_code
    tables: dict[str, TableInfo] = {}
    if not root.is_dir():
        return tables
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            low = fn.lower()
            if p.is_symlink() or not low.endswith((".sql", ".prisma", ".py", ".ts", ".js", ".java", ".kt")):
                continue
            try:
                if p.stat().st_size > MAX_FILE:
                    continue
                text = p.read_text(errors="replace")
            except OSError:
                continue
            if low.endswith((".java", ".kt")):
                for k, v in scan_jpa(text).items():
                    tables.setdefault(k, v)
                continue
            if low == "models.py":
                for k, v in scan_django_models(rel, text).items():
                    tables.setdefault(k, v)
            try:
                found = extract_schemas(rel, text)
            except Exception:  # noqa: BLE001 - static hint only
                found = []
            for f in found:
                if f.kind == "schema_entity":
                    _merge_found(tables, f.name, (f.attrs or {}).get("columns"), f"target:{(f.attrs or {}).get('source')}")
    return tables


# ------------------------------------------------------------------ diff
def _family(t: str) -> str:
    t = (t or "").lower()
    for fam, pats in (("bool", ("bool",)), ("int", ("int", "serial", "long")), ("numeric", ("numeric", "decimal")),
                      ("real", ("real", "float", "double")), ("time", ("date", "time")),
                      ("blob", ("blob", "bytea", "binary")), ("json", ("json",)), ("uuid", ("uuid",)),
                      ("text", ("char", "text", "string", "clob", "varchar"))):
        if any(p in t for p in pats):
            return fam
    return ""


_COMPAT = {frozenset({"bool", "int"}), frozenset({"numeric", "real"}), frozenset({"uuid", "text"}),
           frozenset({"json", "text"}), frozenset({"numeric", "int"})}


def diff_schemas(actual: dict[str, TableInfo], expected: dict[str, TableInfo]) -> list[SchemaDiff]:
    diffs: list[SchemaDiff] = []
    for tname in sorted(expected):
        if tname in BOOKKEEPING:
            continue
        et = expected[tname]
        at = actual.get(tname)
        if at is None:
            diffs.append(SchemaDiff(kind="missing_table", table=tname, expected=et.source, severity="blocker",
                                    note="the new ORM maps a table the legacy schema does not have"))
            continue
        if not at.columns_known or not et.columns_known:
            continue
        for c, ec in sorted(et.columns.items()):
            ac = at.columns.get(c)
            if ac is None:
                diffs.append(SchemaDiff(kind="missing_column", table=tname, column=c, expected=ec.type,
                                        severity="blocker", note="the new ORM maps a column the legacy schema lacks"))
                continue
            fa, fe = _family(ac.type), _family(ec.type)
            if fa and fe and fa != fe and frozenset({fa, fe}) not in _COMPAT:
                diffs.append(SchemaDiff(kind="type_mismatch", table=tname, column=c, legacy=ac.type,
                                        expected=ec.type, severity="warning"))
    if expected:
        for tname in sorted(set(actual) - set(expected) - BOOKKEEPING):
            diffs.append(SchemaDiff(kind="extra_table", table=tname, legacy=actual[tname].source, severity="info",
                                    note="legacy table not mapped by the new ORM (fine if the new service never uses it)"))
        for tname in sorted(set(actual) & set(expected)):
            at, et = actual[tname], expected[tname]
            if not at.columns_known or not et.columns_known:
                continue
            for c in sorted(set(at.columns) - set(et.columns)):
                ac = at.columns[c]
                if ac.nullable is False and not ac.pk:
                    diffs.append(SchemaDiff(kind="unmapped_not_null", table=tname, column=c, legacy=ac.type,
                                            severity="warning",
                                            note="NOT NULL column the new ORM does not map: its inserts will fail "
                                                 "unless the column has a default"))
                else:
                    diffs.append(SchemaDiff(kind="extra_column", table=tname, column=c, legacy=ac.type,
                                            severity="info"))
    return diffs


# ------------------------------------------------------------------ plan / graph helpers
def slice_tables(plan, graph) -> dict[str, set[str]]:
    """slice id -> normalized table names its nodes are or read/write (data_entity nodes)."""
    if plan is None or graph is None:
        return {}
    by_id = {n.id: n for n in graph.nodes}
    touches: dict[str, set[str]] = {}
    for e in graph.edges:
        if e.type in ("reads", "writes") and by_id.get(e.target) is not None and by_id[e.target].type == "data_entity":
            touches.setdefault(e.source, set()).add(_norm(by_id[e.target].name))
    out: dict[str, set[str]] = {}
    for sl in plan.slices:
        t: set[str] = set()
        for nid in sl.node_ids:
            n = by_id.get(nid)
            if n is not None and n.type == "data_entity":
                t.add(_norm(n.name))
            t |= touches.get(nid, set())
        out[sl.id] = t
    return out


def _plan_graph(project):
    from .harness import HarnessError, approved_graph, load_plan
    try:
        plan = load_plan(project)
    except HarnessError:
        plan = None
    try:
        graph = approved_graph(project)
    except HarnessError:
        graph = None
    return plan, graph


# ------------------------------------------------------------------ expand / contract specs
def _q(name: str) -> str:
    return f'"{name}"'


def mapping_sql(m: DataMapping) -> str:
    cols = [_q(c) for c in m.from_columns]
    c = cols[0]
    sep = str(m.params.get("sep", " "))
    t = m.transform
    if t == "identity":
        return c
    if t in ("lower", "upper", "trim"):
        return f"{t.upper()}({c})"
    if t == "to_int":
        return f"CAST({c} AS INTEGER)"
    if t == "to_float":
        return f"CAST({c} AS REAL)"
    if t == "to_str":
        return f"CAST({c} AS TEXT)"
    if t == "concat":
        return f" || '{sep}' || ".join(cols)
    if t == "cents_to_units":
        return f"({c} / 100.0)"
    if t == "units_to_cents":
        return f"CAST(ROUND({c} * 100) AS INTEGER)"
    if t == "default_if_null":
        v = m.params["value"]
        lit = "NULL" if v is None else str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else \
            ("TRUE" if v is True else "FALSE" if v is False else "'" + str(v).replace("'", "''") + "'")
        return f"COALESCE({c}, {lit})"
    return ""      # split_*: no portable SQL, the backfill is written by hand


def change_specs(changes: list[SchemaChange], tables_by_slice: dict[str, set[str]],
                 mappings: list[DataMapping] | None = None) -> list[ChangeSpec]:
    out = []
    for ch in changes:
        t = _q(ch.table)
        after = sorted(s for s, ts in tables_by_slice.items() if ch.table.lower() in ts)
        exp, back, con = [], [], []
        if ch.kind == "add_column":
            dflt = f" DEFAULT {ch.default}" if ch.default is not None else ""
            exp.append(f"ALTER TABLE {t} ADD COLUMN {_q(ch.column)} {ch.type}{dflt};")
            if not ch.nullable:
                con.append(f"ALTER TABLE {t} ALTER COLUMN {_q(ch.column)} SET NOT NULL;")
            step = f"{ch.id}: add {ch.table}.{ch.column} (expand before the first slice that writes it)"
        elif ch.kind == "rename_column":
            exp.append(f"ALTER TABLE {t} ADD COLUMN {_q(ch.new_name)} {ch.type};")
            m = next((x for x in (mappings or []) if x.change == ch.id or (x.table == ch.table and x.to == ch.new_name
                                                                         and x.from_columns == [ch.column])), None)
            expr = mapping_sql(m) if m is not None else _q(ch.column)
            back.append(f"UPDATE {t} SET {_q(ch.new_name)} = {expr or _q(ch.column)} WHERE {_q(ch.new_name)} IS NULL;")
            con.append(f"ALTER TABLE {t} DROP COLUMN {_q(ch.column)};")
            step = (f"{ch.id}: rename {ch.table}.{ch.column} -> {ch.new_name}: expand, dual-write both columns while "
                    "legacy and new run side by side, backfill, contract after the last slice touching the table")
        elif ch.kind == "drop_column":
            con.append(f"ALTER TABLE {t} DROP COLUMN {_q(ch.column)};")
            step = f"{ch.id}: stop reading {ch.table}.{ch.column} in the new code; drop it at cut-over"
        elif ch.kind == "change_type":
            tmp = _q(f"{ch.column}__v2")
            exp.append(f"ALTER TABLE {t} ADD COLUMN {tmp} {ch.type};")
            back.append(f"UPDATE {t} SET {tmp} = CAST({_q(ch.column)} AS {ch.type}) WHERE {tmp} IS NULL;")
            con += [f"ALTER TABLE {t} DROP COLUMN {_q(ch.column)};",
                    f"ALTER TABLE {t} RENAME COLUMN {tmp} TO {_q(ch.column)};"]
            step = f"{ch.id}: change {ch.table}.{ch.column} to {ch.type} via a shadow column"
        elif ch.kind == "add_table":
            cols = ", ".join(f"{_q(k)} {v}" for k, v in ch.columns.items())
            exp.append(f"CREATE TABLE {t} ({cols});")
            step = f"{ch.id}: create {ch.table}"
        else:
            con.append(f"DROP TABLE {t};")
            step = f"{ch.id}: drop {ch.table} after every slice that touches it is migrated"
        out.append(ChangeSpec(id=ch.id, kind=ch.kind, table=ch.table, expand=exp, backfill=back, contract=con,
                              contract_after=after, cutover_step=step))
    return out


def validate_changes(changes: list[SchemaChange], known: dict[str, TableInfo]) -> list[str]:
    errs = []
    for ch in changes:
        tbl = known.get(ch.table.lower())
        if ch.kind == "add_table":
            if tbl is not None:
                errs.append(f"{ch.id}: table {ch.table} already exists")
            continue
        if tbl is None:
            errs.append(f"{ch.id}: unknown table {ch.table}")
            continue
        if tbl.columns_known and ch.column:
            has = ch.column.lower() in tbl.columns
            if ch.kind == "add_column" and has:
                errs.append(f"{ch.id}: {ch.table}.{ch.column} already exists")
            if ch.kind in ("rename_column", "drop_column", "change_type") and not has:
                errs.append(f"{ch.id}: unknown column {ch.table}.{ch.column}")
            if ch.kind == "rename_column" and ch.new_name.lower() in tbl.columns:
                errs.append(f"{ch.id}: {ch.table}.{ch.new_name} already exists")
    return errs


def apply_expand(schema: dict[str, TableInfo], changes: list[SchemaChange]) -> dict[str, TableInfo]:
    """The schema after the expand steps (what both services see while strangling)."""
    out = {k: v.model_copy(deep=True) for k, v in schema.items()}
    for ch in changes:
        t = out.get(ch.table.lower())
        if ch.kind == "add_table":
            out[ch.table.lower()] = TableInfo(columns={k.lower(): ColumnInfo(type=v) for k, v in ch.columns.items()},
                                              source=f"declared:{ch.id}")
        elif t is not None and ch.kind in ("add_column", "rename_column"):
            col = ch.column if ch.kind == "add_column" else ch.new_name
            t.columns[col.lower()] = ColumnInfo(type=ch.type or "", nullable=True)
        elif t is not None and ch.kind == "change_type":
            t.columns[f"{ch.column}__v2".lower()] = ColumnInfo(type=ch.type or "", nullable=True)
    return out


# ------------------------------------------------------------------ mapping checks (synthetic fixtures only)
def apply_transform(m: DataMapping, row: dict[str, Any]):
    vals = [row.get(c) for c in m.from_columns]
    v = vals[0]
    sep = str(m.params.get("sep", " "))
    t = m.transform
    if t == "concat":
        return sep.join(str(x) for x in vals if x is not None)
    if t == "default_if_null":
        return m.params["value"] if v is None else v
    if v is None:
        return None
    if t == "identity":
        return v
    if t == "lower":
        return str(v).lower()
    if t == "upper":
        return str(v).upper()
    if t == "trim":
        return str(v).strip()
    if t == "to_int":
        return int(str(v).strip())
    if t == "to_float":
        return float(str(v).strip())
    if t == "to_str":
        return str(v)
    if t == "split_first":
        return str(v).split(sep)[0]
    if t == "split_last":
        return str(v).split(sep)[-1]
    if t == "cents_to_units":
        return float(Decimal(str(v)) / 100)
    if t == "units_to_cents":
        return int((Decimal(str(v)) * 100).to_integral_value())
    raise ValueError(f"unknown transform {t}")


def run_mapping_checks(mf: MappingFile | None) -> list[MappingCheck]:
    from ..ingest.secrets import redact_text
    out = []
    for m in (mf.mappings if mf is not None else []):
        fails, passed = [], 0
        for i, fx in enumerate(m.fixtures):
            blob = json.dumps(fx.input, default=str) + " " + json.dumps(fx.out, default=str)
            if redact_text("", blob)[1]:
                fails.append(f"fixture {i}: value looks like a real credential; fixtures must be synthetic")
                continue
            try:
                got = apply_transform(m, fx.input)
            except (ValueError, TypeError, InvalidOperation) as e:
                fails.append(f"fixture {i}: {type(e).__name__}: {str(e)[:120]}")
                continue
            if got == fx.out or (isinstance(got, float) and isinstance(fx.out, (int, float))
                                 and abs(got - fx.out) < 1e-9):
                passed += 1
            else:
                fails.append(f"fixture {i}: expected {json.dumps(fx.out, default=str)[:80]}, got "
                             f"{json.dumps(got, default=str)[:80]}")
        status = "untested" if not m.fixtures else ("passed" if not fails else "failed")
        out.append(MappingCheck(id=m.id, table=m.table, to=m.to, transform=m.transform, sql=mapping_sql(m),
                                fixtures=len(m.fixtures), passed=passed, failures=fails, status=status))
    return out


# ------------------------------------------------------------------ sandbox execution
def _tables_from_probe(d: dict) -> dict[str, TableInfo]:
    out = {}
    for name, t in (d.get("tables") or {}).items():
        cols = {}
        for c, info in ((t or {}).get("columns") or {}).items():
            info = info if isinstance(info, dict) else {}
            cols[_norm(c)] = ColumnInfo(type=str(info.get("type", "")), nullable=info.get("nullable"),
                                        pk=bool(info.get("pk")))
        out[_norm(name)] = TableInfo(columns=cols, source="sandbox")
    return out


def parse_rows(text: str) -> dict[str, TableInfo]:
    """psql -At -F '\\t' / mysql -N -B output: table, column, type, is_nullable per line."""
    out: dict[str, TableInfo] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not _IDENT.match(parts[0].split(".")[-1]) or not parts[1]:
            continue
        t = out.setdefault(_norm(parts[0]), TableInfo(source="sandbox"))
        nullable = None if len(parts) < 4 else parts[3].strip().upper() in ("YES", "TRUE", "1")
        t.columns[_norm(parts[1])] = ColumnInfo(type=parts[2].strip(), nullable=nullable)
    return out


def _read_report(project, res, name: str) -> dict | None:
    root = Path(project.root)
    for ref in res.reports:
        if ref.endswith("/" + name) or ref.endswith(name):
            try:
                return json.loads((root / ref).read_text())
            except (OSError, ValueError):
                return None
    if res.stdout_ref:
        try:
            return json.loads((root / res.stdout_ref).read_text())
        except (OSError, ValueError):
            pass
    try:
        return json.loads(res.stdout_tail) if res.stdout_tail.strip().startswith("{") else None
    except ValueError:
        return None


def _job(project, image, command, *, limits, workdir, mounts=(), env=None, synth=(), stage=""):
    from .ids import allocate
    from .models import ExecJob
    return ExecJob(run_id=allocate(project, "RUN"), image=image, command=command, phase="test", network="none",
                   workdir=workdir, mounts=list(mounts), limits=limits, env=dict(env or {}),
                   synthetic_secrets=list(synth), labels={"harness": "schema", "stage": stage})


def _copy_ws(project, src_rel: str, run_id: str) -> Path:
    from .harness import _copy_workspace
    lay = _layout(project)
    ws = lay.run_work(run_id) / "app"
    lay.ensure(ws.parent)
    src = Path(project.root) / src_rel
    if not src.is_dir():
        raise SchemaCompatError(f"build workspace {src_rel} is missing: rebuild the harness")
    _copy_workspace(src, ws)
    return ws


def _expand_sql_text(specs: list[ChangeSpec]) -> str:
    lines = [f"-- {s.id} expand (synthetic sandbox DB only)\n" + "\n".join(s.expand) for s in specs if s.expand]
    return "\n".join(lines) + ("\n" if lines else "")


def _sqlite_run(project, spec, b, specs: list[ChangeSpec], report: SchemaCompatReport) -> Path | None:
    """Legacy migrations + expand SQL on an ephemeral SQLite file; returns the migrated DB file (host path)."""
    from .harness import ensure_router_image, load_state
    from .models import Mount
    lay = _layout(project)
    st = load_state(project)
    leg_ent = st.get("legacy") or {}
    if not leg_ent.get("ok"):
        raise SchemaCompatError("schema-check needs a successful legacy build (`harness build PROJECT --legacy`) "
                                "or --static")
    leg = spec.legacy
    sc = spec.schema_check
    sql_files = sc.sql_files or ([] if spec.db.migrate else _flyway_files(project))
    if not spec.db.migrate and not sql_files:
        report.notes.append("legacy schema is created by the application at start-up (no db.migrate and no SQL "
                            "migrations): introspection skipped, static view used")
        return None
    from .ids import allocate
    ws_run = allocate(project, "RUN")
    ws = _copy_ws(project, leg_ent["workspace"], ws_run)
    ws_mount = Mount(source=lay.rel(ws), target=leg.workdir, read_only=False)
    if spec.db.migrate:
        j = _job(project, leg.image, spec.db.migrate, limits=leg.limits, workdir=leg.workdir, mounts=[ws_mount],
                 env=leg.env, synth=leg.synthetic_secrets, stage="legacy_migrate")
        res = b.run(j)
        report.runs.append(res.run_id)
        if not res.ok:
            report.status = "error"
            report.notes.append(f"legacy migrations failed ({res.run_id}): "
                                f"{(res.error or res.stderr_tail).strip()[-300:]}")
            return None
    img = ensure_router_image(project, spec, b)
    probe = _job(project, img["image"], ["python", "/app/schema_probe.py"], limits=spec.router.limits,
                 workdir="/tmp", stage="introspect")      # NOT /app: a tmpfs there would mask /app/schema_probe.py
    mounts = [ws_mount]
    apply: list[str] = []
    if sql_files:
        mounts.append(Mount(source="legacy_src", target="/work/legacy", read_only=True))
        apply += [f"/work/legacy/{f}" for f in sql_files]
    exp = _expand_sql_text(specs)
    if exp:
        sql_dir = lay.run_work(probe.run_id) / "sql"
        from .state import atomic_write_text
        atomic_write_text(sql_dir / "expand.sql", exp)
        mounts.append(Mount(source=lay.rel(sql_dir), target="/work/sql", read_only=True))
        apply.append("/work/sql/expand.sql")
    cmd = ["python", "/app/schema_probe.py", "--sqlite", f"{leg.workdir}/{sc.sqlite_path}"]
    for a in apply:
        cmd += ["--apply", a]
    cmd += ["--out", "/reports/schema.json"]
    probe = probe.model_copy(update={"command": cmd, "mounts": mounts})
    res = b.run(probe)
    report.runs.append(res.run_id)
    data = _read_report(project, res, "schema.json")
    if data is None or (not res.ok and not data.get("tables")):
        report.status = "error"
        report.notes.append(f"schema probe failed ({res.run_id}): "
                            f"{(res.error or (data or {}).get('error') or res.stderr_tail).strip()[-300:]}")
        return None
    if not data.get("ok", True):
        report.status = "error"
        report.notes.append(f"applying SQL failed: {str(data.get('error', ''))[:300]}")
    report.legacy_tables = _tables_from_probe(data)
    report.legacy_source = "sandbox:sqlite"
    if data.get("triggers"):
        report.notes.append("triggers in the migrated schema: " + ", ".join(
            f"{t.get('name')} on {t.get('table')}" for t in data["triggers"]))
    return ws / sc.sqlite_path


_PG_SQL = ("SELECT c.table_name, c.column_name, c.data_type, c.is_nullable FROM information_schema.columns c "
           "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
           "WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema') AND t.table_type = 'BASE TABLE' "
           "ORDER BY c.table_name, c.ordinal_position")
_MY_SQL = ("SELECT c.table_name, c.column_name, c.column_type, c.is_nullable FROM information_schema.columns c "
           "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
           "WHERE c.table_schema = DATABASE() AND t.table_type = 'BASE TABLE' "
           "ORDER BY c.table_name, c.ordinal_position")


def _db_argv(spec, sql: str) -> list[str]:
    if spec.db.engine == "postgres":
        user = spec.db.env.get("POSTGRES_USER", "postgres")
        return ["psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", spec.db.env.get("POSTGRES_DB", user),
                "-At", "-F", "\t", "-c", sql]
    return ["sh", "-c", 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -D "$MYSQL_DATABASE" -N -B -e "$1"', "sh", sql]


def _server_run(project, spec, b, specs, report, orm_cb, *, sleep, clock) -> None:
    """postgres/mysql: throw-away db on an internal network, legacy migrations, expand SQL, introspection,
    ORM check (orm_cb(stack)) attached to the same network; always torn down."""
    from .exec.docker import synthetic_value
    from .harness import _Stack, load_state
    if not all(hasattr(b, a) for a in ("start_service", "exec_in", "ensure_internal_network")):
        raise SchemaCompatError(f"exec backend {getattr(b, 'name', '?')} cannot run a database service")
    st = load_state(project)
    leg_ent = st.get("legacy") or {}
    if spec.db.migrate and not leg_ent.get("ok"):
        raise SchemaCompatError("schema-check needs a successful legacy build (`harness build PROJECT --legacy`)")
    names = set(spec.db.synthetic_secrets) | set(spec.legacy.synthetic_secrets)
    try:
        from .harness import resolve_service
        names |= set(resolve_service(project, spec, "new").synthetic_secrets)
    except Exception:  # noqa: BLE001 - no new service: only legacy names
        pass
    values = {k: synthetic_value() for k in sorted(names)}
    stack = _Stack(project, b, "schema", spec, values, sleep, clock)
    try:
        stack.db(spec.legacy.healthcheck, spec.legacy.limits)
        if spec.db.migrate:
            from .ids import allocate
            ws = _copy_ws(project, leg_ent["workspace"], allocate(project, "RUN"))
            stack.migrate(spec.legacy, _layout(project).rel(ws), spec.db.migrate, [], tag="legacy")
        db_c = stack.containers[0] if stack.containers else None
        if db_c is None:
            raise SchemaCompatError(f"db engine {spec.db.engine} did not start")
        for s in specs:
            for stmt in s.expand:
                rc, out = b.exec_in(db_c, _db_argv(spec, stmt), timeout=60)
                if rc != 0:
                    report.status = "error"
                    report.notes.append(f"expand {s.id} failed: {out.strip()[-200:]}")
        rc, out = b.exec_in(db_c, _db_argv(spec, _PG_SQL if spec.db.engine == "postgres" else _MY_SQL), timeout=60)
        if rc != 0:
            report.status = "error"
            report.notes.append(f"introspection failed: {out.strip()[-300:]}")
        else:
            report.legacy_tables = parse_rows(out)
            report.legacy_source = f"sandbox:{spec.db.engine}"
            if len(out) >= 4000:
                report.notes.append("introspection output reached the 4 KiB exec cap and may be truncated")
        orm_cb(stack)
    finally:
        stack.teardown()


def _orm_plan(project, spec, target, new_svc, db_path_in_container: str | None) -> tuple[str, list[str], dict]:
    from .knowledge import split_framework
    sc = spec.schema_check
    if sc.orm_check:
        return "custom", list(sc.orm_check), dict(sc.orm_env)
    fw = split_framework(target.target.framework or "")[0] if target is not None else ""
    if fw == "django":
        return "django-makemigrations-check", ["python", "manage.py", "makemigrations", "--check", "--dry-run"], {}
    if fw.startswith("spring"):
        return "hibernate-validate", list(new_svc.start), {"SPRING_JPA_HIBERNATE_DDL_AUTO": "validate",
                                                           "SPRING_MAIN_WEB_APPLICATION_TYPE": "none"}
    if (_layout(project).target_code / "prisma" / "schema.prisma").is_file() and db_path_in_container:
        return "prisma-migrate-diff", ["npx", "--no-install", "prisma", "migrate", "diff", "--from-url",
                                       f"file:{db_path_in_container}", "--to-schema-datamodel",
                                       "prisma/schema.prisma", "--exit-code"], {}
    return "", [], {}


def _orm_check(project, spec, b, target, report: SchemaCompatReport, *, db_file: Path | None, stack=None) -> None:
    from .harness import HarnessError, load_state, resolve_service
    from .models import Mount
    st = load_state(project)
    new_ent = st.get("new") or {}
    if not new_ent.get("ok"):
        report.orm_check = OrmCheck(skipped="no successful new build (`harness build PROJECT --new`): ORM check "
                                            "not run")
        return
    try:
        new_svc = resolve_service(project, spec, "new")
    except HarnessError as e:
        report.orm_check = OrmCheck(skipped=str(e))
        return
    sqlite_rel = spec.schema_check.new_sqlite_path or spec.schema_check.sqlite_path
    in_container = f"{new_svc.workdir}/{sqlite_rel}" if spec.db.engine == "sqlite" else None
    tool, cmd, env = _orm_plan(project, spec, target, new_svc, in_container)
    if not cmd:
        report.orm_check = OrmCheck(skipped="no ORM check known for this target (set schema_check.orm_check in "
                                            "harness.yaml); generic introspection diff only")
        return
    if spec.db.engine == "sqlite" and db_file is None:
        report.orm_check = OrmCheck(tool=tool, command=cmd, skipped="no migrated legacy database to check against")
        return
    from .ids import allocate
    ws = _copy_ws(project, new_ent["workspace"], allocate(project, "RUN"))
    if db_file is not None:
        dst = ws / sqlite_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if db_file.exists():
            shutil.copyfile(db_file, dst)
            os.chmod(dst, 0o666)
    lay = _layout(project)
    j = _job(project, new_svc.image, cmd, limits=new_svc.limits, workdir=new_svc.workdir,
             mounts=[Mount(source=lay.rel(ws), target=new_svc.workdir, read_only=False)],
             env={**new_svc.env, **env}, synth=new_svc.synthetic_secrets, stage="orm_check")
    if stack is not None:
        res = b.run(j, internal_network=stack.net, secret_values=stack.sv(new_svc.synthetic_secrets))
    else:
        res = b.run(j)
    report.runs.append(res.run_id)
    from ..ingest.secrets import redact_string
    report.orm_check = OrmCheck(tool=tool, command=cmd, run_id=res.run_id, ok=res.ok, exit_code=res.exit_code,
                                output_tail=redact_string((res.stdout_tail + "\n" + res.stderr_tail).strip())[-2000:]
                                + (f"\n{res.error}" if res.error else ""))


# ------------------------------------------------------------------ main check
def _audit(project, action: str, **fields) -> None:
    from ..audit import AuditLog
    try:
        AuditLog(Path(project.root) / "audit" / "audit.jsonl").record("schemacompat", action, **fields)
    except OSError:
        pass


def check(project, *, backend=None, static: bool = False, sleep=None, clock=None) -> SchemaCompatReport:
    """Run the schema-compat check and write data/schema_compat.json + .md."""
    import time
    from .harness import HarnessError, _backend, load_spec
    from .state import atomic_write_json, atomic_write_text, now_iso
    from .target import TargetSpecError, load_target
    lay = _layout(project)
    plan, graph = _plan_graph(project)
    changes_file = load_schema_changes(project)
    mapping = load_mapping(project)
    try:
        target = load_target(project)
    except TargetSpecError:
        target = None
    try:
        spec = load_spec(project)
    except HarnessError as e:
        if not static:
            raise SchemaCompatError(f"{e} (or use --static)") from None
        spec = None
    report = SchemaCompatReport(created=now_iso(), engine=spec.db.engine if spec else "none",
                                mode="static" if static else "sandbox", status="unverified")
    legacy_static = legacy_static_schema(project, graph)
    errs = validate_changes(changes_file.changes, legacy_static) if changes_file.changes else []
    tbs = slice_tables(plan, graph)
    report.changes = change_specs(changes_file.changes, tbs, mapping.mappings if mapping else None)
    report.mappings = run_mapping_checks(mapping)
    report.validation_errors = errs
    if mapping is not None:
        known = apply_expand(legacy_static, changes_file.changes)
        for m in mapping.mappings:
            t = known.get(m.table)
            if t is None:
                report.validation_errors.append(f"mapping {m.id}: unknown table {m.table}")
            elif t.columns_known:
                miss = [c for c in m.from_columns if c.lower() not in t.columns]
                if miss:
                    report.validation_errors.append(f"mapping {m.id}: unknown column(s) {', '.join(miss)}")
    if not report.validation_errors and not static and spec is not None:
        b = _backend(project, backend)
        if spec.db.engine == "sqlite":
            db_file = _sqlite_run(project, spec, b, report.changes, report)
            _orm_check(project, spec, b, target, report, db_file=db_file)
        elif spec.shared_db:
            _server_run(project, spec, b, report.changes, report,
                        lambda stack: _orm_check(project, spec, b, target, report, db_file=None, stack=stack),
                        sleep=sleep or time.sleep, clock=clock or time.monotonic)
        else:
            report.notes.append("db engine none: no database to introspect (the schema lives in the service "
                                "process); static view used")
            report.orm_check = OrmCheck(skipped="db engine none")
    if not report.legacy_tables:
        report.legacy_tables = apply_expand(legacy_static, changes_file.changes)
        report.legacy_source = report.legacy_source or "static:facts"
    report.expected_tables = target_expected_schema(project)
    report.expected_source = "target_orm_static" if report.expected_tables else "none"
    report.diffs = diff_schemas(report.legacy_tables, report.expected_tables)
    sandboxed = report.legacy_source.startswith("sandbox")
    if report.validation_errors:
        report.status = "invalid"
    elif report.status == "error":
        pass
    elif report.blockers or report.orm_check.ok is False or any(m.status == "failed" for m in report.mappings):
        report.status = "incompatible"
    elif not report.legacy_tables and not report.expected_tables and report.engine == "none":
        report.status = "skipped"
    elif sandboxed and (report.orm_check.ok or report.expected_tables):
        report.status = "compatible"
    else:
        report.status = "unverified"
    lay.ensure(lay.data_dir)
    atomic_write_json(lay.schema_compat, report)
    atomic_write_text(lay.data_dir / "schema_compat.md", render_md(report))
    _audit(project, "schema_check", status=report.status, mode=report.mode, engine=report.engine,
           blockers=len(report.blockers), runs=report.runs, changes=[c.id for c in report.changes])
    return report


def render_md(r: SchemaCompatReport) -> str:
    out = ["# Schema compatibility", "", f"- Status: **{r.status}** ({r.mode}, engine `{r.engine}`)",
           f"- Legacy schema: {r.legacy_source or '-'} ({len(r.legacy_tables)} table(s))",
           f"- Expected by the new ORM: {r.expected_source or '-'} ({len(r.expected_tables)} table(s))"]
    oc = r.orm_check
    out.append(f"- ORM check: {oc.tool or '-'} " + (f"skipped: {oc.skipped}" if oc.skipped else
                                                    ("OK" if oc.ok else f"FAILED (exit {oc.exit_code})")
                                                    + (f" in {oc.run_id}" if oc.run_id else "")))
    for title, items in (("Validation errors", r.validation_errors), ("Notes", r.notes)):
        if items:
            out += ["", f"## {title}", "", *[f"- {x}" for x in items]]
    if r.diffs:
        out += ["", "## Differences", "", "| Severity | Kind | Table | Column | Legacy | Expected | Note |",
                "|---|---|---|---|---|---|---|"]
        out += [f"| {d.severity} | {d.kind} | {d.table} | {d.column} | {d.legacy} | {d.expected} | {d.note} |"
                for d in r.diffs]
    if r.changes:
        out += ["", "## Declared schema changes (expand / contract, synthetic data only)", ""]
        for c in r.changes:
            out += [f"### {c.id} {c.kind} {c.table}", "", f"- Cut-over step: {c.cutover_step}",
                    f"- Contract after: {', '.join(c.contract_after) or 'all slices (none touch the table)'}"]
            for name, stmts in (("expand", c.expand), ("backfill", c.backfill), ("contract", c.contract)):
                if stmts:
                    out += [f"- {name}:", "", "```sql", *stmts, "```", ""]
    if r.mappings:
        out += ["", "## Data mappings (synthetic fixtures)", ""]
        out += [f"- {m.id} {m.table}.{m.to} ({m.transform}): {m.status} {m.passed}/{m.fixtures}"
                + "".join(f"\n  - {f}" for f in m.failures) for m in r.mappings]
    return "\n".join(out) + "\n"


def load_report(project) -> SchemaCompatReport | None:
    p = _layout(project).schema_compat
    return SchemaCompatReport.model_validate_json(p.read_text()) if p.exists() else None


# ------------------------------------------------------------------ blind spots / blockers
_DEF = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?(?:CONSTRAINT\s+)?"
                  r"(PROCEDURE|FUNCTION|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`\[\]]+)"
                  r"(?:[^;]*?\bON\s+([\w.\"`\[\]]+))?", re.I | re.S)
_CALLS = (
    re.compile(r"""\bCALL\s+([A-Za-z_][\w.]*)\s*\(""", re.I),
    re.compile(r"""\{\s*\??=?\s*call\s+([A-Za-z_][\w.]*)""", re.I),
    re.compile(r"""\bcallproc\(\s*['"]([\w.]+)['"]"""),
    re.compile(r"""@Procedure\s*\(\s*(?:(?:procedureName|name|value)\s*=\s*)?"([\w.]+)\""""),
    re.compile(r"""createStoredProcedureQuery\(\s*"([\w.]+)\""""),
    re.compile(r"""procedureName\s*=\s*"([\w.]+)\""""),
    re.compile(r"""withProcedureName\(\s*"([\w.]+)\""""),
    re.compile(r"""\bEXEC(?:UTE)?\s+(?:PROCEDURE\s+)?((?:dbo\.)?(?:sp|usp|proc)_\w+)""", re.I),
)
_TRIGGER_REFS = (
    re.compile(r"\bALTER\s+TABLE\s+([\w.\"`\[\]]+)\s+(?:ENABLE|DISABLE)\s+TRIGGER\s+([\w.\"`\[\]]+)", re.I),
    re.compile(r"\b(?:DROP|ALTER)\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?([\w.\"`\[\]]+)(?:\s+ON\s+([\w.\"`\[\]]+))?", re.I),
)
_SCAN_EXT = (".sql", ".py", ".java", ".kt", ".js", ".ts", ".go", ".rb", ".cs", ".php", ".xml", ".groovy", ".scala",
             ".yml", ".yaml")


def _walk_text(root: Path):
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink() or not fn.lower().endswith(_SCAN_EXT):
                continue
            try:
                if p.stat().st_size > MAX_FILE:
                    continue
                yield p.relative_to(root).as_posix(), p.read_text(errors="replace")
            except OSError:
                continue


def find_blind_spots(project, changes_file: SchemaChangesFile | None = None) -> dict:
    """Procedure calls / trigger references in code/ whose definitions are not in the repo or a schema export.
    -> {"procedures": [{name, file, line}], "triggers": [{name, table, file, line}], "defined": [...]}"""
    changes_file = changes_file or load_schema_changes(project)
    defined: set[str] = set()
    texts = list(_walk_text(Path(project.code)))
    for rel in changes_file.schema_export:
        p = (Path(project.root) / rel).resolve()
        if p.is_file() and p.is_relative_to(Path(project.root).resolve()) and p.stat().st_size <= 10 * MAX_FILE:
            texts.append((rel, p.read_text(errors="replace")))
    for _rel, text in texts:
        for m in _DEF.finditer(text):
            defined.add(_norm(m.group(2)))
    accepted = {_norm(a.name) for a in changes_file.accepted}
    procs, trigs = [], []
    for rel, text in texts:
        if rel in changes_file.schema_export:
            continue
        for rx in _CALLS:
            for m in rx.finditer(text):
                n = _norm(m.group(1))
                if n not in defined and n not in accepted:
                    procs.append({"name": n, "file": rel, "line": text.count("\n", 0, m.start()) + 1})
        for i, rx in enumerate(_TRIGGER_REFS):
            for m in rx.finditer(text):
                if i == 0:
                    table, name = _norm(m.group(1)), _norm(m.group(2))
                else:
                    name, table = _norm(m.group(1)), _norm(m.group(2) or "")
                if name not in defined and name not in accepted:
                    trigs.append({"name": name, "table": table, "file": rel,
                                  "line": text.count("\n", 0, m.start()) + 1})
    uniq = lambda xs: [dict(t) for t in sorted({tuple(sorted(x.items())) for x in xs})]  # noqa: E731
    return {"procedures": uniq(procs), "triggers": uniq(trigs), "defined": sorted(defined)}


def schema_blockers(project) -> dict[str, list[str]]:
    """{slice_id: [reasons]} for slices blocked by data blind spots (see module docstring). Slices without
    blockers are omitted."""
    plan, graph = _plan_graph(project)
    if plan is None or graph is None:
        return {}
    changes_file = load_schema_changes(project)
    accepted = {_norm(a.name) for a in changes_file.accepted}
    spots = find_blind_spots(project, changes_file)
    tables = slice_tables(plan, graph)
    by_id = {n.id: n for n in graph.nodes}
    file_slices: dict[str, set[str]] = {}
    for sl in plan.slices:
        for nid in sl.node_ids:
            n = by_id.get(nid)
            if n is None:
                continue
            for f in {n.module, *(e.file for e in n.evidence)}:
                if f:
                    file_slices.setdefault(f, set()).add(sl.id)
    out: dict[str, list[str]] = {}

    def add(sid: str, reason: str) -> None:
        lst = out.setdefault(sid, [])
        if reason not in lst:
            lst.append(reason)
    for p in spots["procedures"]:
        for sid in sorted(file_slices.get(p["file"], ())):
            add(sid, f"calls stored procedure {p['name']} ({p['file']}:{p['line']}) whose definition is not in the "
                     "repository: supply a schema export (data/schema_changes.yaml schema_export) or accept it")
    blocked_tables: dict[str, str] = {}
    for t in spots["triggers"]:
        if t["table"]:
            blocked_tables.setdefault(t["table"], f"trigger {t['name']} on {t['table']} is referenced "
                                                  f"({t['file']}:{t['line']}) but not defined in the repository")
        for sid in sorted(file_slices.get(t["file"], ())):
            add(sid, f"references trigger {t['name']} ({t['file']}:{t['line']}) not defined in the repository")
    for bs in changes_file.blind_spots:
        if _norm(bs.table) not in accepted:
            blocked_tables.setdefault(_norm(bs.table), f"declared blind spot on table {bs.table}: {bs.reason}")
    rep = None
    try:
        rep = load_report(project)
    except ValueError:
        rep = None
    for d in (rep.blockers if rep is not None else []):
        blocked_tables.setdefault(d.table, f"schema-compat {d.kind} {d.table}{'.' + d.column if d.column else ''}")
    for sid, ts in tables.items():
        for t in sorted(ts & set(blocked_tables)):
            add(sid, blocked_tables[t])
    return {k: v for k, v in sorted(out.items())}


# ------------------------------------------------------------------ CLI
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

    @groups.config.command("schema-check")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--static", is_flag=True, help="No sandbox: compare Phase 1 facts with target_code/ statically.")
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def schema_check_cmd(project: Path, static: bool, fmt: str) -> None:
        """Check that the new service runs on the legacy schema (plus declared expand changes)."""
        from .harness import HarnessError
        p = _project(project)
        _gate(p)
        try:
            r = check(p, static=static)
        except (SchemaCompatError, HarnessError) as e:
            raise click.ClickException(str(e))
        click.echo(r.model_dump_json(indent=2) if fmt == "json" else render_md(r), nl=fmt == "json")
        if r.status in ("incompatible", "invalid", "error"):
            raise SystemExit(1)

    @groups.config.command("schema-blockers")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def schema_blockers_cmd(project: Path, fmt: str) -> None:
        """Slices blocked by stored procedures / triggers / tables outside the repository."""
        try:
            b = schema_blockers(_project(project))
        except SchemaCompatError as e:
            raise click.ClickException(str(e))
        if fmt == "json":
            click.echo(json.dumps(b, indent=2))
            return
        if not b:
            click.echo("no schema blockers")
        for sid, reasons in b.items():
            click.echo(f"{sid}:")
            for r in reasons:
                click.echo(f"  - {r}")
