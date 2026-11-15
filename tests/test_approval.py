import os
import stat

import pytest
from click.testing import CliRunner

from migration_agent.audit import read_audit
from migration_agent.cli import main
from migration_agent.graph import load_latest, save_snapshot
from migration_agent.pipeline import Inventory, InventoryItem
from migration_agent.project import Project
from migration_agent.review._io import ReviewError
from migration_agent.review.approval import (
    approval_status, approve, graph_sha256, load_approvals, readiness, sha256_text,
)
from migration_agent.review.comments import add_comment, mark_processed
from migration_agent.review.decisions import add_decision
from migration_agent.review.docrender import write_latest
from migration_agent.review.resolutions import add_resolution
from migration_agent.verify.models import MigrationContract, Resolution, ResolutionsFile

from phase1_helpers import make_project, write_findings


@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return make_project(tmp_path, tmp_path_factory)


def _doc(p):
    return (p.docs / "understanding.latest.md").read_text()


def _has(items, text):
    return any(text in x for x in items)


def test_clean_project_is_ready_with_warnings(proj):
    r = readiness(proj)
    assert r.ready and r.blockers == []
    assert _has(r.warnings, "Verifier never run")
    assert _has(r.warnings, "2 quirk/bug item(s) on the default decision")
    assert _has(r.warnings, "low or no confidence")


def test_blocker_no_graph_and_no_doc(tmp_path):
    p = Project.init(tmp_path / "e", source="s")
    r = readiness(p)
    assert not r.ready
    assert _has(r.blockers, "no graph snapshot") and _has(r.blockers, "no docs/understanding.latest.md")


def test_blocker_missing_doc_only(proj):
    (proj.docs / "understanding.latest.md").unlink()
    assert readiness(proj).blockers == ["no docs/understanding.latest.md: run `migration-agent analyze` first"]


def test_blocker_coverage_incomplete(proj):
    inv = Inventory.model_validate_json(proj.inventory_path.read_text())
    inv.items.append(InventoryItem(id="INV-route-zzz", kind="route", name="GET /zzz"))
    proj.inventory_path.write_text(inv.model_dump_json())
    write_latest(proj)  # keep the doc current so only the coverage blocker remains
    r = readiness(proj)
    assert len(r.blockers) == 1 and "coverage incomplete: 1 unmapped" in r.blockers[0] and "INV-route-zzz" in r.blockers[0]


def test_blocker_unresolved_and_open_findings(proj):
    write_findings(proj, proj.quirk_id)
    r = readiness(proj)
    assert r.blockers == [r.blockers[0]] and "2 Verifier finding(s) not resolved: F-0001, F-0002" in r.blockers[0]
    assert not _has(r.warnings, "Verifier never run")
    add_resolution(proj, "F-0001", "dismissed")
    rf = ResolutionsFile.model_validate_json((proj.root / "feedback" / "resolutions.json").read_text())
    rf.resolutions.append(Resolution(finding_id="F-0002", status="open"))
    (proj.root / "feedback" / "resolutions.json").write_text(rf.model_dump_json())
    assert readiness(proj).blockers == [
        "1 Verifier finding(s) not resolved: F-0002 (use `resolve` or the review server)"]
    add_resolution(proj, "F-0002", "accepted_code")
    assert readiness(proj).ready


def test_blocker_pending_reanalysis(proj):
    write_findings(proj, proj.quirk_id)
    add_resolution(proj, "F-0001", "needs_reanalysis")
    add_resolution(proj, "F-0002", "dismissed")
    r = readiness(proj)
    assert len(r.blockers) == 1 and "waiting for re-analysis: F-0001" in r.blockers[0]


def test_blocker_unprocessed_comment(proj):
    c = add_comment(proj, proj.plain_id, "is this right?")
    r = readiness(proj)
    assert len(r.blockers) == 1 and c.id in r.blockers[0] and "comment" in r.blockers[0]
    mark_processed(proj, [c.id])
    assert readiness(proj).ready


def test_blocker_stale_doc(proj):
    (proj.docs / "understanding.latest.md").write_text(_doc(proj) + "\nhand edit\n")
    r = readiness(proj)
    assert len(r.blockers) == 1 and "out of date" in r.blockers[0]
    write_latest(proj)
    assert readiness(proj).ready


def test_deferred_is_warning_not_blocker(proj):
    write_findings(proj, proj.quirk_id)
    add_resolution(proj, "F-0001", "deferred", "handle in migration")
    add_resolution(proj, "F-0002", "dismissed")
    r = readiness(proj)
    assert r.ready and _has(r.warnings, "1 Verifier finding(s) deferred to the migration: F-0001")


def test_approve_refuses_when_blocked(proj):
    add_comment(proj, proj.plain_id, "x")
    with pytest.raises(ReviewError, match="not ready for approval") as e:
        approve(proj, "alice")
    assert "comment" in str(e.value)
    assert not (proj.root / "feedback" / "approvals.json").exists()
    assert not (proj.docs / "migration_contract.json").exists()
    assert not list(proj.docs.glob("*.approved.md"))
    res = CliRunner().invoke(main, ["approve", str(proj.root), "--by", "alice"])
    assert res.exit_code != 0 and "not ready for approval" in res.output and "comment" in res.output

