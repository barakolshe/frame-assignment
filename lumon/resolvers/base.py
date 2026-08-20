"""The seam between the interpreter and the outside world.

Two implementations exist: `ConcurrentResolver` and `SerialResolver`. They
differ in *how long they wait*, never in *what they return*. That invariant --
waiting may vary, answers may not -- is what the determinism test enforces. In
SOLID terms, that test IS the Liskov check for this hierarchy.

**Why the seam is wider than `resolve(innie_id) -> int`.** A one-at-a-time
signature cannot express short-circuit: it forces the interpreter to block on
every reference in turn, which defeats the spec's "unblock as soon as the
result is determined". Putting `any_of` / `all_of` here keeps *all* waiting
strategy on the concurrency side and leaves the interpreter thread-free.

Deliberately NOT on this interface: anything about cancellation, cells, or
threads. Adding `is_cancelled()` would force `SerialResolver` to carry a
meaningless stub and leak concurrency into the interpreter (ISP). Cancellation
surfaces instead as an exception raised *through* the methods below.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence


class Resolver(ABC):
    """Reads other Innies' published work products."""

    @abstractmethod
    def value(self, innie_id: str) -> int:
        """Wait for one Innie and return its published value."""

    @abstractmethod
    def values(self, innie_ids: Sequence[str]) -> list[int]:
        """Wait for ALL of these Innies. Result order matches `innie_ids`."""

    @abstractmethod
    def any_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        """True if `pred` holds for ANY of these Innies' values.

        An OR-fold, so `true` is absorbing: an implementation may stop waiting
        the instant one value satisfies `pred`, because no unread value can
        overturn the answer. `ANY OF []` is False.
        """

    @abstractmethod
    def all_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        """True if `pred` holds for ALL of these Innies' values.

        An AND-fold, so `false` is absorbing: an implementation may stop
        waiting the instant one value fails `pred`. `ALL OF []` is True.
        """
