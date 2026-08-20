"""The single-threaded reference oracle.

Every assertion here is structural -- a published value -- never a timing
measurement. The runner has one thread, so "did it short-circuit?" can only be
answered by what the registry ends up holding -- which is exactly the property
that matters: short-circuiting may change how long you wait, never what you
compute.

The interesting tests are the branch-deferral ones. A branch that would
re-enter an Innie already under evaluation is *deferred*, not resolved, so that
a healthy branch gets its chance first. Without that, this oracle publishes -1
where the concurrent run completes normally, and a serial-vs-concurrent
comparison fails pointing at the oracle rather than at a real bug.
"""

from __future__ import annotations

import pathlib

import pytest

from lumon.cell import PendingAtSnapshot
from lumon.loader import Innie, load, load_path
from lumon.runners import RUNNERS
from lumon.runners.serial import SerialRunner, run_serial

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"

EXPECTED: dict[str, dict[str, int]] = {
    "1.json": {"HELLY": 20, "MARK": 5, "IRVING": 25, "BURT": 0},
    "2.json": {"DYLAN": 100, "BURT": 200},
    "3.json": {"HELLY": 8, "MARK": 16, "IRVING": 124, "BURT": 497, "DYLAN": 0},
}


def values(innies: list[Innie]) -> dict[str, int | None]:
    return {result.innie_id: result.value for result in run_serial(innies).snapshot()}


def schedule(*pairs: tuple[str, str]) -> list[Innie]:
    return load({"innies": [{"id": i, "schedule": s} for i, s in pairs]})


# ------------------------------------------------------------- acceptance


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_serial_matches_golden_values(name: str) -> None:
    assert values(load_path(SAMPLES / name)) == EXPECTED[name]


def test_serial_is_registered_under_its_mode_name() -> None:
    assert RUNNERS["serial"] is SerialRunner


# ------------------------------------------------------------------ cycles


def test_serial_detects_a_two_cycle() -> None:
    assert values(
        schedule(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1}


def test_serial_detects_a_self_reference() -> None:
    # A self-reference is a cycle of length 1, not a parse error.
    assert values(schedule(("A", "LOAD 5\nADD A\nWAFFLE"))) == {"A": -1}


def test_deadlock_resolves_outward_only_cycle_members_publish_minus_one() -> None:
    # C merely *waits on* the A/B cycle. It consumes -1 as ordinary data.
    assert values(
        schedule(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("C", "LOAD 10\nADD A\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1, "C": 9}


# -------------------------------------------------------- branch deferral


def test_serial_defers_a_cyclic_branch_and_escapes_via_a_healthy_one() -> None:
    # MARK cycles back to HELLY; DYLAN does not. Without deferral this
    # returns {"HELLY": -1, "MARK": -1} while the concurrent run returns
    # {"HELLY": 0, "MARK": 0} -- one schedule, two answers.
    assert values(
        schedule(
            ("DYLAN", "LOAD 1\nWAFFLE"),
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]\nWAFFLE"),
            ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
        )
    ) == {"DYLAN": 1, "HELLY": 0, "MARK": 0}


def test_deferral_is_transitive_not_a_direct_on_stack_check() -> None:
    """The two-hop version of the test above: MARK -> IRVING -> HELLY.

    MARK itself is never on the stack when HELLY folds, so an implementation
    that only asks "is this id on the stack?" resolves MARK, walks into the
    cycle, and publishes -1 for all three. Deferral has to consider the whole
    branch, not its first hop.
    """
    assert values(
        schedule(
            ("DYLAN", "LOAD 1\nWAFFLE"),
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]\nWAFFLE"),
            ("MARK", "LOAD 0\nADD IRVING\nWAFFLE"),
            ("IRVING", "LOAD 0\nADD HELLY\nWAFFLE"),
        )
    ) == {"DYLAN": 1, "HELLY": 0, "MARK": 0, "IRVING": 0}


def test_serial_still_reports_a_cycle_when_no_branch_escapes() -> None:
    assert values(
        schedule(
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK]\nWAFFLE"),
            ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
        )
    ) == {"HELLY": -1, "MARK": -1}


def test_a_cycle_inside_a_probed_branch_settles_without_swallowing_the_asker() -> None:
    """P <-> Q is a cycle in its own right, reached only *through* a branch
    HELLY is trying. It must settle to -1 where it is found -- HELLY and MARK
    are not on it, so HELLY still publishes a real value: deadlock resolves
    outward."""
    assert values(
        schedule(
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK]\nWAFFLE"),
            ("MARK", "LOAD 0\nADD P\nWAFFLE"),
            ("P", "LOAD 0\nADD Q\nWAFFLE"),
            ("Q", "LOAD 0\nADD P\nWAFFLE"),
        )
    ) == {"HELLY": 0, "MARK": -1, "P": -1, "Q": -1}


