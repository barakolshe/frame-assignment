"""AND/OR deadlock detection.

An AND-wait (`ADD [B, C]`) is blocked if ANY dependency is stuck.
An OR-wait (`ANY OF [B, C]`) is blocked only if ALL branches are stuck.

Mixing the two needs a greatest fixpoint rather than a plain reachability walk:
whether an OR-waiter is stuck depends on whether its branches are, which
depends in turn on their waiters. Treating an OR-wait as an AND-wait -- the
tempting shortcut -- invents deadlocks, handing -1 to an Innie that still had
a live branch to escape through.
"""

from __future__ import annotations

from lumon.deadlock.base import DeadlockDetector
from lumon.waitgraph import WaitGraph


class AndOrDetector(DeadlockDetector):
    """Two phases:

    1. **Stuck set, by greatest fixpoint.** Assume every blocked Innie is
       stuck, then repeatedly release any that still has a way forward. The
       fixpoint is unique and independent of iteration order, so it is
       deterministic -- the property S9 rests on.
    2. **Cycles within the stuck set.** Restrict the graph to stuck-to-stuck
       edges and take the SCC. Only those get -1; the rest of the stuck set is
       waiting *on* a cycle and is rescued when the cycle publishes.

    With no OR-groups present this reduces exactly to `SccDetector`, which is
    what makes it a drop-in replacement rather than a second semantics.
    """

    def find_cycle(self, graph: WaitGraph) -> list[str] | None:
        stuck = self._stuck_set(graph)
        if not stuck:
            return None

        for node in sorted(stuck):
            forward = self._reachable(graph, stuck, node)
            if node not in forward:
                continue  # stuck, but not on a cycle: waiting on one
            scc = {n for n in forward if node in self._reachable(graph, stuck, n)}
            return sorted(scc | {node})
        return None

    # -- phase 1 ----------------------------------------------------------

    def _stuck_set(self, graph: WaitGraph) -> set[str]:
        stuck = graph.blocked()
        changed = True
        while changed:
            changed = False
            for node in sorted(stuck):
                if self._has_way_forward(graph, node, stuck):
                    stuck.discard(node)
                    changed = True
        return stuck

    @staticmethod
    def _has_way_forward(graph: WaitGraph, node: str, stuck: set[str]) -> bool:
        """Progress is possible when every AND-dependency can progress and
        every OR-group holds at least one member that can. An Innie that is
        not blocked at all is not in `stuck`, so waiting on a running Innie
        counts as a way forward."""
        for dep in graph.and_edges(node):
            if dep in stuck:
                return False
        for group in graph.or_groups(node):
            if group and all(member in stuck for member in group):
                return False
        return True

    # -- phase 2 ----------------------------------------------------------

    @staticmethod
    def _stuck_edges(graph: WaitGraph, stuck: set[str], node: str) -> set[str]:
        """The edges that can carry a cycle: AND-edges into the stuck set, and
        OR-groups only where the *whole* group is stuck. A group with a live
        branch is an escape route, not a dependency."""
        edges = {dep for dep in graph.and_edges(node) if dep in stuck}
        for group in graph.or_groups(node):
            if group and all(member in stuck for member in group):
                edges |= group
        return edges

    def _reachable(self, graph: WaitGraph, stuck: set[str], start: str) -> set[str]:
        seen: set[str] = set()
        frontier = list(self._stuck_edges(graph, stuck, start))
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(self._stuck_edges(graph, stuck, node))
        return seen
