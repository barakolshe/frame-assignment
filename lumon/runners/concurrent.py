"""One Outie (thread) per Innie -- the spec's "multiple Outies", literally."""

from __future__ import annotations

import threading

from lumon.cell import Registry
from lumon.errors import Cancelled
from lumon.interp import execute
from lumon.loader import Innie
from lumon.resolvers.concurrent import ConcurrentResolver
from lumon.runners.base import Runner, register


def run_innie(innie: Innie, registry: Registry) -> None:
    """The body of one Outie thread.

    The catch-all is load-bearing, not defensive habit. This function has one
    job beyond running the program: guarantee that the Cell is settled on
    every path out, because an unsettled Cell hangs every dependent for the
    rest of the run and no watchdog exists yet to notice. Success, VOID, and
    fault are the only three exits.

    Deadlock adds a fourth, and it is the exception to the rule above: a
    cancelled Innie's Cell was already settled to -1 by the thread that
    detected the cycle, so this one must return WITHOUT settling. Doing
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
            cell.fault(error)  # the fault becomes this Innie's work product
        return
    if registry.is_cancelled(innie.id):
        # Belt and braces: cancellation is only ever observed while blocked,
        # so this cannot fire today. It documents the invariant and costs a
        # dict lookup.
        return
    if outcome.staged is None:
        cell.commit_void()  # a workday with no WAFFLE publishes VOID
    else:
        cell.commit(outcome.staged)  # the last staged value, published once


@register
class ConcurrentRunner(Runner):
    """A thread per Innie, deliberately not a pool.

    A bounded pool is the tempting optimisation and it is wrong: N Innies
    blocked inside an N-worker pool starve the very Innies they are waiting
    on, turning a perfectly acyclic schedule into a hang. Threads are cheap at
    Lumon scale and correctness is not negotiable.
    """

    name = "concurrent"

    def run(self, innies: list[Innie]) -> Registry:
        # Pre-populated from the full Innie list, so every reference has
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
        for thread in threads:
            thread.join()
        return registry


def run_concurrent(innies: list[Innie]) -> Registry:
    return ConcurrentRunner().run(innies)
