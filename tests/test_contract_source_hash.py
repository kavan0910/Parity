"""Pre-M0 fix 2: the migration contract records ingest_report.json source_hash."""
from migration_agent.ingest import IngestReport
from migration_agent.review.approval import approve, readiness
from migration_agent.verify.models import MigrationContract

from phase1_helpers import make_project


def _contract(p):
    return MigrationContract.model_validate_json((p.docs / "migration_contract.json").read_text())


def test_contract_has_source_hash(tmp_path, tmp_path_factory):
    p = make_project(tmp_path, tmp_path_factory)
    rep = IngestReport.model_validate_json((p.root / "ingest_report.json").read_text())
    assert rep.source_hash
    assert not any("source_hash" in w for w in readiness(p).warnings)
    approve(p, "alice")
    c = _contract(p)
    assert c.schema_version == 2 and c.source_hash == rep.source_hash


def test_missing_ingest_report_gives_empty_hash_and_warning(tmp_path, tmp_path_factory):
    p = make_project(tmp_path, tmp_path_factory)
    (p.root / "ingest_report.json").unlink()
    r = readiness(p)
    assert r.ready and any("empty source_hash" in w for w in r.warnings)
    approve(p, "alice")
    c = _contract(p)
    assert c.source_hash == "" and any("empty source_hash" in w for w in c.warnings)


def test_v1_contract_without_source_hash_still_parses():
    c = MigrationContract.model_validate({
        "schema_version": 1, "project_name": "x", "project_source": "s", "graph_version": 1, "graph_sha256": "a",
        "doc_sha256": "b", "approved_by": "c", "approved_at": "d", "approved_doc": "docs/x", "graph_snapshot": "g"})
    assert c.source_hash == ""
