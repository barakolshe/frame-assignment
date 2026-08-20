"""Deadlock resolution through the real scheduler.

The algorithm itself is proved in `test_waitgraph.py`, against hand-built
graphs and without a single thread. What this module proves is the other half:
that the wait edges actually get registered as Innies block, that detection
fires from inside the Registry's critical section, and that every cycle member
ends up holding -1 rather than hanging (S9).

Deadlock is a *value*, never an error: an Innie that merely depends on a cycle
reads -1 as ordinary data and finishes normally.
"""

from __future__ import annotations

from lumon.cell import Result
from lumon.loader import load
from lumon.runners.concurrent import run_concurrent

Schedule = dict[str, list[dict[str, str]]]


def sched(*pairs: tuple[str, str]) -> Schedule:
    return {"innies": [{"id": innie_id, "schedule": text} for innie_id, text in pairs]}


def results(schedule: Schedule) -> dict[str, Result]:
    registry = run_concurrent(load(schedule))
    return {result.innie_id: result for result in registry.snapshot()}


def values(schedule: Schedule) -> dict[str, int | None]:
    return {innie_id: result.value for innie_id, result in results(schedule).items()}


# ------------------------------------------------------------ cycle topology


def test_two_node_cycle_both_get_minus_one() -> None:
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1}


def test_three_node_cycle_all_get_minus_one() -> None:
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD C\nWAFFLE"),
            ("C", "LOAD 0\nADD A\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1, "C": -1}


def test_self_reference_is_a_one_node_cycle() -> None:
    """S9 -- a legal input with a defined answer, NOT a parse error."""
    assert values(sched(("A", "LOAD 5\nADD A\nWAFFLE"))) == {"A": -1}


def test_a_dependent_of_a_cycle_receives_minus_one_and_computes_with_it() -> None:
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("C", "LOAD 100\nADD A\nWAFFLE"),  # 100 + (-1)
        )
    ) == {"A": -1, "B": -1, "C": 99}


def test_two_disjoint_cycles_resolve_independently() -> None:
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("C", "LOAD 0\nADD D\nWAFFLE"),
            ("D", "LOAD 0\nADD C\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1, "C": -1, "D": -1}


def test_overlapping_cycles_form_one_scc() -> None:
    """A<->B and B<->C share B; all three are one SCC."""
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD [A, C]\nWAFFLE"),
            ("C", "LOAD 0\nADD B\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1, "C": -1}


def test_partial_dependency_set_only_the_cycle_member_is_cancelled() -> None:
    """X waits on {A, HEALTHY}. Only A is in a cycle, so X is not a member: it
    waits for both, gets A = -1 and HEALTHY's real value, and computes."""
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("HEALTHY", "LOAD 7\nWAFFLE"),
            ("X", "LOAD 0\nADD [A, HEALTHY]\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1, "HEALTHY": 7, "X": 6}


def test_cycle_plus_healthy_subgraph_leaves_the_healthy_part_alone() -> None:
    assert values(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("P", "LOAD 3\nWAFFLE"),
            ("Q", "LOAD 0\nADD P\nMULTIPLY 2\nWAFFLE"),
        )
    ) == {"A": -1, "B": -1, "P": 3, "Q": 6}


def test_a_deadlocked_run_produces_no_faults() -> None:
    """Deadlock is a value, not an error (S9/S10). It doubles as the guard on
    cancellation: a cancelled Innie that woke up and committed anyway would
    trip `DoubleSettle`, which would surface here as a faulted cell."""
    settled = results(
        sched(
            ("A", "LOAD 0\nADD B\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
            ("C", "LOAD 1\nADD A\nWAFFLE"),
        )
    )
    assert not any(result.is_fault for result in settled.values())
    assert not any(result.is_void for result in settled.values())


# --------------------------------------- the runtime graph, not the static one


def test_conditional_edge_that_never_fires_forms_no_cycle() -> None:
    """THE test that proves detection runs on the runtime graph. A statically
    references B and B references A, but A's edge is guarded by a condition
    that is false (S6), so no cycle forms and both complete normally."""
    assert values(
        sched(
            ("GATE", "LOAD 0\nWAFFLE"),
            ("A", "LOAD 10\nCONDITIONAL_ADD [B] IF GATE > 100\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
        )
    ) == {"GATE": 0, "A": 10, "B": 10}


def test_conditional_edge_that_does_fire_forms_a_cycle() -> None:
    assert values(
        sched(
            ("GATE", "LOAD 1000\nWAFFLE"),
            ("A", "LOAD 10\nCONDITIONAL_ADD [B] IF GATE > 100\nWAFFLE"),
            ("B", "LOAD 0\nADD A\nWAFFLE"),
        )
    ) == {"GATE": 1000, "A": -1, "B": -1}


# --------------------------------------------------------------- determinism


def test_deadlocked_schedule_is_identical_across_500_runs() -> None:
    schedule = sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD [A, C]\nWAFFLE"),
        ("C", "LOAD 0\nADD B\nWAFFLE"),
        ("D", "LOAD 50\nADD A\nWAFFLE"),
        ("E", "LOAD 1\nWAFFLE"),
    )
    first = values(schedule)
    for _ in range(500):
        assert values(schedule) == first
    assert first == {"A": -1, "B": -1, "C": -1, "D": 49, "E": 1}
