"""The scheduler, end to end: text in, settled Registry out.

This is the first test module that runs real threads. Two things it proves and
one it deliberately does not:

* **Every Innie settles exactly once.** Success, VOID, or fault -- no escape
  path may leave a Cell pending, because a pending Cell hangs every dependent.
* **One Outie thread per Innie, not a pool.** Asserted structurally by counting
  the threads the runner constructs, never by timing.

Not proved here: deadlock resolution. Nothing in this module contains a cycle;
that is Task 12's job and it must not change a single number below.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from lumon.cell import Registry, Result
from lumon.errors import DependencyFaulted, NoWorkProduct, WatchdogTimeout
from lumon.loader import Innie, load
from lumon.runners import RUNNERS, ConcurrentRunner, run_concurrent
from lumon.runners.concurrent import WATCHDOG_SECONDS

Schedule = dict[str, list[dict[str, str]]]


def results(schedule: Schedule) -> dict[str, Result]:
    registry = run_concurrent(load(schedule))
    return {result.innie_id: result for result in registry.snapshot()}


def values(schedule: Schedule) -> dict[str, int | None]:
    return {innie_id: result.value for innie_id, result in results(schedule).items()}


# ------------------------------------------------------------ the happy path


def test_independent_innies_all_complete() -> None:
    assert values(
        {
            "innies": [
                {"id": "A", "schedule": "LOAD 1\nWAFFLE"},
                {"id": "B", "schedule": "LOAD 2\nWAFFLE"},
            ]
        }
    ) == {"A": 1, "B": 2}


def test_a_dependent_innie_waits_and_reads() -> None:
    assert values(
        {
            "innies": [
                {"id": "A", "schedule": "LOAD 10\nWAFFLE"},
                {"id": "B", "schedule": "LOAD 5\nADD A\nWAFFLE"},
            ]
        }
    ) == {"A": 10, "B": 15}


def test_multiple_waffles_publish_only_the_last() -> None:
    """S1, and the reason 2.json exists.

    BURT reads DYLAN twice and must see 100 both times: WAFFLE stages, the
    runner publishes once, and a published value is immutable. Under an eager
    publish the first read could return 1 and the answer would depend on
    timing.
    """
    assert values(
        {
            "innies": [
                {"id": "DYLAN", "schedule": "LOAD 1\nWAFFLE\nLOAD 100\nWAFFLE"},
                {"id": "BURT", "schedule": "LOAD 0\nADD [DYLAN]\nWAFFLE\nADD [DYLAN]\nWAFFLE"},
            ]
        }
    ) == {"DYLAN": 100, "BURT": 200}


def test_deep_chain_of_dependencies() -> None:
    """N0 <- N1 <- ... <- N39, every link a blocking wait.

    The behavioural counterpart to `test_one_thread_per_innie_not_a_pool`: a
    bounded pool smaller than 40 would park its workers on Innies that never
    get a worker, and starve.
    """
    depth = 40
    innies = [{"id": "N0", "schedule": "LOAD 1\nWAFFLE"}]
    innies += [
        {"id": f"N{i}", "schedule": f"LOAD 0\nADD N{i - 1}\nADD 1\nWAFFLE"}
        for i in range(1, depth)
    ]
    assert values({"innies": innies})[f"N{depth - 1}"] == depth


# ------------------------------------------------------ settling, not hanging


def test_an_innie_with_no_waffle_is_void_not_pending() -> None:
    """S2. VOID is a settled Cell, which is the whole point -- a pending one
    would hang every dependent."""
    assert results({"innies": [{"id": "A", "schedule": "LOAD 5"}]})["A"].is_void


def test_reading_a_void_innie_faults_the_reader() -> None:
    """S2: A settled fine, it just has no work product. So B does not get a
    `DependencyFaulted` -- A did not fault -- it gets the `NoWorkProduct` that
    reading a VOID Innie raises. B's *own* fault is then an ordinary
    dependency fault for C, chained back to the cause."""
    res = results(
        {
            "innies": [
                {"id": "A", "schedule": "LOAD 5"},
                {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
                {"id": "C", "schedule": "LOAD 0\nADD B\nWAFFLE"},
            ]
        }
    )
    assert res["A"].is_void

    void_read = res["B"].error
    assert isinstance(void_read, NoWorkProduct)
    assert void_read.innie_id == "A"

    downstream = res["C"].error
    assert isinstance(downstream, DependencyFaulted)
    assert isinstance(downstream.__cause__, NoWorkProduct)


def test_faults_propagate_down_a_chain_with_chained_causes() -> None:
    """S10: a fault is not a deadlock. It travels, chained, and every Cell on
    the way still settles."""
    res = results(
        {
            "innies": [
                {"id": "A", "schedule": "LOAD 5\nMODULO 0\nWAFFLE"},
                {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
                {"id": "C", "schedule": "LOAD 0\nADD B\nWAFFLE"},
            ]
        }
    )
    assert res["A"].is_fault and res["B"].is_fault and res["C"].is_fault
    error = res["C"].error
    assert isinstance(error, DependencyFaulted)
    assert isinstance(error.__cause__, DependencyFaulted)


# ------------------------------------------------------------- determinism


def test_repeated_runs_are_identical() -> None:
    schedule: Schedule = {
        "innies": [
            {"id": "A", "schedule": "LOAD 10\nWAFFLE"},
            {"id": "B", "schedule": "LOAD 2\nWAFFLE"},
            {"id": "C", "schedule": "LOAD 0\nADD [A, B]\nWAFFLE"},
            {"id": "D", "schedule": "LOAD 100\nWELLNESS_CHECK C > ALL OF [A, B]\nWAFFLE"},
        ]
    }
    first = values(schedule)
    assert first == {"A": 10, "B": 2, "C": 12, "D": 0}
    for _ in range(100):
        assert values(schedule) == first


# ------------------------------------------------------- structural: threads


def test_one_thread_per_innie_not_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """One daemon Outie per Innie -- counted, not timed.

    A bounded pool is the tempting optimisation and it is wrong: N Innies
    blocked inside an N-worker pool starve the very Innies they are waiting
    on. The subclass is defined before the patch, so its base is the real
    Thread.
    """
    constructed: list[threading.Thread] = []

    class SpyThread(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            constructed.append(self)

    monkeypatch.setattr(threading, "Thread", SpyThread)

    assert values(
        {
            "innies": [
                {"id": "A", "schedule": "LOAD 1\nWAFFLE"},
                {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
                {"id": "C", "schedule": "LOAD 0\nADD B\nWAFFLE"},
                {"id": "D", "schedule": "LOAD 0\nADD [A, B, C]\nWAFFLE"},
            ]
        }
    ) == {"A": 1, "B": 1, "C": 1, "D": 3}

    assert sorted(thread.name for thread in constructed) == [
        "outie-A",
        "outie-B",
        "outie-C",
        "outie-D",
    ]
    assert all(thread.daemon for thread in constructed), "a hung run must not wedge the suite"


def test_any_of_does_not_serialize_on_a_slow_branch() -> None:
    """SLOW hangs off a 20-link chain; FAST is immediate. `ANY OF` must be
    satisfiable by FAST without the whole chain resolving first.

    Asserted by result, not by timing: a sequential `any_of` would also pass
    this, so it is deliberately paired with
    `test_deadlock.py::test_or_branch_escapes_a_cycle_no_false_deadlock`, which
    can only pass with a genuine OR-wait.
    """
    chain: list[dict[str, str]] = [{"id": "C0", "schedule": "LOAD 1\nWAFFLE"}]
    chain += [
        {"id": f"C{i}", "schedule": f"LOAD 0\nADD C{i - 1}\nWAFFLE"} for i in range(1, 20)
    ]
    assert (
        values(
            {
                "innies": chain
                + [
                    {"id": "FAST", "schedule": "LOAD 1\nWAFFLE"},
                    {
                        "id": "P",
                        "schedule": "LOAD 9\nWELLNESS_CHECK 5 > ANY OF [FAST, C19]\nWAFFLE",
                    },
                ]
            }
        )["P"]
        == 0
    )


def test_runners_registry_exposes_the_concurrent_runner() -> None:
    """RUNNERS is the OCP extension point the CLI derives --mode from: a third
    strategy is a dict entry, never an edit to the CLI."""
    assert ConcurrentRunner.name == "concurrent"
    assert RUNNERS["concurrent"] is ConcurrentRunner


# ---------------------------------------------------------------- the watchdog


def test_the_watchdog_turns_a_hung_run_into_a_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged run must fail loudly, naming the threads and the Cells.

    The hang is forced by an Event this test never sets, so the Outie provably
    never returns -- no sleep, and no assertion on how long anything took. The
    deadline only decides how soon the diagnosis arrives.

    Note what the watchdog does NOT do: settle the stragglers to -1. That would
    turn every future hang into a plausible-looking answer. It is a bug
    detector, and in a correct implementation it never fires at all.
    """
    wedged = threading.Event()

    def never_returns(innie: Innie, registry: Registry) -> None:
        wedged.wait()

    monkeypatch.setattr("lumon.runners.concurrent.run_innie", never_returns)
    innies = load({"innies": [{"id": "A", "schedule": "LOAD 1\nWAFFLE"}]})
    try:
        with pytest.raises(WatchdogTimeout) as exc:
            ConcurrentRunner(timeout=0.05).run(innies)
    finally:
        wedged.set()  # release the daemon thread rather than leaking it

    assert "outie-A" in str(exc.value), "the message must name the stuck thread"
    assert "cells still pending: ['A']" in str(exc.value)


def test_the_watchdog_has_a_deadline_when_the_cli_constructs_a_runner() -> None:
    """`RUNNERS[mode]()` takes no arguments, so the default is what every real
    run gets. Asserted structurally -- nothing here waits for it."""
    assert ConcurrentRunner().timeout == WATCHDOG_SECONDS
