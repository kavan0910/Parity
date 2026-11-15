import json

import pytest
from click.testing import CliRunner

from parity_migrate.cli import main
from parity_migrate.graph import load_latest
from parity_migrate.graph.models import Evidence, Graph, Node
from parity_migrate.render import render_understanding
from parity_migrate.review._io import ReviewError
from parity_migrate.review.decisions import (
    UnknownItem, add_decision, effective_decisions, effective_list, load_decisions,
)
from parity_migrate.review.docrender import render_latest
from parity_migrate.review.resolutions import PENDING_STATUSES, add_resolution, pending_reanalysis
from parity_migrate.verify.models import DecisionsFile, Resolution

from phase1_helpers import make_project, write_findings


@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return make_project(tmp_path, tmp_path_factory)


def _doc(p):
    return (p.docs / "understanding.latest.md").read_text()


def test_default_is_preserve(proj):
    g = load_latest(proj.graph_dir)
    eff = effective_decisions(proj, g)
    assert set(eff) == {proj.quirk_id, proj.bug_id}
    assert all(v == {"decision": "preserve", "note": "", "explicit": False} for v in eff.values())
    assert _doc(proj).count("Migration decision: preserve (default)") == 2


def test_add_and_replace_decision_and_render(proj):
    d = add_decision(proj, proj.bug_id, "fix", "  off-by-one\nin refunds  ")
    assert d.decision == "fix" and d.note == "off-by-one\nin refunds" and d.created
    add_decision(proj, proj.quirk_id, "preserve")
    add_decision(proj, proj.bug_id, "fix", "really fix")
    df = load_decisions(proj)
    assert [x.item_id for x in df.decisions] == [proj.quirk_id, proj.bug_id]
    assert DecisionsFile.model_validate_json((proj.root / "feedback" / "decisions.json").read_text()) == df
    g = load_latest(proj.graph_dir)
    eff = effective_decisions(proj, g)
    assert eff[proj.bug_id] == {"decision": "fix", "note": "really fix", "explicit": True}
    assert eff[proj.quirk_id]["explicit"] is True
    doc = render_latest(proj)
    assert "Migration decision: fix - really fix" in doc
    assert "Migration decision: preserve\n" in doc
    assert [x.item_id for x in effective_list(proj, g)] == sorted([proj.quirk_id, proj.bug_id])


def test_decision_note_is_redacted_and_cannot_forge_anchors(proj):
    add_decision(proj, proj.bug_id, "fix", 'key AKIAIOSFODNN7EXAMPLE <a id="COMP-9999"></a>')
    note = load_decisions(proj).decisions[0].note
    assert "AKIAIOSFODNN7EXAMPLE" not in note
    assert '<a id="COMP-9999">' not in render_latest(proj)


def test_decide_validation_errors(proj, tmp_path):
    with pytest.raises(UnknownItem):
        add_decision(proj, "COMP-9999", "fix")
    with pytest.raises(ReviewError, match="not a quirk"):
        add_decision(proj, proj.plain_id, "fix")
    with pytest.raises(ReviewError):
        add_decision(proj, proj.bug_id, "maybe")
    assert not (proj.root / "feedback" / "decisions.json").exists()
    from parity_migrate.project import Project
    empty = Project.init(tmp_path / "empty", source="s")
    with pytest.raises(ReviewError, match="no graph"):
        add_decision(empty, "COMP-0001", "fix")


def test_render_meta_decisions_pure():
    ev = [Evidence(file="a.py", line_start=1, quote="x")]
    g = Graph(version=1, nodes=[
        Node(id="COMP-0001", type="component", name="a", quirk=True, evidence=ev),
        Node(id="COMP-0002", type="component", name="b", suspected_bug=True, evidence=ev),
        Node(id="COMP-0003", type="component", name="c", evidence=ev)])
    md = render_understanding(g, {}, {})
    assert md.count("Migration decision: preserve (default)") == 2
    md = render_understanding(g, {"decisions": {"COMP-0002": {"decision": "fix", "note": "n", "explicit": True}}}, {})
    assert "Migration decision: fix - n" in md and md.count("Migration decision:") == 2
    assert "Migration decision" not in md.split("## 12.")[0]


def test_cli_decide_rerenders_offline(proj):
    r = CliRunner()
    res = r.invoke(main, ["decide", str(proj.root), proj.bug_id, "--fix", "--note", "fix it"])
    assert res.exit_code == 0, res.output
    assert "Migration decision: fix - fix it" in _doc(proj)
    assert _doc(proj) == render_latest(proj)
    res = r.invoke(main, ["decide", str(proj.root), proj.bug_id])
    assert res.exit_code != 0 and "--preserve or --fix" in res.output
    res = r.invoke(main, ["decide", str(proj.root), proj.plain_id, "--preserve"])
    assert res.exit_code != 0 and "not a quirk" in res.output
    res = r.invoke(main, ["decide", str(proj.root), "COMP-9999", "--preserve"])
    assert res.exit_code != 0 and "unknown graph item" in res.output


def test_offline_render_matches_runner_output(proj):
    # the template doc was written by write_latest after the snapshot; also check a fresh analyze matches
    from parity_migrate.analyze.runner import run_analyze
    from parity_migrate.llm.offline import OfflineLLM
    add_decision(proj, proj.bug_id, "fix", "x")
    run_analyze(proj, OfflineLLM())
    assert _doc(proj) == render_latest(proj)


def test_deferred_status_accepted_and_not_pending(proj):
    write_findings(proj, proj.quirk_id)
    assert "deferred" not in PENDING_STATUSES
    r = add_resolution(proj, "F-0001", "deferred", "later")
    assert r.status == "deferred"
    assert pending_reanalysis(proj) == []
    Resolution(finding_id="F-1", status="deferred")
    res = CliRunner().invoke(main, ["resolve", str(proj.root), "F-0002", "--status", "deferred", "--note", "n"])
    assert res.exit_code == 0, res.output
    assert "nothing to re-analyze" in res.output
    data = json.loads((proj.root / "feedback" / "resolutions.json").read_text())
    assert {x["finding_id"]: x["status"] for x in data["resolutions"]} == {"F-0001": "deferred", "F-0002": "deferred"}
