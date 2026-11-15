"""M0 entry gate (migrate/contract.py): docs/phase2-plan.md section 1, rules 1-7."""
import json

import pytest

from parity_migrate.audit import read_audit
from parity_migrate.graph import load_latest, save_snapshot
from parity_migrate.migrate.contract import (
    ContractError, approved_doc_body, check_contract, contract_json_schema, load_contract, require_contract,
)
from parity_migrate.review.approval import approve
from parity_migrate.review.decisions import add_decision
from parity_migrate.review.docrender import write_latest
from parity_migrate.review.resolutions import add_resolution
from parity_migrate.verify.models import MigrationContract

from phase1_helpers import make_project, write_findings
from phase2_helpers import approved_project, contract_file, edit_contract, make_writable


@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return approved_project(tmp_path, tmp_path_factory)


def _has(items, text):
    return any(text in x for x in items)


def test_fresh_approval_passes(proj):
    chk = check_contract(proj)
    assert chk.ok and chk.errors == [] and chk.blockers == []
    assert chk.graph_version == chk.latest_graph_version == load_latest(proj.graph_dir).version
    assert len(chk.contract_sha256) == 64
    c = require_contract(proj)
    assert isinstance(c, MigrationContract) and c.source_hash
    assert any(e["action"] == "contract_check" and e["ok"] for e in read_audit(proj.audit_dir / "audit.jsonl"))


def test_schema_export_is_the_phase1_model(tmp_path):
    out = tmp_path / "s.json"
    schema = contract_json_schema(out)
    assert schema == MigrationContract.model_json_schema() == json.loads(out.read_text())
    assert "source_hash" in schema["properties"]


# rule 1
def test_missing_contract(tmp_path, tmp_path_factory):
    p = make_project(tmp_path, tmp_path_factory)
    chk = check_contract(p)
    assert not chk.ok and _has(chk.errors, "no migration_contract.json")
    with pytest.raises(ContractError, match="approve"):
        require_contract(p)


@pytest.mark.parametrize("text,msg", [("{oops", "not valid JSON"), ("[]", "JSON object"),
                                      ('{"schema_version": 99}', "not supported"),
                                      ('{"schema_version": 2}', "does not match the contract schema")])
def test_unreadable_contract(proj, text, msg):
    contract_file(proj).write_text(text)
    with pytest.raises(ContractError, match=msg):
        load_contract(proj)
    assert _has(check_contract(proj).errors, msg)


def test_v1_contract_is_supported_with_warning(proj):
    edit_contract(proj, schema_version=1)
    chk = check_contract(proj)
    assert chk.ok and _has(chk.warnings, "predates source_hash")


# rule 2
def test_stale_graph_snapshot_bytes(proj):
    c = load_contract(proj)
    snap = proj.root / c.graph_snapshot
    snap.write_text(snap.read_text() + " ")
    chk = check_contract(proj)
    assert not chk.ok and _has(chk.errors, "graph_sha256 mismatch")


def test_stale_approved_doc(proj):
    c = load_contract(proj)
    doc = proj.root / c.approved_doc
    make_writable(doc)
    doc.write_text(doc.read_text() + "\nedited\n")
    assert _has(check_contract(proj).errors, "doc_sha256 mismatch")
    doc.write_text("no header at all")
    assert _has(check_contract(proj).errors, "no approval header")
    doc.unlink()
    assert _has(check_contract(proj).errors, "is missing")


def test_contract_paths_must_stay_inside_project(proj):
    edit_contract(proj, graph_snapshot="../../etc/passwd", approved_doc="/etc/hosts")
    errs = check_contract(proj).errors
    assert _has(errs, "graph_snapshot") and _has(errs, "approved_doc") and _has(errs, "project-relative")


def test_approved_doc_body_strips_header():
    assert approved_doc_body("> a\n> b\n\n# Doc\n\ntext") == "# Doc\n\ntext"


# rule 3
def test_newer_graph_version_needs_pin(proj):
    g = load_latest(proj.graph_dir)
    save_snapshot(proj.graph_dir, g)
    chk = check_contract(proj)
    assert not chk.ok and _has(chk.errors, "re-analysed after approval") and _has(chk.errors, "--pin-contract")
    pinned = check_contract(proj, pin=True)
    assert pinned.ok and pinned.pinned and _has(pinned.warnings, "migrating from the approved snapshot")
    assert pinned.latest_graph_version == g.version + 1


def test_changed_decision_after_approval(proj):
    add_decision(proj, proj.quirk_id, "fix", "now fix it")
    write_latest(proj)
    chk = check_contract(proj)
    assert not chk.ok and _has(chk.errors, "changed since approval")
    assert check_contract(proj, pin=True).ok


# rule 4
def test_source_changed_since_approval(proj):
    rep = proj.root / "ingest_report.json"
    d = json.loads(rep.read_text())
    d["source_hash"] = "0" * 64
    rep.write_text(json.dumps(d))
    assert _has(check_contract(proj).errors, "legacy source changed since approval")
    rep.unlink()
    assert _has(check_contract(proj).errors, "ingest_report.json missing")


def test_contract_without_source_hash_warns(proj):
    edit_contract(proj, source_hash="")
    chk = check_contract(proj)
    assert chk.ok and _has(chk.warnings, "no source_hash")


# rule 5
def test_missing_decision(proj):
    c = load_contract(proj)
    edit_contract(proj, decisions=[d.model_dump() for d in c.decisions if d.item_id != proj.quirk_id])
    chk = check_contract(proj)
    assert not chk.ok and _has(chk.errors, "without a decision") and _has(chk.errors, proj.quirk_id)


# rule 6
def test_unmapped_coverage_needs_accept_flag(proj):
    edit_contract(proj, coverage={"mapped": 5, "unmapped": 2, "excluded": 0, "complete": False})
    chk = check_contract(proj)
    assert not chk.ok and _has(chk.errors, "2 unmapped inventory item(s)") and _has(chk.errors, "--accept-unmapped")
    acc = check_contract(proj, accept_unmapped=True)
    assert acc.ok and _has(acc.warnings, "accepted (--accept-unmapped")
    last = [e for e in read_audit(proj.audit_dir / "audit.jsonl") if e["action"] == "contract_check"][-1]
    assert last["accept_unmapped"] is True


# rule 7
def test_deferred_findings_become_blockers(tmp_path, tmp_path_factory):
    p = make_project(tmp_path, tmp_path_factory)
    write_findings(p, p.bug_id)
    add_resolution(p, "F-0001", "deferred", "post-migration")
    add_resolution(p, "F-0002", "dismissed")
    approve(p, "alice")
    chk = check_contract(p)
    assert chk.ok
    assert [(b.finding_id, b.item_ids, b.note) for b in chk.blockers] == [("F-0001", [p.bug_id], "post-migration")]
    assert _has(chk.warnings, "deferred finding F-0001") and _has(chk.warnings, p.bug_id)
