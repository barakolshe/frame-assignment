"""AND-wait detection: the strongly-connected component containing a cycle."""

from __future__ import annotations

from lumon.deadlock.base import DeadlockDetector
from lumon.waitgraph import WaitGraph


class SccDetector(DeadlockDetector):
    """Finds cycle members as an SCC, not as the path a search happened to
    walk. SCC membership is a property of the graph -- independent of which
    thread ran the search and in what order it visited nodes -- and that is
    exactly what makes the answer deterministic.

    OR-groups are invisible here, so this is the AND-only reference: correct
    for `ADD [B, C]`, wrong for `ANY OF [B, C]` (it would report a cycle for
    an Innie that still has a live escape branch). `AndOrDetector` is what the
    Registry actually runs; this one stays as the algorithm the AND-only tests
    pin, and as the thing that generalization has to reduce to.

    The graph holds at most one node per blocked Innie, so the naive O(V*E)
    form is fine at this scale; Tarjan would be a correct substitute.
    """

    def find_cycle(self, graph: WaitGraph) -> list[str] | None:
        for node in sorted(graph.blocked()):
            forward = self._reachable_from(graph, node)
            if node not in forward:
                continue  # cannot reach itself: no cycle through this node
            scc = {n for n in forward if node in self._reachable_from(graph, n)}
            return sorted(scc | {node})  # sorted: a set has no stable order
        return None

    @staticmethod
    def _reachable_from(graph: WaitGraph, start: str) -> set[str]:
        seen: set[str] = set()
        frontier = list(graph.and_edges(start))
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(graph.and_edges(node))
        return seen
