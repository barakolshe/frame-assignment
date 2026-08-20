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

from lumon.errors import DependencyFaulted, DoubleSettle, NoWorkProduct

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

    Single responsibility: own the Cells and the Condition they share. It does
    not know what a cycle is -- that lives in the wait-for graph and the
    detectors.
    """

    def __init__(self, innie_ids: Iterable[str]):
        self.cond = threading.Condition()
        self._order: list[str] = list(innie_ids)
        self._cells: dict[str, Cell] = {
            innie_id: Cell(innie_id, self.cond) for innie_id in self._order
        }

    def cell(self, innie_id: str) -> Cell:
        try:
            return self._cells[innie_id]
        except KeyError:
            raise KeyError(f"no such Innie: {innie_id!r}") from None

    def ids(self) -> list[str]:
        """Innie ids in construction order -- a copy, so a caller cannot
        reorder the registry by accident."""
        return list(self._order)

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
