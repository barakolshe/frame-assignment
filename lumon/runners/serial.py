"""The single-threaded reference oracle -- one Outie doing every Innie's work.

**Why this exists alongside the threaded runner.** `ConcurrentRunner` is the
deliverable: the spec asks for Outies working simultaneously. This one is how
we check that the threaded one is right. It drives the SAME interpreter
through the SAME Resolver seam, so the two can only disagree if state has
leaked outside the Registry -- which makes "the results are deterministic" a
claim a test can fail on rather than one nobody can falsify. See the module
docstring of `lumon/resolvers/serial.py` for why a single implementation
cannot check itself.

Only one of the two runs in any given execution: `--mode` picks it, once, up
front. They never cooperate, and nothing switches between them mid-run. The
only place both appear is a test that runs the same schedule through each and
diffs the registries.

It is a control, not a fallback for when threads are inconvenient -- but it
ships, and `--mode serial` is the debugging path when a number looks wrong:
one thread, no interleaving, a stack trace that means something.

Lazy, memoized and recursive rather than a topological sort. A static order
does not exist -- `CONDITIONAL_ADD` only resolves its list when its condition
holds (S6), so the dependency graph is discovered as it is walked. Recursion
gives cycle detection for free: an Innie already on the evaluation stack is a
cycle, by definition.
"""

from __future__ import annotations

from lumon.cell import DEADLOCK_VALUE, Registry, Result
from lumon.interp import execute
from lumon.isa import Program
from lumon.loader import Innie
from lumon.resolvers.serial import SerialResolver
from lumon.runners.base import Runner, register


class _Reentry(Exception):
    """Control flow, not a failure -- which is why it is here and not in
    `errors.py`. Raised when resolving a branch would re-enter an Innie below
    the probe that started it, and caught by that probe, which then reports
    the branch as deferrable (S8b).
    """

    def __init__(self, innie_id: str) -> None:
        super().__init__(f"branch re-enters {innie_id}")
        self.innie_id = innie_id


class LazyEvaluator:
    """Evaluates Innies on demand, memoizing into a Registry.

    Two pieces of state, and the interesting one is the second:

    * `_on_stack` -- the Innies currently being evaluated, outermost first.
      Membership means a cycle.
    * `_barriers` -- the stack depth at which each in-flight `probe` began.
      A cycle reaching *below* the innermost barrier is deferrable, because
      the fold that started that probe may still have a healthy branch to
      take. A cycle contained entirely above it is not: no deferral can help,
      so it settles to -1 where it is found.
    """

    def __init__(self, innies: list[Innie]) -> None:
        self._programs: dict[str, Program] = {innie.id: innie.program for innie in innies}
        # Pre-populated from the full Innie list (S11) and doubling as the memo
        # table: a settled Cell is a cached result, so each Innie runs once.
        self._registry = Registry(innie.id for innie in innies)
        self._on_stack: list[str] = []
        self._barriers: list[int] = []

    # -- the Evaluator protocol -----------------------------------------

    def probe(self, innie_id: str) -> Result | None:
        """Try to resolve a branch, backing out if it turns out to be cyclic.

        Nothing is committed on the `_Reentry` path, so backing out costs only
        the work done -- the branch is simply evaluated again later, and
        evaluation is pure and memoized. Cells that *did* settle during the
        attempt are keepers: they are either real values or cycles that live
        entirely inside the branch and would resolve to -1 regardless.
        """
        self._barriers.append(len(self._on_stack))
        try:
            return self.result_for(innie_id)
        except _Reentry:
            return None
        finally:
            self._barriers.pop()

    def result_for(self, innie_id: str) -> Result:
        cell = self._registry.cell(innie_id)
        memoized = cell.peek()
        if memoized is not None:
            return memoized

        if innie_id in self._on_stack:
            return self._cycle(innie_id)

        self._on_stack.append(innie_id)
        try:
            outcome = execute(self._programs[innie_id], SerialResolver(self))
        except _Reentry:
            raise  # a deferral signal in flight -- not this Innie's failure
        except BaseException as error:  # deliberate catch-all, as in the runner
            # S10: the fault becomes this Innie's work product. Guarded because
            # a cycle may already have settled this Cell to -1 underneath us.
            if cell.peek() is None:
                cell.fault(error)
            return self._settled(cell.peek(), innie_id)
        finally:
            self._on_stack.pop()

        if cell.peek() is None:  # not already settled as a cycle member
            if outcome.staged is None:
                cell.commit_void()  # S2: a workday with no WAFFLE publishes VOID
            else:
                cell.commit(outcome.staged)  # S1: the last staged value
        return self._settled(cell.peek(), innie_id)

    # -- cycles ----------------------------------------------------------

    def _cycle(self, innie_id: str) -> Result:
        """`innie_id` is already under evaluation, so the walk has closed a
        loop. Either defer it, or settle the loop to -1."""
        position = self._on_stack.index(innie_id)

        if self._barriers and position < self._barriers[-1]:
            # The loop passes below the innermost probe, so the fold that
            # started that probe might still escape through another branch.
            # Unwind and let it try (S8b).
            raise _Reentry(innie_id)

        # No probe can help: everything from here up is on the cycle, and only
        # cycle members publish -1 (S9). Their dependents read it as data.
        for member in self._on_stack[position:]:
            member_cell = self._registry.cell(member)
            if member_cell.peek() is None:
                member_cell.commit(DEADLOCK_VALUE)
        return self._settled(self._registry.cell(innie_id).peek(), innie_id)

    # -- plumbing --------------------------------------------------------

    @staticmethod
    def _settled(result: Result | None, innie_id: str) -> Result:
        """Every path above settles the Cell before reading it back; this
        turns "the type says it might be None" into a loud failure if one of
        them ever stops doing so."""
        if result is None:
            raise AssertionError(f"{innie_id} unsettled after evaluation")
        return result

    def run(self) -> Registry:
        for innie_id in self._registry.ids():
            self.result_for(innie_id)
        return self._registry


@register
class SerialRunner(Runner):
    """The Runner contract, met without a single thread: every Cell settled on
    return, and results that depend only on the input (S8)."""

    name = "serial"

    def run(self, innies: list[Innie]) -> Registry:
        return LazyEvaluator(innies).run()


def run_serial(innies: list[Innie]) -> Registry:
    return SerialRunner().run(innies)
