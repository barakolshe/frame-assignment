"""Shared test doubles.

`DictResolver` is the interpreter's stand-in for the outside world. It lives
here rather than inside a single test module because Tasks 13 and 16 reuse it
as a known-good reference the real resolvers must agree with.

`aggressive_switching` is the suite's only timing knob, and it is deliberately
not a timing *assertion*: it changes how often the interpreter switches
threads, never how long anything is allowed to take.
"""

import sys
from collections.abc import Callable, Iterator, Sequence

import pytest

from lumon.resolvers import Resolver


class DictResolver(Resolver):
    """A Resolver over a fixed dict.

    Two properties the tests lean on:

    * **Strict left-to-right short-circuit.** `any_of` stops at the first
      true, `all_of` at the first false. That is the reference behaviour the
      concurrent resolver must agree with: it may stop waiting sooner, but
      it may never return a different answer.
    * **A call log.** `touched` records which Innies were actually consulted,
      so a test can prove *structurally* that a false `CONDITIONAL_ADD` never
      resolved its list. No sleeps, no elapsed-time assertions.
    """

    def __init__(self, values: dict[str, int]) -> None:
        self._values = values
        self.touched: list[str] = []

    def value(self, innie_id: str) -> int:
        self.touched.append(innie_id)
        if innie_id not in self._values:
            raise KeyError(f"no value for {innie_id}")
        return self._values[innie_id]

    def values(self, innie_ids: Sequence[str]) -> list[int]:
        return [self.value(innie_id) for innie_id in innie_ids]

    def any_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        # `any` stops at the first true (absorbing) and the generator is lazy,
        # so a later Innie is genuinely never read. `ANY OF []` -> False.
        return any(pred(self.value(innie_id)) for innie_id in innie_ids)

    def all_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        # `all` stops at the first false (absorbing). `ALL OF []` -> True.
        return all(pred(self.value(innie_id)) for innie_id in innie_ids)


@pytest.fixture
def dict_resolver() -> type[DictResolver]:
    return DictResolver


@pytest.fixture
def aggressive_switching() -> Iterator[None]:
    """Preempt threads roughly every bytecode, for the duration of one test.

    The default 5ms interval lets a short workday run start to finish without
    a single switch, so repetition alone explores very few interleavings. A
    microsecond interval explores a great many. Restored in `finally`, because
    leaving it set would slow every test that runs afterwards.
    """
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(original)
