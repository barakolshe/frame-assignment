"""Resolver backed by a live Registry: waits on the Cells' shared Condition.

Every wait here is an AND-wait -- block until *all* the named Innies have
settled -- and it goes through `Registry.await_innies`, which registers the
wait edges and runs deadlock detection in the same lock acquisition. Waiting
anywhere else would be a wait the detector cannot see.

`any_of` / `all_of` are OR-folds, so an absorbing value could end the wait
early (S8) and Requirement 4 asks for exactly that. Task 13 makes them do it.
Until then they wait left to right, matching `DictResolver` in the tests
one-for-one: less eager than the spec wants, identical in what it returns,
which is the invariant that actually matters.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from lumon.cell import Registry, unwrap
from lumon.errors import Cancelled
from lumon.resolvers.base import Resolver


class ConcurrentResolver(Resolver):
    """One per Innie, for the duration of one workday.

    A plain class, not a model: it holds a reference to shared lock state and
    is only ever constructed by the runner.

    `me` is the asking Innie -- the name the wait-for edges and cancellation
    hang on. Cancellation surfaces as `Cancelled` raised *through* these
    methods, so the interpreter never learns that threads exist (ISP).
    """

    def __init__(self, registry: Registry, me: str) -> None:
        self.registry = registry
        self.me = me

    def value(self, innie_id: str) -> int:
        return self.values([innie_id])[0]

    def values(self, innie_ids: Sequence[str]) -> list[int]:
        # Resolve the Cells before taking the lock. An id that is not in the
        # Registry is a KeyError naming it -- the loader already rejected
        # unknown references (S11), so this can only be an internal bug, and
        # it must surface as one rather than as a wait that never ends.
        for innie_id in innie_ids:
            self.registry.cell(innie_id)

        self.registry.await_innies(self.me, set(innie_ids))
        if self.registry.is_cancelled(self.me):
            # This Innie is a cycle member; its Cell already holds -1.
            raise Cancelled(self.me)

        with self.registry.cond:
            results = [self.registry.result_locked(innie_id) for innie_id in innie_ids]

        # unwrap only ever raises, and raising while holding the Condition that
        # every other Outie blocks on is a hazard worth not having. The order
        # of `innie_ids` decides which fault wins, so it is deterministic.
        return [unwrap(result) for result in results]

    def any_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        # `any` stops at the first true and the generator is lazy, so a later
        # Innie is genuinely never waited on. `ANY OF []` -> False.
        return any(pred(self.value(innie_id)) for innie_id in innie_ids)

    def all_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        # `all` stops at the first false. `ALL OF []` -> True.
        return all(pred(self.value(innie_id)) for innie_id in innie_ids)
