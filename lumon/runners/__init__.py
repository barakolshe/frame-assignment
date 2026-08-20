"""Execution strategies.

Importing this package is what populates `RUNNERS`: each concrete runner
registers itself at import time, so the CLI can derive `--mode`'s choices from
the table without naming a single implementation.

`SerialRunner` / `run_serial` join the exports in Task 14.
"""

from lumon.runners.base import RUNNERS, Runner
from lumon.runners.concurrent import ConcurrentRunner, run_concurrent

__all__ = ["RUNNERS", "ConcurrentRunner", "Runner", "run_concurrent"]
