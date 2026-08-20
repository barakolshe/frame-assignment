"""One Outie (thread) per Innie -- the spec's "multiple Outies", literally."""

from __future__ import annotations

import threading

from lumon.cell import Registry
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
    """
    cell = registry.cell(innie.id)
    resolver = ConcurrentResolver(registry, innie.id)
    try:
        outcome = execute(innie.program, resolver)
    except BaseException as error:  # deliberate catch-all -- see the docstring
        cell.fault(error)  # S10: the fault becomes this Innie's work product
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
        for thread in threads:
            thread.join()
        return registry


def run_concurrent(innies: list[Innie]) -> Registry:
    return ConcurrentRunner().run(innies)
