"""Execution strategies.

Importing this package is what populates `RUNNERS`: each concrete runner
registers itself at import time, so the CLI can derive `--mode`'s choices from
the table without naming a single implementation.
"""

from lumon.runners.base import RUNNERS, Runner
from lumon.runners.concurrent import ConcurrentRunner, run_concurrent
from lumon.runners.serial import SerialRunner, run_serial

__all__ = [
    "RUNNERS",
    "ConcurrentRunner",
    "Runner",
    "SerialRunner",
    "run_concurrent",
    "run_serial",
]
