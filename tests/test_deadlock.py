"""Deadlock resolution through the real scheduler.

The algorithm itself is proved in `test_waitgraph.py`, against hand-built
graphs and without a single thread. What this module proves is the other half:
that the wait edges actually get registered as Innies block, that detection
fires from inside the Registry's critical section, and that every cycle member
ends up holding -1 rather than hanging.

Deadlock is a *value*, never an error: an Innie that merely depends on a cycle
reads -1 as ordinary data and finishes normally.
"""

from __future__ import annotations

from lumon.cell import Result
from lumon.errors import NoWorkProduct
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
    """A self-reference is a legal input with a defined answer -- a cycle of
    length 1, resolved to -1, NOT a parse error."""
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
    """Deadlock is a value, not an error. It doubles as the guard on
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
    that is false, so no cycle forms and both complete normally."""
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


def test_a_late_arriving_edge_still_joins_the_component() -> None:
    """Detection must read the *whole* wait-for graph, not the part of it that
    happened to exist first.

    A and B are circular on their own and block almost immediately. LATE
    closes a bigger loop -- B waits on it, it waits on B -- but only after a
    12-link chain resolves, so it registers its edge long after A and B are
    asleep. Detecting the moment an edge lands would resolve the two-node
    component, leave LATE to read -1 and finish with 0, and produce a
    different answer on any run where LATE got there in time. Detection waits
    until nothing can move, so all three are one component on every run.

    This is asserted by value and by repetition, never by timing: the chain
    length only decides how likely the bad interleaving is, not what the
    correct answer is.
    """
    chain: list[tuple[str, str]] = [("C0", "LOAD 1\nWAFFLE")]
    chain += [(f"C{i}", f"LOAD 0\nADD C{i - 1}\nWAFFLE") for i in range(1, 12)]
    schedule = sched(
        *chain,
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD [A, LATE]\nWAFFLE"),
        ("LATE", "LOAD 0\nADD C11\nADD B\nWAFFLE"),
    )
    for _ in range(50):
        settled = values(schedule)
        assert (settled["A"], settled["B"], settled["LATE"]) == (-1, -1, -1)
        assert settled["C11"] == 1


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


# ------------------------------------------------------------------ OR-waits


def test_or_branch_escapes_a_cycle_no_false_deadlock() -> None:
    """HELLY's ANY OF has two branches. MARK cycles back to HELLY; DYLAN does
    not. HELLY must escape via DYLAN and complete normally -- an AND-only wait
    graph calls HELLY and MARK a cycle here, which is deterministic and
    wrong."""
    assert values(
        sched(
            ("DYLAN", "LOAD 1\nWAFFLE"),
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]\nWAFFLE"),
            ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
        )
    ) == {"DYLAN": 1, "HELLY": 0, "MARK": 0}


def test_or_branch_with_no_escape_is_still_a_cycle() -> None:
    """MARK is HELLY's only branch, and MARK waits on HELLY. Genuine cycle."""
    assert values(
        sched(
            ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK]\nWAFFLE"),
            ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
        )
    ) == {"HELLY": -1, "MARK": -1}


def test_all_of_short_circuits_on_a_false_branch_and_escapes() -> None:
    """5 > FALSIFIER is false, so ALL OF is false without ever needing LATE --
    which cycles back. HELLY escapes."""
    assert values(
        sched(
            ("FALSIFIER", "LOAD 999\nWAFFLE"),
            ("HELLY", "LOAD 7\nWELLNESS_CHECK 5 > ALL OF [FALSIFIER, LATE]\nWAFFLE"),
            ("LATE", "LOAD 0\nADD HELLY\nWAFFLE"),
        )
    ) == {"FALSIFIER": 999, "HELLY": 7, "LATE": 7}


# ------------------------------------------ an absorbing value beats a fault


def test_an_absorbing_branch_wins_over_a_faulting_one_whatever_settles_first() -> None:
    """VOID_X publishes no work product, so reading it faults; GOOD satisfies
    the predicate. `true` is absorbing, so the fold is `true` however it settles
    first -- if the fault were raised on arrival instead, the answer would
    depend on thread timing, and P would be 9 on some runs and 0 on others.
    Repeated because that is exactly the kind of bug one run can hide.
    """
    schedule = sched(
        ("VOID_X", "LOAD 5"),  # no WAFFLE: VOID, and reading it raises
        ("GOOD", "LOAD 1\nWAFFLE"),
        ("P", "LOAD 9\nWELLNESS_CHECK 5 > ANY OF [VOID_X, GOOD]\nWAFFLE"),
    )
    for _ in range(50):
        assert values(schedule) == {"VOID_X": None, "GOOD": 1, "P": 0}


def test_a_false_branch_absorbs_all_of_ahead_of_a_faulting_one() -> None:
    """The same rule for the AND-fold: `false` is absorbing, so FALSIFIER
    decides the answer and VOID_X's fault never surfaces."""
    assert values(
        sched(
            ("VOID_X", "LOAD 5"),
            ("FALSIFIER", "LOAD 999\nWAFFLE"),
            ("P", "LOAD 9\nWELLNESS_CHECK 5 > ALL OF [VOID_X, FALSIFIER]\nWAFFLE"),
        )
    ) == {"VOID_X": None, "FALSIFIER": 999, "P": 9}


def test_a_deferred_fault_still_faults_the_reader_when_nothing_absorbs_it() -> None:
    """Deferred, not discarded: with no absorbing value anywhere in the fold,
    the fault is the answer and P faults rather than quietly reading
    `ANY OF` as false."""
    settled = results(
        sched(
            ("VOID_X", "LOAD 5"),
            ("P", "LOAD 9\nWELLNESS_CHECK 5 > ANY OF [VOID_X]\nWAFFLE"),
        )
    )
    fault = settled["P"].error
    assert isinstance(fault, NoWorkProduct)
    assert fault.innie_id == "VOID_X"
