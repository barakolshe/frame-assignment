"""Deadlock detectors: pure functions from a wait-for graph to cycle members.

`AndOrDetector` is what the Registry runs -- it understands both kinds of wait.
`SccDetector` is the AND-only algorithm it must reduce to when no OR-group is
present, and the reference the AND-only tests pin.
"""

from lumon.deadlock.andor import AndOrDetector
from lumon.deadlock.base import DeadlockDetector
from lumon.deadlock.scc import SccDetector

__all__ = ["AndOrDetector", "DeadlockDetector", "SccDetector"]
