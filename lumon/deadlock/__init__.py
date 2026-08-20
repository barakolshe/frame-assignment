"""Deadlock detectors: pure functions from a wait-for graph to cycle members.

`AndOrDetector` joins them in Task 13 and becomes the Registry's default.
"""

from lumon.deadlock.base import DeadlockDetector
from lumon.deadlock.scc import SccDetector

__all__ = ["DeadlockDetector", "SccDetector"]
