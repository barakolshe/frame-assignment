"""Resolver backed by a live Registry: waits on the Cells' shared Condition.

Two kinds of wait, and the difference matters to more than speed:

* `value` / `values` are **AND**-waits -- block until *all* the named Innies
  have settled -- and go through `Registry.await_innies`.
* `any_of` / `all_of` are **OR**-waits: every branch is awaited at once and the
  first absorbing value ends the wait (S8, and Requirement 4's "unblock as soon
  as the result is determined"), via `Registry.await_any`.

Both register their edges with the wait-for graph inside the same lock
acquisition that runs detection. Waiting anywhere else would be a wait the
detector cannot see; modelling the OR-wait as an AND-wait would make an Innie
with a live escape branch look like a cycle member and hand it -1.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from lumon.cell import Registry, unwrap
from lumon.errors import Cancelled, DependencyFaulted, LumonError, NoWorkProduct
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
        return self._quantified(innie_ids, pred, absorbing=True)  # ANY OF [] -> False

    def all_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        return self._quantified(innie_ids, pred, absorbing=False)  # ALL OF [] -> True

    def _quantified(
        self,
        innie_ids: Sequence[str],
        pred: Callable[[int], bool],
        absorbing: bool,
    ) -> bool:
        """Wait on every branch at once and answer the moment an absorbing
        value lands. `absorbing` is True for ANY OF (an OR-fold, where `true`
        wins) and False for ALL OF (an AND-fold, where `false` wins).

        S8, the rule this whole method exists to obey: short-circuiting may
        change how long we wait, never what we compute. That holds because an
        absorbing value cannot be overturned by the branches it skips -- so
        the answer is a fold over the branches, not a function of the order
        they happened to settle in.

        Faults follow from the same rule and are the one place the plan's
        sketch was not deterministic. A fault is not an absorbing value, so it
        cannot decide the fold while an absorbing branch exists; raising it the
        instant it arrives would make the answer depend on which branch settled
        first. It is deferred instead, and surfaces only if no branch absorbs
        the fold -- the earliest one in the caller's order, which is what a
        strictly left-to-right resolver would have raised.
        """
        for innie_id in innie_ids:
            self.registry.cell(innie_id)  # unknown ids are a bug, not a wait (S11)

        positions = {innie_id: i for i, innie_id in enumerate(innie_ids)}
        remaining = list(innie_ids)
        deferred: list[tuple[int, LumonError]] = []

        while remaining:
            settled = self.registry.await_any(self.me, remaining)
            if self.registry.is_cancelled(self.me):
                raise Cancelled(self.me)
            remaining.remove(settled)

            with self.registry.cond:
                result = self.registry.result_locked(settled)
            try:
                value = unwrap(result)
            except (DependencyFaulted, NoWorkProduct) as fault:
                deferred.append((positions[settled], fault))
                continue

            if pred(value) is absorbing:
                return absorbing

        if deferred:
            raise min(deferred, key=lambda entry: entry[0])[1]
        return not absorbing
