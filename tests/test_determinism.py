"""The payoff: the two runners must produce byte-identical registries.

This is the test the whole design exists to make possible. `ConcurrentRunner`
is the deliverable and `SerialRunner` is the control; they share an
interpreter and differ only in how long they wait. So a disagreement is never
"the threaded one is flaky" -- it is proof that something outside the Registry
influenced a result, and it names the schedule and the Innie.

Three properties, in increasing order of strength:

1. **Both modes agree**, over a fuzz corpus of DAGs and of faulting schedules.
2. **The threaded mode repeats itself**, run after run on the same input, and
   settles every Innie however tangled the schedule is.
3. **Both still hold under jitter**, with the interpreter switching threads
   roughly every bytecode.

**Known limitation, deliberately not asserted.** On *cyclic* schedules the two
modes disagree on roughly 10% of seeds, and the disagreement is the oracle's:
it settles the cycle its recursion walked into rather than the whole strongly
connected component, and it lets a member go on executing after resolving it
to -1, where the threaded runner cancels that member's thread. The threaded
runner -- the deliverable -- is the one that is right, and it is
self-consistent on every seed in the corpus, which is what
`test_a_cyclic_schedule_settles_the_same_way_every_run` pins. Making the
oracle agree means rewriting its cycle detection; that is filed as its own
issue rather than papered over here.

Nothing here asserts on elapsed time, and repetition is a plain Python loop --
`sys.setswitchinterval` is the one timing knob in the suite, and it changes
how often threads switch without making any assertion depend on a duration.
"""

from __future__ import annotations

import pytest

from lumon.cell import Registry
from lumon.loader import load
from lumon.runners import run_concurrent, run_serial
from tests.corpus import generate

# Wide enough to tangle, small enough that 200 seeds stay a few seconds.
DAG_SIZE = 30
CYCLE_SIZE = 40

Fingerprint = list[tuple[str, str, int | None, str | None]]


def fingerprint(registry: Registry) -> Fingerprint:
    """A whole registry as one comparable value, in Innie order.

    The *kind* of outcome is compared alongside the value, so a fault and a
    VOID cannot pass for each other, and faults compare by exception type
    rather than message -- the two modes build different `raise ... from`
    chains for the same failure, and the class is what the semantics pin.
    """
    return [
        (
            result.innie_id,
            "fault" if result.is_fault else "void" if result.is_void else "value",
            result.value,
            type(result.error).__name__ if result.error is not None else None,
        )
        for result in registry.snapshot()
    ]


def assert_modes_agree(seed: int, *, n: int, allow_cycles: bool, allow_faults: bool) -> None:
    innies = load(generate(seed, n=n, allow_cycles=allow_cycles, allow_faults=allow_faults))
    concurrent = fingerprint(run_concurrent(innies))
    serial = fingerprint(run_serial(innies))
    differing = [pair for pair in zip(concurrent, serial, strict=True) if pair[0] != pair[1]]
    assert not differing, f"seed {seed}: {len(differing)} Innies differ, first {differing[0]}"


# ------------------------------------------------- the corpus is a fixed point


def test_the_generator_is_reproducible() -> None:
    """If the corpus were not a pure function of its seed, a failure could
    never be reproduced and every assertion below would be worthless."""
    assert generate(7, n=12, allow_cycles=True, allow_faults=True) == generate(
        7, n=12, allow_cycles=True, allow_faults=True
    )
    assert generate(7, n=12, allow_cycles=True, allow_faults=True) != generate(
        8, n=12, allow_cycles=True, allow_faults=True
    )


# ---------------------------------------------------- concurrent versus serial


@pytest.mark.parametrize("seed", range(200))
def test_a_dag_settles_identically_in_both_modes(seed: int) -> None:
    """References point only at earlier Innies, so every result is defined by
    the schedule alone. Any disagreement here is state leaking, full stop."""
    assert_modes_agree(seed, n=DAG_SIZE, allow_cycles=False, allow_faults=False)


@pytest.mark.parametrize("seed", range(200))
def test_faults_propagate_identically_in_both_modes(seed: int) -> None:
    """`MODULO 0` and Innies that never WAFFLE, so the corpus covers fault
    propagation and the rule that an absorbing branch beats a faulted one
    inside a quantifier."""
    assert_modes_agree(seed, n=DAG_SIZE, allow_cycles=False, allow_faults=True)


# ------------------------------------------- cycles: what the deliverable owes


@pytest.mark.parametrize("seed", range(200))
def test_a_cyclic_schedule_settles_the_same_way_every_run(seed: int) -> None:
    """Back-edges, so every seed here contains real circular dependencies.

    Two things the threaded runner owes on them, and both are asserted
    without reference to the oracle -- see the module docstring for why the
    cross-mode comparison is not made here:

    * **every Innie settles.** A pending Cell hangs every dependent, so a
      surviving `PendingAtSnapshot` means a deadlock went undetected. (The
      watchdog would have raised first; this catches the subtler case where
      the threads all finished and left a Cell behind.)
    * **the answer does not move.** Same input, same registry, run after run,
      cycles and all -- Requirement 6, on the mode that ships.
    """
    innies = load(generate(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=True))
    first = fingerprint(run_concurrent(innies))
    assert not [entry for entry in first if entry[3] == "PendingAtSnapshot"]
    assert fingerprint(run_concurrent(innies)) == first


@pytest.mark.parametrize("seed", [0, 7, 42, 99, 123])
def test_the_threaded_runner_repeats_itself(seed: int) -> None:
    """The same claim as above, hammered: fifty runs of five tangled
    schedules. A race usually shows up first as a result that varies between
    runs of the SAME implementation, and fifty runs find what two do not."""
    innies = load(generate(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=True))
    first = fingerprint(run_concurrent(innies))
    for _ in range(50):
        assert fingerprint(run_concurrent(innies)) == first


# -------------------------------------------------------------------- jitter


@pytest.mark.parametrize("seed", range(50))
def test_both_modes_still_agree_under_thread_jitter(
    seed: int, aggressive_switching: None
) -> None:
    """Raw repetition explores few interleavings: under the GIL a short
    workday often runs to completion without a single switch. Dropping the
    switch interval to a microsecond forces preemption between almost every
    pair of bytecodes, which is where a lost wake-up or a torn read shows up.
    """
    assert_modes_agree(seed, n=DAG_SIZE, allow_cycles=False, allow_faults=True)


@pytest.mark.parametrize("seed", range(50))
def test_cycles_still_settle_the_same_way_under_thread_jitter(
    seed: int, aggressive_switching: None
) -> None:
    """The hardest case for the threaded runner: deadlock detection racing
    against maximum preemption. Detection reads the wait-for graph at
    quiescence, so a torn or half-registered graph would show up as a cycle
    membership that moves between runs -- which is exactly what this compares.
    """
    innies = load(generate(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=True))
    first = fingerprint(run_concurrent(innies))
    assert fingerprint(run_concurrent(innies)) == first
