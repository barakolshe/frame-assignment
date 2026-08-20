"""Deadlock detection as a pure function of the wait-for graph.

Two implementations: `SccDetector` (AND-waits only) and `AndOrDetector` (adds
OR-waits, and is what the Registry uses by default). Both are pure -- no locks,
no threads, no Cells -- so they are unit-tested against hand-built graphs.

The third and last abstraction in this design. It earns its ABC the same way
`Resolver` and `Runner` do: there are genuinely two algorithms, and the
Registry has to depend on the interface rather than on either of them (DIP).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lumon.waitgraph import WaitGraph


class DeadlockDetector(ABC):
    """WaitGraph -> the Innies that must resolve to -1."""

    @abstractmethod
    def find_cycle(self, graph: WaitGraph) -> list[str] | None:
        """Return the members of one cycle, sorted, or None if there is none.

        Contract: the result depends only on `graph` -- never on which thread
        called this, nor on the order nodes were inserted or visited. That is
        what makes deadlock resolution deterministic (S9), and it is why the
        answer is a strongly-connected component rather than whichever path a
        search happened to walk.
        """
