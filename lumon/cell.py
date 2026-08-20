"""Cells: write-once publish/subscribe slots, one per Innie.

Every Cell in a Registry blocks on ONE shared threading.Condition. That is
structural, not stylistic: deadlock detection has to register its wait edges
and run the cycle search inside a single lock acquisition, and
`Condition.wait()` releases that lock atomically while blocked -- so
registration, detection and blocking form one uninterrupted critical section
with no lock-ordering question left to get wrong. At Lumon scale (tens of
Innies) the contention cost is nil.

Cell and Registry are plain classes, not models: they are mutable, they hold
lock state, and they are only ever constructed internally. Result IS a model
-- an immutable value that gets compared, snapshotted and rendered.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from lumon.deadlock.base import DeadlockDetector
from lumon.deadlock.scc import SccDetector
from lumon.errors import DependencyFaulted, DoubleSettle, NoWorkProduct
from lumon.waitgraph import WaitGraph

# What every Innie on a circular dependency publishes (S9). An ordinary value:
# a dependent reads -1 and computes with it. Deadlock is not a fault.
DEADLOCK_VALUE = -1


class PendingAtSnapshot(RuntimeError):
    """A Cell was still unsettled when the Registry was snapshotted.

    Always a bug -- every Innie settles exactly once. Named rather than a bare
    RuntimeError so the watchdog can filter on it without also catching
    genuine faults that happen to be RuntimeErrors.
    """

    def __init__(self, innie_id: str):
        super().__init__(f"{innie_id} never settled")
        self.innie_id = innie_id


class Result(BaseModel):
    """One Innie's settled work product.

        value is an int, error is None  -> published (S1)
        value is None, error is None    -> VOID, finished without a WAFFLE (S2)
        error is not None               -> fault (S10)

    Frozen, so a settled Cell cannot be mutated through the reference a reader
    is holding -- the immutability S1 depends on, enforced by the type rather
    than by convention. No field defaults: VOID is stated at the construction
    site, never inferred from an omission.
    """

    # arbitrary_types_allowed: pydantic has no schema for BaseException, so it
    # validates by isinstance and stores the exception as-is. That is what we
    # want -- the original traceback has to survive for `raise ... from`.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    innie_id: str
    value: int | None
    error: BaseException | None

    @property
    def is_void(self) -> bool:
        return self.value is None and self.error is None

    @property
    def is_fault(self) -> bool:
        return self.error is not None


def unwrap(result: Result) -> int:
    """Result -> int, or the exception a reader of it deserves (S2, S10)."""
    if result.error is not None:
        raise DependencyFaulted(result.innie_id) from result.error
    if result.value is None:
        raise NoWorkProduct(result.innie_id)
    return result.value


class Cell:
    """One Innie's published work product. Written once, read many times.

    The `_locked` methods are for callers already holding the shared
    Condition; the plain ones acquire it themselves.
    """

    def __init__(self, innie_id: str, cond: threading.Condition):
        self.id = innie_id
        self._cond = cond
        self._result: Result | None = None

    # -- writes ---------------------------------------------------------

    def commit(self, value: int) -> None:
        self._settle(Result(innie_id=self.id, value=value, error=None))

    def commit_void(self) -> None:
        self._settle(Result(innie_id=self.id, value=None, error=None))

    def fault(self, error: BaseException) -> None:
        self._settle(Result(innie_id=self.id, value=None, error=error))

    def _settle(self, result: Result) -> None:
        with self._cond:
            self._settle_locked(result)

    def _settle_locked(self, result: Result) -> None:
        """Caller holds the shared Condition. Deadlock resolution settles
        through here, being already inside the critical section."""
        if self._result is not None:
            raise DoubleSettle(f"{self.id} already settled as {self._result!r}")
        self._result = result
        self._cond.notify_all()

    # -- reads ----------------------------------------------------------

    def is_settled(self) -> bool:
        with self._cond:
            return self.is_settled_locked()

    def is_settled_locked(self) -> bool:
        """Caller holds the shared Condition."""
        return self._result is not None

    def peek(self) -> Result | None:
        """The settled Result, or None if still pending. Never blocks."""
        with self._cond:
            return self.peek_locked()

    def peek_locked(self) -> Result | None:
        """Caller holds the shared Condition."""
        return self._result

    def get(self) -> int:
        """Block until this Innie settles, then unwrap what it published."""
        with self._cond:
            while self._result is None:
                self._cond.wait()
            result = self._result
        return unwrap(result)  # outside the lock: unwrap only ever raises


class Registry:
    """One Cell per Innie, pre-populated at construction (S11) so an unknown
    reference is a KeyError, never a hang.

    Single responsibility: own the Cells and the Condition they share, and be
    the one place where blocking happens. It does not know what a cycle *is* --
    that is the wait-for graph's data and the detector's algorithm, both of
    which it merely holds the lock for.

    `detector` is the DIP seam: swapping the algorithm is a constructor
    argument, never an edit to this file. It defaults rather than being
    required because every caller -- the runners, the tests -- constructs a
    Registry to run a schedule, not to choose an algorithm.
    """

    def __init__(self, innie_ids: Iterable[str], detector: DeadlockDetector | None = None):
        self.cond = threading.Condition()
        self._order: list[str] = list(innie_ids)
        self._cells: dict[str, Cell] = {
            innie_id: Cell(innie_id, self.cond) for innie_id in self._order
        }
        self._graph = WaitGraph()
        self._cancelled: set[str] = set()
        self._detector: DeadlockDetector = detector or SccDetector()

    def cell(self, innie_id: str) -> Cell:
        try:
            return self._cells[innie_id]
        except KeyError:
            raise KeyError(f"no such Innie: {innie_id!r}") from None

    def ids(self) -> list[str]:
        """Innie ids in construction order -- a copy, so a caller cannot
        reorder the registry by accident."""
        return list(self._order)

    # -- blocking protocol ------------------------------------------------

    def await_innies(self, me: str, targets: set[str]) -> None:
        """Block until every target has settled, detecting cycles first.

        Every point where an Innie blocks on *all* of a set of Innies comes
        through here: ADD/MULTIPLY/MODULO over a list, a bare Ref,
        CONDITIONAL_ADD's targets, and an unquantified condition's operands.

        The critical property: registering the wait edges and running detection
        happen inside a SINGLE lock acquisition. Split them and two threads can
        each register, each see a graph missing the other's edge, and both
        conclude there is no cycle. Because all Cells share this Condition and
        `wait()` releases it atomically, registration -> detection -> blocking
        is one uninterrupted critical section.

        Returning does not mean the targets settled: a cancelled Innie returns
        early, and the caller is expected to ask `is_cancelled` before reading
        anything.
        """
        with self.cond:
            pending = self.pending_locked(targets)
            if not pending:
                return

            self._graph.wait_on(me, pending)
            try:
                self._detect_locked()
                while not self.all_settled_locked(targets):
                    if me in self._cancelled:
                        return
                    self.cond.wait()
            finally:
                self._graph.clear(me)

    def is_cancelled(self, innie_id: str) -> bool:
        """True once this Innie has been resolved as a cycle member. Its Cell
        already holds -1, so its thread must stop without settling again."""
        with self.cond:
            return innie_id in self._cancelled

    def result_locked(self, innie_id: str) -> Result:
        """The settled Result of an Innie already waited for. Caller holds
        self.cond."""
        result = self._cells[innie_id].peek_locked()
        if result is None:
            raise AssertionError(f"{innie_id} unsettled after a completed wait")
        return result

    # -- detection --------------------------------------------------------

    def _detect_locked(self) -> None:
        """Run the detector and resolve whatever it finds. Caller holds
        self.cond.

        Called at every point an Innie registers a wait edge, which is exactly
        when a cycle can complete: whoever adds the final edge sees the whole
        cycle and resolves it.
        """
        cycle = self._detector.find_cycle(self._graph)
        if cycle:
            self._resolve_cycle(cycle)

    def _resolve_cycle(self, members: list[str]) -> None:
        """Commit -1 to every cycle member (S9). Caller holds self.cond.

        This is the one place a Cell is written by a thread other than its
        own -- the members are asleep. They learn about it on waking, via
        `is_cancelled`, and return without settling a second time. The
        already-settled guard is not paranoia: a member may have been resolved
        by an overlapping cycle a moment earlier.
        """
        for innie_id in members:
            self._cancelled.add(innie_id)
            self._graph.clear(innie_id)
            cell = self._cells[innie_id]
            if not cell.is_settled_locked():
                cell._settle_locked(
                    Result(innie_id=innie_id, value=DEADLOCK_VALUE, error=None)
                )
        self.cond.notify_all()

    def all_settled_locked(self, innie_ids: Iterable[str]) -> bool:
        """Caller holds self.cond."""
        return all(self._cells[innie_id].is_settled_locked() for innie_id in innie_ids)

    def pending_locked(self, innie_ids: Iterable[str]) -> set[str]:
        """Caller holds self.cond."""
        return {
            innie_id
            for innie_id in innie_ids
            if not self._cells[innie_id].is_settled_locked()
        }

    def snapshot(self) -> list[Result]:
        """Settled results in construction order. A still-pending Innie
        surfaces as a fault carrying PendingAtSnapshot; a correct run
        produces none."""
        results: list[Result] = []
        with self.cond:
            for innie_id in self._order:
                result = self._cells[innie_id].peek_locked()
                if result is None:
                    result = Result(
                        innie_id=innie_id,
                        value=None,
                        error=PendingAtSnapshot(innie_id),
                    )
                results.append(result)
        return results
