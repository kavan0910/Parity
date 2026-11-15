"""M0 entry gate: read docs/migration_contract.json and check it is current (docs/phase2-plan.md section 1).

The contract schema has ONE definition, `verify.models.MigrationContract`, written by
review/approval.approve. This module only reads it; `contract_json_schema()` exports that same model.

Gate rules (`check_contract`), each failing rule is an error unless stated otherwise:
  1. docs/migration_contract.json exists, parses, and its schema_version is supported.
  2. graph_sha256 == sha256 of the referenced snapshot file; doc_sha256 == sha256 of the approved doc text
     below its approval header. Mismatch = files changed after approval (stale).
  3. graph_version == graph/latest.json version (and the current understanding doc is the approved one),
     unless `pin=True`: then a warning, and Phase 2 migrates from the approved snapshot.
  4. source_hash == current ingest_report.json source_hash (legacy source unchanged since approval).
     A contract without source_hash (v1, or no ingest report at approval) is a warning.
  5. every quirk / suspected_bug node of the approved snapshot has a decision in the contract.
  6. coverage complete (no unmapped inventory items), else an error unless `accept_unmapped=True` (warning,
     recorded in the audit log).
  7. deferred Verifier findings: a warning each, and returned in `blockers` with their item_ids so the
     planner can attach them to every slice that contains one of those items (plan 4.4).

    from parity_migrate.migrate.contract import check_contract, require_contract
    chk = check_contract(project)               # ContractCheck, never raises for gate failures
    contract = require_contract(project)        # MigrationContract or ContractError listing the errors
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..verify.models import MigrationContract

SUPPORTED_CONTRACT_VERSIONS = (1, 2)
CONTRACT_NAME = "migration_contract.json"


class ContractError(RuntimeError):
    def __init__(self, message: str, check: "ContractCheck | None" = None):
        super().__init__(message)
        self.check = check


class ContractBlocker(BaseModel):
    """A deferred Verifier finding the planner turns into slice blockers."""
    model_config = ConfigDict(extra="forbid")
    finding_id: str
    kind: str
    title: str = ""
    item_ids: list[str] = Field(default_factory=list)
    note: str = ""


class ContractCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[ContractBlocker] = Field(default_factory=list)
    graph_version: int | None = None          # contract graph version (None if unreadable)
    latest_graph_version: int | None = None
    contract_sha256: str = ""                 # sha256 of the contract file bytes (for cutover approval)
    pinned: bool = False


def contract_path(project) -> Path:
    return Path(project.docs) / CONTRACT_NAME


def contract_json_schema(out: str | Path | None = None) -> dict:
    """JSON Schema of `verify.models.MigrationContract`; written to `out` when given."""
    schema = MigrationContract.model_json_schema()
    if out is not None:
        from .state import atomic_write_text
        atomic_write_text(out, json.dumps(schema, indent=2) + "\n")
    return schema


def load_contract(project) -> MigrationContract:
    """Parse the contract. Raises ContractError (missing, invalid JSON/schema, unsupported version)."""
    p = contract_path(project)
    if not p.exists():
        raise ContractError(f"no {CONTRACT_NAME}: approve the Phase 1 understanding first "
                            "(`parity approve PROJECT --by NAME`)")
    try:
        data = json.loads(p.read_text())
    except (ValueError, UnicodeDecodeError) as e:
        raise ContractError(f"{CONTRACT_NAME} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ContractError(f"{CONTRACT_NAME} must be a JSON object")
    ver = data.get("schema_version", 1)
    if ver not in SUPPORTED_CONTRACT_VERSIONS:
        raise ContractError(f"{CONTRACT_NAME} schema_version {ver!r} is not supported "
                            f"(supported: {', '.join(map(str, SUPPORTED_CONTRACT_VERSIONS))})")
    try:
        return MigrationContract.model_validate(data)
    except ValidationError as e:
        first = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])
        raise ContractError(f"{CONTRACT_NAME} does not match the contract schema: {first}") from None


def _inside(project, rel: str) -> Path | None:
    """Resolve a project-relative path from the contract; None if absolute or escaping the project."""
    if not rel or rel.startswith(("/", "\\")):
        return None
    p = (Path(project.root) / rel).resolve()
    return p if p.is_relative_to(Path(project.root).resolve()) else None


def approved_doc_body(text: str) -> str:
    """The doc text an approval hashed: the approved copy minus its `> ...` header block and blank line."""
    head, sep, body = text.partition("\n\n")
    if not sep or not all(ln.startswith(">") for ln in head.splitlines()):
        raise ValueError("approved doc has no approval header")
    return body


def check_contract(project, pin: bool = False, accept_unmapped: bool = False) -> ContractCheck:
    from ..graph.store import GraphStoreError, load_latest, parse_graph
    from ..review.approval import ingest_source_hash, sha256_bytes, sha256_text
    from ..review.docrender import doc_path

    errors: list[str] = []
    warnings: list[str] = []
    blockers: list[ContractBlocker] = []

    def done(contract: MigrationContract | None = None, latest: int | None = None, csha: str = "") -> ContractCheck:
        chk = ContractCheck(ok=not errors, errors=errors, warnings=warnings, blockers=blockers,
                            graph_version=contract.graph_version if contract else None,
                            latest_graph_version=latest, contract_sha256=csha, pinned=pin)
        _audit(project, chk, accept_unmapped)
        return chk

    # rule 1
    try:
        c = load_contract(project)
    except ContractError as e:
        errors.append(str(e))
        return done()
    csha = sha256_bytes(contract_path(project).read_bytes())
    if c.schema_version < 2:
        warnings.append(f"contract schema_version {c.schema_version} predates source_hash; re-approve to record it")

    # rule 2: hashes against the files the contract names
    snap_graph = None
    snap = _inside(project, c.graph_snapshot)
    if snap is None:
        errors.append(f"contract graph_snapshot {c.graph_snapshot!r} is not a project-relative path")
    elif not snap.exists():
        errors.append(f"approved graph snapshot {c.graph_snapshot} is missing")
    else:
        raw = snap.read_bytes()
        if sha256_bytes(raw) != c.graph_sha256:
            errors.append(f"stale: graph snapshot {c.graph_snapshot} changed since approval (graph_sha256 mismatch)")
        else:
            try:
                snap_graph = parse_graph(raw.decode("utf-8"))
            except (GraphStoreError, ValueError, ValidationError) as e:
                errors.append(f"approved graph snapshot {c.graph_snapshot} unreadable: {type(e).__name__}")
            else:
                if snap_graph.version != c.graph_version:
                    errors.append(f"contract graph_version v{c.graph_version} does not match snapshot "
                                  f"{c.graph_snapshot} (v{snap_graph.version})")
    doc = _inside(project, c.approved_doc)
    if doc is None:
        errors.append(f"contract approved_doc {c.approved_doc!r} is not a project-relative path")
    elif not doc.exists():
        errors.append(f"approved doc {c.approved_doc} is missing")
    else:
        try:
            body = approved_doc_body(doc.read_text())
        except (ValueError, UnicodeDecodeError) as e:
            errors.append(f"approved doc {c.approved_doc} unreadable: {e}")
        else:
            if sha256_text(body) != c.doc_sha256:
                errors.append(f"stale: approved doc {c.approved_doc} changed since approval (doc_sha256 mismatch)")

    # rule 3: latest graph version (and the current doc) vs the contract
    latest_v = None
    try:
        latest = load_latest(Path(project.graph_dir))
        latest_v = latest.version if latest is not None else None
    except GraphStoreError as e:
        errors.append(f"graph/latest.json unreadable: {e}")
    if latest_v is not None and latest_v != c.graph_version:
        msg = f"graph re-analysed after approval: contract is v{c.graph_version}, latest is v{latest_v}"
        if pin:
            warnings.append(msg + " (--pin-contract: migrating from the approved snapshot)")
        else:
            errors.append(msg + ": re-approve, or pass --pin-contract to migrate from the approved snapshot")
    elif latest_v is not None:
        cur = doc_path(project)
        if cur.exists() and sha256_text(cur.read_text()) != c.doc_sha256:
            msg = ("docs/understanding.latest.md changed since approval (for example a quirk decision changed)")
            if pin:
                warnings.append(msg + " (--pin-contract: the contract's decisions apply)")
            else:
                errors.append(msg + ": re-approve, or pass --pin-contract to keep the approved decisions")

    # rule 4: legacy source unchanged
    current_hash = ingest_source_hash(project)
    if not c.source_hash:
        warnings.append("contract has no source_hash: Phase 2 cannot detect legacy source changes since approval "
                        "(re-run `ingest` and re-approve to record it)")
    elif current_hash is None:
        errors.append("ingest_report.json missing or unreadable: cannot confirm the legacy source is the approved one")
    elif current_hash != c.source_hash:
        errors.append("stale: the legacy source changed since approval (ingest_report.json source_hash differs from "
                      "the contract): re-run analyze and re-approve")

    # rule 5: decisions for every quirk / suspected bug
    if snap_graph is not None:
        decided = {d.item_id for d in c.decisions}
        missing = sorted(n.id for n in snap_graph.nodes if (n.quirk or n.suspected_bug) and n.id not in decided)
        if missing:
            errors.append(f"{len(missing)} quirk/suspected-bug node(s) without a decision in the contract: "
                          + ", ".join(missing[:10]))

    # rule 6: coverage
    if not c.coverage.complete or c.coverage.unmapped:
        what = (f"{c.coverage.unmapped} unmapped inventory item(s)" if c.coverage.unmapped
                else "coverage not marked complete")
        if accept_unmapped:
            warnings.append(f"{what} accepted (--accept-unmapped, recorded in the audit log)")
        else:
            errors.append(f"coverage incomplete at approval: {what}; fix coverage and re-approve, or pass "
                          "--accept-unmapped")

    # rule 7: deferred findings -> blockers
    for f in c.deferred_findings:
        blockers.append(ContractBlocker(finding_id=f.id, kind=f.kind, title=f.title, item_ids=list(f.item_ids),
                                        note=f.note))
        warnings.append(f"deferred finding {f.id} ({f.title}) blocks every slice containing "
                        + (", ".join(f.item_ids[:10]) if f.item_ids else "(no items: blocks cut-over approval)"))
    return done(c, latest_v, csha)


def _audit(project, chk: ContractCheck, accept_unmapped: bool) -> None:
    from ..audit import AuditLog
    try:
        AuditLog(Path(project.audit_dir) / "audit.jsonl").record(
            "contract", "contract_check", ok=chk.ok, errors=len(chk.errors), warnings=len(chk.warnings),
            blockers=[b.finding_id for b in chk.blockers], pinned=chk.pinned, accept_unmapped=accept_unmapped,
            graph_version=chk.graph_version, contract_sha256=chk.contract_sha256)
    except OSError:
        pass


def require_contract(project, pin: bool = False, accept_unmapped: bool = False) -> MigrationContract:
    """For Phase 2 commands that write: the contract if the gate passes, else ContractError (check attached)."""
    chk = check_contract(project, pin=pin, accept_unmapped=accept_unmapped)
    if not chk.ok:
        raise ContractError("migration contract check failed:\n- " + "\n- ".join(chk.errors), chk)
    return load_contract(project)
