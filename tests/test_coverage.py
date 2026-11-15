import pytest

from parity_migrate.analyze.coverage import CoverageIncomplete, assert_complete, check_coverage
from parity_migrate.graph.models import Graph, Node
from parity_migrate.pipeline import Inventory, InventoryItem


def inv(*ids):
    return Inventory(items=[InventoryItem(id=i, kind="source_file", name=i, file=i) for i in ids], counts={})


def g(refs):
    return Graph(nodes=[Node(id="COMP-0001", type="component", name="x", inventory_refs=refs)])


def test_counts_and_excluded():
    r = check_coverage(inv("I1", "I2", "I3", "I4"), g(["I1"]), {"I2": "vendored", "I1": "ignored"})
    assert (r.mapped, r.excluded, r.unmapped) == (1, 1, 2)
    assert [i.id for i in r.unmapped_items] == ["I3", "I4"] and not r.complete


def test_gate():
    bad = check_coverage(inv("I1", "I2"), g(["I1"]))
    with pytest.raises(CoverageIncomplete):
        assert_complete(bad)
    ok = check_coverage(inv("I1", "I2"), g(["I1"]), {"I2": "generated"})
    assert ok.complete
    assert_complete(ok)
