"""The payoff: the two runners must produce byte-identical registries.

This is the test the whole design exists to make possible. `ConcurrentRunner`
is the deliverable and `SerialRunner` is the control; they share an
interpreter and differ only in how they wait (S8). So a disagreement is never
"the threaded one is flaky" -- it is proof that something outside the Registry
influenced a result, and it names the schedule and the Innie.

Three properties, in increasing order of strength:

1. **Both modes agree**, over a fuzz corpus that covers DAGs, faults, cycles,
   and all of them at once.
2. **The threaded mode repeats itself**, run after run on the same input.
3. **It still does under jitter**, with the interpreter switching threads
   roughly every bytecode.

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
    propagation (S10) and the S8(a) rule that an absorbing branch beats a
    faulted one inside a quantifier."""
    assert_modes_agree(seed, n=DAG_SIZE, allow_cycles=False, allow_faults=True)


@pytest.mark.parametrize("seed", range(200))
def test_cycles_resolve_identically_in_both_modes(seed: int) -> None:
    """Back-edges, so the corpus contains real circular dependencies. Both
    modes must agree on exactly which Innies are on a cycle and publish -1
    (S9) -- the hardest thing in the project to get to agree, because one mode
    reads it off a quiescent wait-for graph and the other off a recursion."""
    assert_modes_agree(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=False)


@pytest.mark.parametrize("seed", range(200))
def test_cycles_and_faults_together_settle_identically(seed: int) -> None:
    """Deadlock and fault in the same schedule, where they can race: a fault
    that surfaces before a cycle resolves changes who is on the cycle."""
    assert_modes_agree(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=True)


# ------------------------------------------------------------ self-consistency


@pytest.mark.parametrize("seed", [0, 7, 42, 99, 123])
def test_the_threaded_runner_repeats_itself(seed: int) -> None:
    """Weaker than the cross-mode tests and still worth having: it catches a
    result that varies between runs of the SAME implementation, which is the
    shape a race usually takes before it is understood."""
    innies = load(generate(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=True))
    first = fingerprint(run_concurrent(innies))
    for _ in range(50):
        assert fingerprint(run_concurrent(innies)) == first


@pytest.mark.parametrize("seed", range(50))
def test_both_modes_still_agree_under_thread_jitter(
    seed: int, aggressive_switching: None
) -> None:
    """Raw repetition explores few interleavings: under the GIL a short
    workday often runs to completion without a single switch. Dropping the
    switch interval to a microsecond forces preemption between almost every
    pair of bytecodes, which is where a lost wake-up or a torn read shows up.
    """
    assert_modes_agree(seed, n=CYCLE_SIZE, allow_cycles=True, allow_faults=True)