def test_an_inner_fold_escapes_while_an_outer_fold_is_probing() -> None:
    """ME probes X, X needs P, and P's own fold has both a branch back to ME
    and a healthy one. The inner fold must defer ME and take R, which lets the
    whole chain complete -- exactly what the concurrent run does."""
    assert values(
        schedule(
            ("ME", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [X, D]\nWAFFLE"),
            ("X", "LOAD 0\nADD P\nWAFFLE"),
            ("P", "LOAD 50\nWELLNESS_CHECK 5 > ANY OF [ME, R]\nWAFFLE"),
            ("R", "LOAD 1\nWAFFLE"),
            ("D", "LOAD 99\nWAFFLE"),
        )
    ) == {"ME": 0, "X": 0, "P": 0, "R": 1, "D": 99}


def test_nested_folds_with_no_escape_anywhere_settle_the_whole_cycle() -> None:
    """The mirror of the test above: neither fold has a satisfying
    alternative, so ME -> X -> P -> ME is a genuine cycle and all three
    publish -1. R and D are consulted, fail the comparison, and are untouched
    by the deadlock."""
    assert values(
        schedule(
            ("ME", "LOAD 100\nWELLNESS_CHECK 200 > ANY OF [X, D]\nWAFFLE"),
            ("X", "LOAD 0\nADD P\nWAFFLE"),
            ("P", "LOAD 50\nWELLNESS_CHECK 200 > ANY OF [ME, R]\nWAFFLE"),
            ("R", "LOAD 1000\nWAFFLE"),
            ("D", "LOAD 1000\nWAFFLE"),
        )
    ) == {"ME": -1, "X": -1, "P": -1, "R": 1000, "D": 1000}


# ------------------------------------------------ the settlement contract


def test_a_workday_without_a_waffle_publishes_void() -> None:
    result = run_serial(schedule(("A", "LOAD 5"))).snapshot()[0]
    assert result.is_void


def test_a_fault_settles_into_the_cell_and_propagates_to_dependents() -> None:
    results = {
        result.innie_id: result
        for result in run_serial(
            schedule(
                ("A", "LOAD 5\nMODULO 0\nWAFFLE"),
                ("B", "LOAD 0\nADD A\nWAFFLE"),
            )
        ).snapshot()
    }
    assert "MODULO by zero" in str(results["A"].error)
    assert results["B"].is_fault
    assert "dependency A faulted" in str(results["B"].error)


def test_every_cell_is_settled_on_return() -> None:
    """The Runner contract: no Innie is ever left pending, whatever happened.
    A cycle, a fault and a VOID in one schedule."""
    registry = run_serial(
        schedule(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("C", "LOAD 5\nMODULO 0\nWAFFLE"),
            ("D", "LOAD 5"),
            ("E", "LOAD 1\nWAFFLE"),
        )
    )
    assert not any(
        isinstance(result.error, PendingAtSnapshot) for result in registry.snapshot()
    )


# ------------------------------------------------------------- determinism


def test_serial_result_is_independent_of_innie_declaration_order() -> None:
    forward = schedule(
        ("A", "LOAD 1\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
    )
    backward = schedule(
        ("B", "LOAD 0\nADD A\nWAFFLE"),
        ("A", "LOAD 1\nWAFFLE"),
    )
    assert values(forward) == values(backward) == {"A": 1, "B": 1}


def test_a_deferred_branch_gives_the_same_answer_from_either_end() -> None:
    """The deferral schedule declared in reverse. Deferral must not depend on
    the order run() happens to walk the Innies in."""
    assert values(
        schedule(
            ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]\nWAFFLE"),
            ("DYLAN", "LOAD 1\nWAFFLE"),
        )
    ) == {"MARK": 0, "HELLY": 0, "DYLAN": 1}
