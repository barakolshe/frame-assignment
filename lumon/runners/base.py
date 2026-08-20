"""A Runner turns a list of Innies into a settled Registry.

The second of the three abstractions in this design, and it earns its keep:
there are genuinely two execution strategies -- threads (`concurrent.py`) and
the single-threaded reference oracle (`serial.py`) -- and the CLI has
to pick between them without knowing either (DIP).

`RUNNERS` is the OCP extension point. A third strategy is a dict entry and
`--mode` gains a choice; the CLI is never edited.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lumon.cell import Registry
from lumon.loader import Innie


class Runner(ABC):
    """Executes a whole work schedule."""

    name: str

    @abstractmethod
    def run(self, innies: list[Innie]) -> Registry:
        """Execute every Innie and return the settled Registry.

        The contract every implementation owes its callers, and what the
        determinism test checks one against the other:

        * every Cell is settled on return -- none left pending, whatever
          happened -- VOID, deadlock and fault all count as settled;
        * the results depend only on `innies`. Two runners may wait very
          differently and must still agree on every value.
        """


RUNNERS: dict[str, type[Runner]] = {}


def register(runner: type[Runner]) -> type[Runner]:
    """Add a runner to the table the CLI derives `--mode` from."""
    RUNNERS[runner.name] = runner
    return runner