def test_approve_requires_approver(proj):
    with pytest.raises(ReviewError, match="approved_by"):
        approve(proj, "   ")


def test_approve_writes_history_copy_and_contract(proj):
    write_findings(proj, proj.bug_id)
    add_resolution(proj, "F-0001", "deferred", "post-migration")
    add_resolution(proj, "F-0002", "dismissed")
    add_decision(proj, proj.bug_id, "fix", "refund off-by-one")
    write_latest(proj)
    doc = _doc(proj)
    g = load_latest(proj.graph_dir)
    a = approve(proj, "alice", "looks right")

    assert a.graph_version == g.version and a.approved_by == "alice" and a.note == "looks right"
    assert a.graph_sha256 == graph_sha256(proj, g.version) and a.doc_sha256 == sha256_text(doc)
    assert a.deferred_finding_ids == ["F-0001"]
    assert {d.item_id: (d.decision, d.explicit) for d in a.decisions} == {
        proj.bug_id: ("fix", True), proj.quirk_id: ("preserve", False)}
    assert load_approvals(proj).approvals == [a]

    copy = proj.docs / f"understanding.v{g.version}.approved.md"
    assert a.approved_doc == f"docs/{copy.name}"
    text = copy.read_text()
    assert text.endswith(doc) and "Approved by: alice" in text and a.graph_sha256 in text and a.doc_sha256 in text
    assert sha256_text(text[len(text) - len(doc):]) == a.doc_sha256
    assert not (os.stat(copy).st_mode & stat.S_IWUSR)

    c = MigrationContract.model_validate_json((proj.docs / "migration_contract.json").read_text())
    assert c.graph_version == g.version and c.graph_sha256 == a.graph_sha256 and c.doc_sha256 == a.doc_sha256
    assert c.project_name == proj.meta().name and c.project_source == proj.meta().source
    assert c.approved_doc == a.approved_doc and c.graph_snapshot == f"graph/snapshots/v{g.version:03d}.json"
    assert (proj.root / c.graph_snapshot).exists() and (proj.root / c.approved_doc).exists()
    assert [(d.id, d.kind, d.title, d.item_ids, d.note) for d in c.deferred_findings] == [
        ("F-0001", "contradiction", "refund window", [proj.bug_id], "post-migration")]
    assert {d.item_id for d in c.decisions} == {proj.bug_id, proj.quirk_id}
    assert c.coverage.complete and c.coverage.unmapped == 0 and c.coverage.mapped > 0
    assert c.open_questions == [] and c.assumptions == []
    assert _has(c.warnings, "deferred")

    st = approval_status(proj)
    assert st["state"] == "approved" and st["approval"]["approved_by"] == "alice"
    assert any(e["action"] == "approve" and e["graph_version"] == g.version for e in read_audit(proj.audit_dir / "audit.jsonl"))
    with pytest.raises(ReviewError, match="already approved"):
        approve(proj, "bob")


def test_approval_stale_after_new_snapshot(proj):
    approve(proj, "alice")
    assert approval_status(proj)["state"] == "approved"
    g = load_latest(proj.graph_dir)
    save_snapshot(proj.graph_dir, g)
    st = approval_status(proj)
    assert st["state"] == "stale" and f"approved v{g.version}, latest is v{g.version + 1}" in st["reason"]


def test_approval_stale_after_decision_change_and_reapprove(proj):
    a1 = approve(proj, "alice")
    CliRunner().invoke(main, ["decide", str(proj.root), proj.quirk_id, "--fix"])
    st = approval_status(proj)
    assert st["state"] == "stale" and "doc changed" in st["reason"]
    a2 = approve(proj, "bob")
    assert a2.graph_version == a1.graph_version and a2.approved_doc != a1.approved_doc
    assert a2.approved_doc.endswith(f"v{a1.graph_version}.r2.approved.md")
    assert (proj.root / a1.approved_doc).exists()
    assert [x.approved_by for x in load_approvals(proj).approvals] == ["alice", "bob"]
    assert approval_status(proj)["state"] == "approved"


def test_approval_status_none(proj):
    assert approval_status(proj) == {"state": "none", "approval": None, "reason": "not approved yet"}


def test_cli_status_and_approve(proj):
    r = CliRunner()
    res = r.invoke(main, ["status", str(proj.root)])
    assert res.exit_code == 0 and "ready for approval: yes" in res.output and "approval: none" in res.output
    assert "warning: Verifier never run" in res.output
    assert r.invoke(main, ["approve", str(proj.root)]).exit_code != 0  # --by is required
    res = r.invoke(main, ["approve", str(proj.root), "--by", "alice", "--note", "ok"])
    assert res.exit_code == 0, res.output
    assert "migration_contract.json" in res.output
    res = r.invoke(main, ["status", str(proj.root)])
    assert res.exit_code == 0 and "approval: approved" in res.output
    add_comment(proj, proj.plain_id, "x")
    res = r.invoke(main, ["status", str(proj.root)])
    assert res.exit_code == 0 and "ready for approval: no" in res.output and "blocker:" in res.output
    empty = Project.init(proj.root.parent / "empty", source="s")
    res = r.invoke(main, ["status", str(empty.root)])
    assert res.exit_code == 0 and "no graph snapshot" in res.output
