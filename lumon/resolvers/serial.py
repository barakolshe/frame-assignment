"""Resolver backed by a recursive evaluator: recurses on demand, never waits.

**What this is for.** Not a faster path, and not a fallback for when threads
are inconvenient: it is the instrument used to check that the *threaded*
runner is right. Both resolvers drive the same interpreter and must return the
same values for the same schedule -- the only thing they may differ on is how
long they wait. So running a schedule through both and diffing the
registries turns "the results are deterministic" into something a test can
actually fail on. `tests/test_cli.py::test_serial_mode_agrees_with_concurrent`
does that on the samples today; a generated corpus is what extends it beyond
the schedules a human thought to write.

**Why one implementation cannot check itself.** Running the threaded runner 50
times and getting the same number proves the answer is *repeatable*, not that
it is *correct* -- a wrong answer is perfectly repeatable when the OS
interleaves threads the same way every run, which on one machine it usually
does. An implementation with no threads at all cannot share that bug, so a
disagreement between the two is proof that scheduling leaked into a result,
and it names the schedule and the Innie it leaked into.

`ConcurrentResolver` blocks a thread until a Cell settles; this one evaluates
the Innie it needs, right there on the stack. Same answers, different waiting.

The one non-obvious behaviour lives in `_quantified` and it is load-bearing:
**a branch that would re-enter an Innie already under evaluation is deferred,
not resolved**. Try every branch that can stand on its own first; only if
none of them yields an absorbing result do the deferred ones get resolved,
which is when they settle to -1. The concurrent resolver gets this
ordering for free -- a cyclic branch cannot settle until the detector fires,
so a non-cyclic branch always settles first -- and without it this oracle
publishes -1 where the concurrent run completes normally.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from lumon.cell import Result, unwrap
from lumon.resolvers.base import Resolver


class Evaluator(Protocol):
    """What `SerialResolver` needs from the machinery driving it.

    Deliberately two methods wide (DIP + ISP): the resolver knows nothing
    about the memo table, the evaluation stack, or how a deferrable branch is
    detected. That also keeps the import one-directional -- `runners.serial`
    depends on this module, never the other way round.
    """

    def result_for(self, innie_id: str) -> Result:
        """Evaluate this Innie if it has not been evaluated yet, and return
        its settled Result. A cycle back to an Innie under evaluation settles
        that cycle to -1 rather than recursing forever."""

    def probe(self, innie_id: str) -> Result | None:
        """`result_for`, but `None` if the branch would re-enter an Innie
        already under evaluation -- i.e. "this one is deferrable".
        Nothing is committed on that path, so the branch can be resolved for
        real later."""


class SerialResolver(Resolver):
    """One per run, not one per Innie: the recursion carries the identity of
    the asking Innie on the call stack, so there is nothing to hold per-Innie.

    A plain class, not a model -- it wraps mutable evaluation machinery and is
    only ever constructed by the runner.
    """

    def __init__(self, evaluator: Evaluator) -> None:
        self._evaluator = evaluator

    def value(self, innie_id: str) -> int:
        # Faults never escape the evaluator -- they settle into the Cell -- so
        # `unwrap` is the single place a dependency's failure is re-raised,
        # exactly as in ConcurrentResolver.
        return unwrap(self._evaluator.result_for(innie_id))

    def values(self, innie_ids: Sequence[str]) -> list[int]:
        # An AND-wait: no branch can escape, so there is nothing to defer.
        # Left to right, so the order of `innie_ids` decides which fault wins.
        return [self.value(innie_id) for innie_id in innie_ids]

    def any_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        return self._quantified(innie_ids, pred, absorbing=True)

    def all_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        return self._quantified(innie_ids, pred, absorbing=False)

    def _quantified(
        self,
        innie_ids: Sequence[str],
        pred: Callable[[int], bool],
        absorbing: bool,
    ) -> bool:
        """The OR-fold and the AND-fold are the same walk with a different
        absorbing value: `True` ends an ANY OF, `False` ends an ALL OF.

        Two passes, so that a branch which would re-enter an Innie already
        under evaluation is tried only after the ones that can stand alone.
        Returning early on an absorbing value is always sound -- by definition
        the skipped branches cannot overturn it.
        """
        deferred: list[str] = []

        for innie_id in innie_ids:
            probed = self._evaluator.probe(innie_id)
            if probed is None:
                deferred.append(innie_id)
                continue
            if pred(unwrap(probed)) is absorbing:
                return absorbing

        for innie_id in deferred:  # no way forward, so these settle to -1
            if pred(self.value(innie_id)) is absorbing:
                return absorbing

        # Nothing absorbed: ANY OF [] -> False, ALL OF [] -> True.
        return not absorbing
