"""One Outie (thread) per Innie -- the spec's "multiple Outies", literally."""

from __future__ import annotations

import threading
import time

from lumon.cell import PendingAtSnapshot, Registry
from lumon.errors import Cancelled, WatchdogTimeout
from lumon.interp import execute
from lumon.loader import Innie
from lumon.resolvers.concurrent import ConcurrentResolver
from lumon.runners.base import Runner, register

# Long enough that no honest schedule at Lumon scale comes close, short enough
# that a wedged run fails a test suite instead of hanging it.
WATCHDOG_SECONDS = 30.0


def run_innie(innie: Innie, registry: Registry) -> None:
    """The body of one Outie thread.

    The catch-all is load-bearing, not defensive habit. This function has one
    job beyond running the program: guarantee that the Cell is settled on
    every path out, because an unsettled Cell hangs every dependent for the
    rest of the run -- the watchdog turns that into a diagnostic rather than
    fixing it. Success, VOID, and fault are the only three exits.

    Deadlock adds a fourth, and it is the exception to the rule above: a
    cancelled Innie's Cell was already settled to -1 by the thread that
    detected the cycle (S9), so this one must return WITHOUT settling. Doing
    otherwise trips `DoubleSettle` -- which stays a hard error, because
    everywhere else a second settle really is a bug.
    """
    cell = registry.cell(innie.id)
    resolver = ConcurrentResolver(registry, innie.id)
    try:
        outcome = execute(innie.program, resolver)
    except Cancelled:
        return  # a cycle member: the detecting thread already published -1
    except BaseException as error:  # deliberate catch-all -- see the docstring
        if not registry.is_cancelled(innie.id):
            cell.fault(error)  # S10: the fault becomes this Innie's work product
        return
    if registry.is_cancelled(innie.id):
        # Belt and braces: cancellation is only ever observed while blocked,
        # so this cannot fire today. It documents the invariant and costs a
        # dict lookup.
        return
    if outcome.staged is None:
        cell.commit_void()  # S2: a workday with no WAFFLE publishes VOID
    else:
        cell.commit(outcome.staged)  # S1: the last staged value, published once


@register
class ConcurrentRunner(Runner):
    """A thread per Innie, deliberately not a pool.

    A bounded pool is the tempting optimisation and it is wrong: N Innies
    blocked inside an N-worker pool starve the very Innies they are waiting
    on, turning a perfectly acyclic schedule into a hang. Threads are cheap at
    Lumon scale and correctness is not negotiable.
    """

    name = "concurrent"

    def __init__(self, timeout: float = WATCHDOG_SECONDS):
        """The one default in this package, and it is load-bearing: the CLI
        constructs runners generically as `RUNNERS[mode]()`, choosing a
        strategy and not a deadline. Tests that drive the watchdog pass their
        own.
        """
        self.timeout = timeout

    def run(self, innies: list[Innie]) -> Registry:
        # Pre-populated from the full Innie list (S11), so every reference has
        # a Cell to block on before any thread starts.
        registry = Registry(innie.id for innie in innies)
        threads = [
            threading.Thread(
                target=run_innie,
                args=(innie, registry),
                name=f"outie-{innie.id}",
                daemon=True,  # a hung run must never wedge the test suite
            )
            for innie in innies
        ]
        for thread in threads:
            thread.start()

        # The watchdog is a BUG DETECTOR, not a fallback. Settling the
        # stragglers to -1 here would turn every future hang into a plausible
        # looking answer; raising keeps it what it is -- a broken invariant --
        # and the message names the threads and Cells to look at. In a correct
        # implementation it never fires: every Innie settles exactly once, so
        # every wait ends.
        deadline = time.monotonic() + self.timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        stragglers = sorted(thread.name for thread in threads if thread.is_alive())
        if stragglers:
            pending = sorted(
                result.innie_id
                for result in registry.snapshot()
                if isinstance(result.error, PendingAtSnapshot)
            )
            raise WatchdogTimeout(
                f"no progress after {self.timeout}s; "
                f"threads still running: {stragglers}; "
                f"cells still pending: {pending}"
            )
        return registry


def run_concurrent(innies: list[Innie]) -> Registry:
    return ConcurrentRunner().run(innies)
