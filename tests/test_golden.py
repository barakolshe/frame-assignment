"""Acceptance: the three sample schedules against hand-computed values.

Every number in `EXPECTED` was computed by hand from the spec -- never
recorded from a run of this implementation. When a number here
disagrees with the code, re-derive it by hand first: the hand computation is
the authority, and "the test must be wrong" is the failure mode this file
exists to prevent.

None of the three samples contains a cycle, so deadlock detection must leave
every number below untouched when it lands.
"""

from __future__ import annotations

import pathlib

import pytest

from lumon.loader import load_path
from lumon.runners import run_concurrent

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"

EXPECTED: dict[str, dict[str, int]] = {
    # HELLY 10 +5 +5; MARK 2+3; IRVING 0+20+5;
    # BURT 25 > ALL OF [20, 5] -> reset.
    "1.json": {"HELLY": 20, "MARK": 5, "IRVING": 25, "BURT": 0},
    # DYLAN stages 1 then 100 and publishes only the last, so BURT's two
    # reads both see 100 -> 100 then 200.
    "2.json": {"DYLAN": 100, "BURT": 200},
    # HELLY 1*2*2*2; MARK 0+8+8; IRVING 100 (8 > ANY OF [16] is false, no
    # reset) + 8 + 16 since 16 > 10; BURT 1 +124 four times;
    # DYLAN (8+16+124+497)*2 then 497 > ALL OF [8, 16, 124] -> reset.
    "3.json": {"HELLY": 8, "MARK": 16, "IRVING": 124, "BURT": 497, "DYLAN": 0},
}


def values(name: str) -> dict[str, int | None]:
    registry = run_concurrent(load_path(SAMPLES / name))
    return {result.innie_id: result.value for result in registry.snapshot()}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_matches_hand_computed_values(name: str) -> None:
    assert values(name) == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_is_stable_across_repeated_runs(name: str) -> None:
    """Determinism is a hard requirement, not a tendency. Thread interleaving
    changes run to run; the answer must not."""
    expected = EXPECTED[name]
    for _ in range(50):
        assert values(name) == expected
