"""The wait-for graph: who is blocked on whom, right now.

Distinct from the static reference graph and much smaller -- an entry exists
only while an Innie is actually blocked. That distinction is load-bearing:
`CONDITIONAL_ADD [X] IF <cond>` creates an edge only when the condition is true
at runtime (S6), so a graph built by scanning source over-approximates and
would hand -1 to Innies that finish perfectly well.

Deliberately a plain mutable class with no lock of its own: the Registry owns
the lock and every method here is called with it held. Keeping the data
separate from both the lock and the algorithm is what lets the detectors be
unit-tested without a single thread.
"""

from __future__ import annotations

from collections.abc import Iterable


class WaitGraph:
    """AND-edges and OR-groups. No algorithm lives here -- see `lumon.deadlock`."""

    def __init__(self) -> None:
        # AND-waits: `_and[A] == {B, C}` means A needs BOTH B and C.
        self._and: dict[str, set[str]] = {}
        # OR-waits: `_or[A] == [{B, C}]` means A needs ANY of B, C.
        self._or: dict[str, list[set[str]]] = {}

    # -- AND-waits --------------------------------------------------------

    def wait_on(self, waiter: str, targets: Iterable[str]) -> None:
        """Replace `waiter`'s AND-edges. One Innie runs on one thread, so it
        can only be inside one AND-wait at a time."""
        self._and[waiter] = set(targets)

    def and_edges(self, node: str) -> set[str]:
        return self._and.get(node, set())

    # -- OR-waits ---------------------------------------------------------

    def push_or_group(self, waiter: str, targets: Iterable[str]) -> None:
        """Register one alternative set: `waiter` proceeds when ANY member
        settles. A stack, so a nested wait cannot lose the outer group."""
        self._or.setdefault(waiter, []).append(set(targets))

    def pop_or_group(self, waiter: str) -> None:
        groups = self._or.get(waiter)
        if groups:
            groups.pop()
            if not groups:
                self._or.pop(waiter, None)

    def or_groups(self, node: str) -> list[set[str]]:
        return self._or.get(node, [])

    # -- shared -----------------------------------------------------------

    def clear(self, waiter: str) -> None:
        self._and.pop(waiter, None)
        self._or.pop(waiter, None)

    def blocked(self) -> set[str]:
        """Every Innie currently waiting on something -- a fresh set, so a
        detector may narrow it down in place."""
        return set(self._and) | set(self._or)
