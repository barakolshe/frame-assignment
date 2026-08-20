"""Cell and Registry -- the write-once publish slot.

Concurrency is proved structurally here: Barrier and Event force the ordering
and every assertion is about counts and values. Nothing sleeps, and nothing
asserts on elapsed time.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from lumon.cell import (
    DEADLOCK_VALUE,
    Cell,
    PendingAtSnapshot,
    Registry,
    Result,
    unwrap,
)
from lumon.errors import DependencyFaulted, DoubleSettle, NoWorkProduct

JOIN_TIMEOUT = 2.0


def make_registry(*innie_ids: str) -> Registry:
    return Registry(innie_ids)


def join_all(threads: list[threading.Thread]) -> None:
    """Join every thread, failing loudly rather than wedging the suite."""
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive(), "thread never woke -- lost wakeup"


# ------------------------------------------------------------------ settling


def test_commit_then_get_returns_the_value() -> None:
    reg = make_registry("A")
    reg.cell("A").commit(42)
    assert reg.cell("A").get() == 42


def test_commit_void_then_get_raises_no_work_product() -> None:
    """An Innie with zero WAFFLEs is settled (VOID), and reading it faults."""
    reg = make_registry("A")
    reg.cell("A").commit_void()
    with pytest.raises(NoWorkProduct) as exc:
        reg.cell("A").get()
    assert exc.value.innie_id == "A"


def test_fault_then_get_raises_chained() -> None:
    """The traceback has to say why the dependency failed."""
    reg = make_registry("A")
    cause = ValueError("boom")
    reg.cell("A").fault(cause)
    with pytest.raises(DependencyFaulted) as exc:
        reg.cell("A").get()
    assert exc.value.innie_id == "A"
    assert exc.value.__cause__ is cause


SETTLERS: dict[str, Callable[[Cell], None]] = {
    "commit": lambda cell: cell.commit(1),
    "commit_void": lambda cell: cell.commit_void(),
    "fault": lambda cell: cell.fault(ValueError("boom")),
}


@pytest.mark.parametrize("second", list(SETTLERS))
@pytest.mark.parametrize("first", list(SETTLERS))
def test_a_settled_cell_cannot_settle_again(first: str, second: str) -> None:
    """Write-once, whichever way it was written and whichever way the second
    write comes in. The first result stands."""
    reg = make_registry("A")
    cell = reg.cell("A")
    SETTLERS[first](cell)
    settled = cell.peek()

    with pytest.raises(DoubleSettle):
        SETTLERS[second](cell)

    assert cell.peek() is settled


def test_is_settled_and_peek_track_the_settle() -> None:
    reg = make_registry("A")
    cell = reg.cell("A")
    assert not cell.is_settled()
    assert cell.peek() is None

    cell.commit(7)

    assert cell.is_settled()
    assert cell.peek() == Result(innie_id="A", value=7, error=None)


def test_deadlock_value_is_ordinary_data_not_an_error() -> None:
    """A dependent reading a deadlocked Innie gets the integer -1 and
    computes with it. Deadlock is a value, not a fault."""
    assert DEADLOCK_VALUE == -1
    reg = make_registry("A")
    reg.cell("A").commit(DEADLOCK_VALUE)

    assert reg.cell("A").get() == -1
    settled = reg.cell("A").peek()
    assert settled is not None
    assert not settled.is_fault


# -------------------------------------------------------------------- Result


def test_result_is_frozen() -> None:
    """A settled Cell cannot be mutated through the reference a reader holds."""
    result = Result(innie_id="A", value=1, error=None)
    with pytest.raises(ValidationError):
        result.value = 2


def test_result_shape_predicates() -> None:
    published = Result(innie_id="A", value=0, error=None)  # 0 is not VOID
    void = Result(innie_id="A", value=None, error=None)
    faulted = Result(innie_id="A", value=None, error=ValueError("boom"))

    assert (published.is_void, published.is_fault) == (False, False)
    assert (void.is_void, void.is_fault) == (True, False)
    assert (faulted.is_void, faulted.is_fault) == (False, True)


def test_unwrap_returns_the_value_of_a_published_result() -> None:
    assert unwrap(Result(innie_id="A", value=5, error=None)) == 5
    assert unwrap(Result(innie_id="A", value=0, error=None)) == 0


def test_unwrap_of_a_void_result_raises_no_work_product() -> None:
    with pytest.raises(NoWorkProduct):
        unwrap(Result(innie_id="A", value=None, error=None))


def test_unwrap_of_a_faulted_result_chains_the_cause() -> None:
    cause = ValueError("boom")
    with pytest.raises(DependencyFaulted) as exc:
        unwrap(Result(innie_id="A", value=None, error=cause))
    assert exc.value.__cause__ is cause


# ------------------------------------------------------------------ Registry


def test_registry_is_prepopulated_so_unknown_ids_fail_fast() -> None:
    """An unknown reference is a KeyError, never a hang."""
    reg = make_registry("A")
    with pytest.raises(KeyError):
        reg.cell("GHOST")


def test_the_same_cell_is_returned_every_time() -> None:
    reg = make_registry("A")
    assert reg.cell("A") is reg.cell("A")


def test_ids_preserves_construction_order_and_returns_a_copy() -> None:
    reg = make_registry("B", "A", "C")
    assert reg.ids() == ["B", "A", "C"]
    reg.ids().append("MUTATED")
    assert reg.ids() == ["B", "A", "C"]


def test_snapshot_preserves_construction_order() -> None:
    reg = make_registry("B", "A", "C")
    reg.cell("A").commit(1)
    reg.cell("B").commit(2)
    reg.cell("C").commit_void()

    snapshot = reg.snapshot()
    assert [r.innie_id for r in snapshot] == ["B", "A", "C"]
    assert [r.value for r in snapshot] == [2, 1, None]


def test_snapshot_reports_a_pending_cell_as_a_fault() -> None:
    """A pending Cell is always a bug, so it surfaces as a named fault rather
    than silently vanishing from the output."""
    reg = make_registry("A", "B")
    reg.cell("A").commit(1)

    by_id = {r.innie_id: r for r in reg.snapshot()}
    assert by_id["A"].value == 1

    pending = by_id["B"].error
    assert isinstance(pending, PendingAtSnapshot)
    assert pending.innie_id == "B"


def test_locked_helpers_report_settlement_under_the_registry_lock() -> None:
    reg = make_registry("A", "B")
    reg.cell("A").commit(1)

    with reg.cond:
        assert reg.cell("A").is_settled_locked()
        assert not reg.cell("B").is_settled_locked()
        assert reg.cell("A").peek_locked() is not None
        assert reg.cell("B").peek_locked() is None
        assert reg.all_settled_locked(["A"])
        assert not reg.all_settled_locked(["A", "B"])
        assert reg.pending_locked(["A", "B"]) == {"B"}


def test_every_cell_shares_the_registry_condition() -> None:
    """Deadlock detection needs edge registration, cycle search and blocking
    inside ONE lock acquisition. That is only structural if every Cell blocks
    on the same Condition."""
    reg = make_registry("A", "B")
    assert reg.cell("A")._cond is reg.cond
    assert reg.cell("B")._cond is reg.cond


# --------------------------------------------------------------- concurrency


def test_get_on_a_pending_cell_blocks_until_commit() -> None:
    reg = make_registry("A")
    ready = threading.Barrier(2)
    seen: list[int] = []

    def reader() -> None:
        ready.wait()
        seen.append(reg.cell("A").get())

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    ready.wait()
    assert not seen  # nothing published, so nothing can have been seen

    # Would block forever if get() held the lock while waiting.
    reg.cell("A").commit(7)

    join_all([thread])
    assert seen == [7]


def test_ten_blocked_readers_all_wake_with_the_same_value() -> None:
    reg = make_registry("A")
    ready = threading.Barrier(11)
    lock = threading.Lock()
    seen: list[int] = []

    def reader() -> None:
        ready.wait()
        value = reg.cell("A").get()
        with lock:
            seen.append(value)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(10)]
    for thread in threads:
        thread.start()
    ready.wait()
    reg.cell("A").commit(99)

    join_all(threads)
    assert seen == [99] * 10


def test_a_reader_blocked_before_a_fault_still_raises() -> None:
    reg = make_registry("A")
    ready = threading.Barrier(2)
    caught: list[DependencyFaulted] = []

    def reader() -> None:
        ready.wait()
        try:
            reg.cell("A").get()
        except DependencyFaulted as exc:
            caught.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    ready.wait()
    cause = ValueError("late boom")
    reg.cell("A").fault(cause)

    join_all([thread])
    assert [exc.__cause__ for exc in caught] == [cause]


def test_a_reader_blocked_before_a_void_raises_no_work_product() -> None:
    reg = make_registry("A")
    ready = threading.Barrier(2)
    caught: list[NoWorkProduct] = []

    def reader() -> None:
        ready.wait()
        try:
            reg.cell("A").get()
        except NoWorkProduct as exc:
            caught.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    ready.wait()
    reg.cell("A").commit_void()

    join_all([thread])
    assert [exc.innie_id for exc in caught] == ["A"]


def test_a_reader_of_one_cell_is_not_satisfied_by_another_cells_commit() -> None:
    """Every Cell shares one Condition, so committing B wakes A's reader too.
    It must re-check its own predicate and go back to waiting."""
    reg = make_registry("A", "B")
    ready = threading.Barrier(2)
    seen: list[int] = []

    def reader() -> None:
        ready.wait()
        seen.append(reg.cell("A").get())

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    ready.wait()

    reg.cell("B").commit(1)  # wakes A's reader; must not satisfy it
    assert not seen

    reg.cell("A").commit(2)
    join_all([thread])
    assert seen == [2]


def _race_to_settle(writers: int) -> tuple[list[int], int, int]:
    """One round of a commit race. Returns (winners, loser count, final value)."""
    reg = make_registry("A")
    start = threading.Barrier(writers)
    lock = threading.Lock()
    won: list[int] = []
    lost: list[int] = []

    def writer(value: int) -> None:
        start.wait()
        try:
            reg.cell("A").commit(value)
        except DoubleSettle:
            with lock:
                lost.append(value)
        else:
            with lock:
                won.append(value)

    threads = [
        threading.Thread(target=writer, args=(value,), daemon=True)
        for value in range(writers)
    ]
    for thread in threads:
        thread.start()
    join_all(threads)

    return won, len(lost), reg.cell("A").get()


def test_exactly_one_writer_settles_a_cell_under_a_race() -> None:
    """The load-bearing invariant: every Innie settles exactly once. Eight
    threads commit distinct values simultaneously; seven must lose."""
    writers = 8
    for _ in range(50):
        won, lost, final = _race_to_settle(writers)
        assert len(won) == 1
        assert lost == writers - 1
        assert final == won[0]
