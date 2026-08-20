"""The wait-for graph and the detectors -- with zero concurrency.

That is the whole point of the SRP split: `WaitGraph` is data, a detector is a
pure function over it, and neither needs a thread to exercise. Every cycle
shape lives here; `test_deadlock.py` then only has to prove that the real
scheduler registers the right edges, not that the algorithm is right.
"""

from __future__ import annotations

import ast
import pathlib

from lumon.deadlock import AndOrDetector, SccDetector
from lumon.waitgraph import WaitGraph


def graph(**edges: str) -> WaitGraph:
    """`graph(A="B")` -- one AND-edge per keyword, single-target for brevity."""
    built = WaitGraph()
    for waiter, target in edges.items():
        built.wait_on(waiter, {target})
    return built


# ------------------------------------------------------------ AND-wait cycles


def test_no_edges_means_no_cycle() -> None:
    assert SccDetector().find_cycle(WaitGraph()) is None


def test_a_chain_is_not_a_cycle() -> None:
    assert SccDetector().find_cycle(graph(A="B", B="C")) is None


def test_two_node_cycle() -> None:
    assert SccDetector().find_cycle(graph(A="B", B="A")) == ["A", "B"]


def test_three_node_cycle() -> None:
    assert SccDetector().find_cycle(graph(A="B", B="C", C="A")) == ["A", "B", "C"]


def test_self_reference_is_a_one_node_cycle() -> None:
    """`ADD A` inside A is a cycle of length 1, not a parse error."""
    assert SccDetector().find_cycle(graph(A="A")) == ["A"]


def test_overlapping_cycles_form_one_scc() -> None:
    """A<->B and B<->C share B, so the component is all three."""
    built = graph(A="B", C="B")
    built.wait_on("B", {"A", "C"})
    assert SccDetector().find_cycle(built) == ["A", "B", "C"]


def test_a_node_waiting_on_a_cycle_is_not_a_member() -> None:
    """X is stuck, but it is not *circular*: the cycle publishes -1 and X
    wakes up and computes with it. Deadlock resolves outward."""
    assert SccDetector().find_cycle(graph(A="B", B="A", X="A")) == ["A", "B"]


def test_result_is_independent_of_insertion_order() -> None:
    """The contract on `find_cycle`, and the reason the answer is an SCC
    rather than the path a search happened to walk."""
    forward = SccDetector().find_cycle(graph(A="B", B="C", C="A"))
    backward = WaitGraph()
    for waiter, target in (("C", "A"), ("B", "C"), ("A", "B")):
        backward.wait_on(waiter, {target})
    assert SccDetector().find_cycle(backward) == forward
    assert forward == ["A", "B", "C"]


def test_clear_removes_a_waiter() -> None:
    built = graph(A="B", B="A")
    built.clear("B")
    assert SccDetector().find_cycle(built) is None


# ------------------------------------------------------------- OR-wait cycles


def test_andor_reduces_to_scc_when_there_are_no_or_groups() -> None:
    """The check that the generalization is one: with no OR-group in sight the
    two detectors must agree edge for edge, which is why every AND-only test
    above still stands after `AndOrDetector` became the default."""
    for edges in (
        {},
        {"A": "B"},
        {"A": "B", "B": "A"},
        {"A": "B", "B": "C", "C": "A"},
        {"A": "A"},
    ):
        assert AndOrDetector().find_cycle(graph(**edges)) == SccDetector().find_cycle(
            graph(**edges)
        )


def test_an_or_group_with_a_live_branch_is_not_stuck() -> None:
    """HELLY waits on ANY OF {MARK, DYLAN}; MARK waits on HELLY; DYLAN is not
    blocked at all. HELLY has a way forward, so nothing is stuck -- and an
    AND-only reading would wrongly report HELLY and MARK as a cycle."""
    built = WaitGraph()
    built.push_or_group("HELLY", {"MARK", "DYLAN"})
    built.wait_on("MARK", {"HELLY"})
    assert AndOrDetector().find_cycle(built) is None


def test_an_or_group_whose_only_branch_cycles_is_a_deadlock() -> None:
    built = WaitGraph()
    built.push_or_group("HELLY", {"MARK"})
    built.wait_on("MARK", {"HELLY"})
    assert AndOrDetector().find_cycle(built) == ["HELLY", "MARK"]


def test_an_or_group_whose_every_branch_cycles_is_a_deadlock() -> None:
    """Both branches lead back to HELLY, so the group is no escape route and
    all three are one component."""
    built = WaitGraph()
    built.push_or_group("HELLY", {"MARK", "DYLAN"})
    built.wait_on("MARK", {"HELLY"})
    built.wait_on("DYLAN", {"HELLY"})
    assert AndOrDetector().find_cycle(built) == ["DYLAN", "HELLY", "MARK"]


def test_and_edge_to_a_cycle_does_not_make_the_waiter_a_member() -> None:
    built = graph(A="B", B="A")
    built.wait_on("X", {"A"})
    assert AndOrDetector().find_cycle(built) == ["A", "B"]


def test_or_group_pops_back_off() -> None:
    """The group lives exactly as long as the wait does: once HELLY stops
    waiting, the edge it implied is gone."""
    built = WaitGraph()
    built.push_or_group("HELLY", {"MARK"})
    built.wait_on("MARK", {"HELLY"})
    built.pop_or_group("HELLY")
    assert AndOrDetector().find_cycle(built) is None


# ------------------------------------------------------------ the SRP split


def test_the_graph_and_the_detectors_never_import_locks_or_cells() -> None:
    """The structural claim this whole module rests on: if `waitgraph` or a
    detector reached for `threading` or `lumon.cell`, none of the tests above
    could stay thread-free. Checked over the parsed AST, so a docstring that
    merely mentions `threading` -- as several do -- cannot fail it."""
    root = pathlib.Path(__file__).parent.parent / "lumon"
    modules = [root / "waitgraph.py", *sorted((root / "deadlock").glob("*.py"))]
    assert len(modules) >= 3, "no detector modules found to check"

    forbidden = {"threading", "lumon.cell", "lumon.runners", "lumon.resolvers"}
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                for banned in forbidden:
                    assert name != banned and not name.startswith(f"{banned}."), (
                        f"{module.name} imports {name}"
                    )
