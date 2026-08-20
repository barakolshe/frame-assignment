"""Read a work schedule, run it, print the registry as JSON.

The only module that knows an end user exists. It picks a runner out of
`RUNNERS` by name and otherwise knows nothing about how either one works
(DIP) -- a third strategy is a dict entry, and `--mode` gains a choice
without this file being edited.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from lumon.cell import Registry
from lumon.errors import LumonError
from lumon.loader import load_path
from lumon.runners import RUNNERS  # importing this is what registers the runners

DEFAULT_MODE = "concurrent"


def format_results(registry: Registry) -> dict[str, list[dict[str, object]]]:
    """The output contract: one entry per Innie, in input order (S11).

    `result` is the published integer, or `null` for the two ways an Innie can
    fail to have one -- VOID (S2) and a fault (S10). Only a fault also carries
    `error`, which is what distinguishes them. A deadlocked Innie is neither:
    it published the integer -1 (S9), and reports as an ordinary value.
    """
    innies: list[dict[str, object]] = []
    for result in registry.snapshot():
        entry: dict[str, object] = {"id": result.innie_id, "result": None}
        if result.error is not None:
            entry["error"] = _describe(result.error)
        else:
            entry["result"] = result.value
        innies.append(entry)
    return {"innies": innies}


def _describe(error: BaseException) -> str:
    """Follow the `__cause__` chain, so a propagated fault reads
    'dependency DYLAN faulted because line 2: MODULO by zero' (S10).

    The `seen` set is not paranoia: `raise ... from` accepts a cycle, and a
    cycle here would be an infinite loop in the error path -- the worst place
    to have one.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__
    return " because ".join(parts)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lumon", description="Run a Lumon Innie work schedule."
    )
    parser.add_argument("schedule", help="path to the work schedule JSON")
    parser.add_argument(
        # Choices come from the registry, so a new Runner needs no edit here.
        "--mode",
        choices=sorted(RUNNERS),
        default=DEFAULT_MODE,
        help=f"execution strategy (default: {DEFAULT_MODE})",
    )
    args = parser.parse_args(argv)

    try:
        innies = load_path(args.schedule)
    except (LumonError, OSError, json.JSONDecodeError) as error:
        # Bad input is the user's problem to fix, so it gets a message rather
        # than a traceback, and its own exit code to distinguish "this
        # schedule is invalid" from "this schedule ran and something faulted".
        print(f"lumon: {error}", file=sys.stderr)
        return 2

    output = format_results(RUNNERS[args.mode]().run(innies))
    print(json.dumps(output, indent=2))
    return 1 if any("error" in innie for innie in output["innies"]) else 0
