"""Black-box acceptance tests derived only from ``Hometask Backend/excersice.txt``.

The author of this module deliberately did not read ``lumon/``, the other tests, the
implementation plan or the README. Every expectation below is traced back to a quoted
line of the brief, and every sample schedule's expected numbers were hand-derived and
written down before the program was run even once.

The program is driven the way a grader would drive it -- as a subprocess:
``sys.executable -m lumon <schedule.json>``. Nothing from ``lumon`` is imported.

Where the brief is silent -- an Innie with no ``WAFFLE`` at all, references to unknown
Innies, comparison operators other than ``>`` and ``<=``, the ordering of the output
list -- no assertion is made. A test that pins down unstated behaviour would only be a
second opinion with nothing behind it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "Hometask Backend"

# A hang guard, not a timing assertion: "Ensure all Innies eventually clock out without
# hanging". Nothing here asserts on how long anything took.
TIMEOUT_SECONDS = 60

# How many times a schedule is re-run when checking "work schedules are always
# deterministic, even if there is a deadlock".
DETERMINISM_RUNS = 5


# --------------------------------------------------------------------------------------
# Driving the program
# --------------------------------------------------------------------------------------


def run_cli(schedule_path: Path) -> dict[str, Any]:
    """Run the scheduler on ``schedule_path`` and return its parsed JSON output."""
    proc = subprocess.run(
        [sys.executable, "-m", "lumon", str(schedule_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    assert proc.returncode == 0, (
        f"lumon exited {proc.returncode} for {schedule_path.name}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    payload: dict[str, Any] = json.loads(proc.stdout)
    return payload


def results_of(schedule_path: Path) -> dict[str, Any]:
    """Return ``{innie id: work product}`` for a schedule.

    The brief says nothing about the order in which finished Innies are reported, so
    this collapses the output to a mapping rather than asserting on a sequence.
    """
    payload = run_cli(schedule_path)
    innies = payload["innies"]
    ids = [entry["id"] for entry in innies]
    assert len(ids) == len(set(ids)), f"duplicate ids in output: {ids}"
    return {entry["id"]: entry["result"] for entry in innies}


def write_schedule(tmp_path: Path, innies: dict[str, str]) -> Path:
    """Write a work schedule in the brief's Work Schedule Format and return its path."""
    payload = {"innies": [{"id": name, "schedule": text} for name, text in innies.items()]}
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# The sample schedules -- the headline acceptance cases
# --------------------------------------------------------------------------------------

# Hand-derived from the brief before running anything. The working is in the PR body.
#
# 1.json
#   HELLY   LOAD 10; SHIFT 2 TIMES {ADD 5}          -> 10+5+5                       = 20
#   MARK    LOAD 2; ADD 3                                                           = 5
#   IRVING  LOAD 0; ADD [HELLY, MARK]               -> 0+20+5                       = 25
#   BURT    LOAD 100; WELLNESS_CHECK IRVING > ALL OF [HELLY, MARK]
#           -> 25>20 and 25>5, condition true, "void your work output (reset to 0)"  = 0
#
# 2.json
#   DYLAN   LOAD 1; WAFFLE; LOAD 100; WAFFLE
#           -> two work products; "waiting Innies receive only the final product"    = 100
#   BURT    LOAD 0; ADD [DYLAN] -> 100; WAFFLE; ADD [DYLAN] -> 200; WAFFLE           = 200
#           BURT's second ADD sees the same 100: a work product is visible only once
#           its author "completes all their work", and DYLAN's latest product is 100.
#
# 3.json
#   HELLY   LOAD 1; SHIFT 3 TIMES {MULTIPLY 2; WAFFLE} -> products 2, 4, 8; latest   = 8
#   MARK    LOAD 0; SHIFT 2 TIMES {ADD HELLY}          -> 0+8+8                      = 16
#   IRVING  LOAD 100; WELLNESS_CHECK HELLY > ANY OF [MARK] -> 8>16 false, no void;
#           CONDITIONAL_ADD [HELLY, MARK] IF MARK > 10 -> 16>10 true -> 100+8+16     = 124
#   BURT    LOAD 1; SHIFT 4 TIMES {ADD IRVING; WAFFLE} -> 125, 249, 373, 497; latest = 497
#   DYLAN   LOAD 0; ADD [HELLY, MARK, IRVING, BURT] -> 645; MULTIPLY 2 -> 1290;
#           WELLNESS_CHECK BURT > ALL OF [HELLY, MARK, IRVING] -> all true -> void   = 0
EXPECTED_SAMPLES: dict[str, dict[str, Any]] = {
    "1.json": {"HELLY": 20, "MARK": 5, "IRVING": 25, "BURT": 0},
    "2.json": {"DYLAN": 100, "BURT": 200},
    "3.json": {"HELLY": 8, "MARK": 16, "IRVING": 124, "BURT": 497, "DYLAN": 0},
}

SAMPLE_PATHS = sorted(SAMPLES_DIR.glob("*.json"))


def test_sample_schedules_were_discovered() -> None:
    """The glob below drives the headline cases; an empty glob must not pass vacuously."""
    assert SAMPLE_PATHS, f"no *.json work schedules found in {SAMPLES_DIR}"


def test_expected_table_has_no_stale_entries() -> None:
    """Every filename in the table must still exist on disk."""
    present = {path.name for path in SAMPLE_PATHS}
    stale = sorted(set(EXPECTED_SAMPLES) - present)
    assert not stale, f"EXPECTED_SAMPLES names files that no longer exist: {stale}"


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda path: path.name)
def test_sample_schedule(sample: Path) -> None:
    """Run every schedule shipped with the brief and assert its full output."""
    if sample.name not in EXPECTED_SAMPLES:
        pytest.fail(
            f"{sample.name} is a sample work schedule with no hand-derived expectation in "
            f"EXPECTED_SAMPLES. Derive its results from the brief and add them -- a new "
            f"sample must never pass without being checked."
        )
    assert results_of(sample) == EXPECTED_SAMPLES[sample.name]


# --------------------------------------------------------------------------------------
# One case per rule the brief states
# --------------------------------------------------------------------------------------

# Each entry is (case id, innies, expected work products). The case id names the rule.
RULE_CASES: list[tuple[str, dict[str, str], dict[str, Any]]] = [
    (
        # "LOAD <value>", "ADD <value>", "MULTIPLY <value>",
        # "MODULO <value> - makes your current work output be the modulo of your
        #  current work output % value"
        "basic-arithmetic",
        {"HELLY": "LOAD 7\nADD 3\nMULTIPLY 4\nMODULO 7\nWAFFLE"},
        {"HELLY": 5},
    ),
    (
        # The brief's own Work Schedule Format example uses a bare reference:
        # "LOAD 5\nADD HELLY\nWAFFLE".
        "bare-reference-add",
        {"HELLY": "LOAD 4\nWAFFLE", "MARK": "LOAD 1\nADD HELLY\nWAFFLE"},
        {"HELLY": 4, "MARK": 5},
    ),
    (
        # "ADD [HELLY, MARK, IRVING] - Wait for all referenced Innies to finalize,
        #  then add their work to yours"
        "add-reference-list",
        {
            "HELLY": "LOAD 2\nWAFFLE",
            "MARK": "LOAD 3\nWAFFLE",
            "IRVING": "LOAD 1\nADD [HELLY, MARK]\nWAFFLE",
        },
        {"HELLY": 2, "MARK": 3, "IRVING": 6},
    ),
    (
        # "MULTIPLY [HELLY, MARK] - Similar to ADD but multiplies", read as multiplying
        # by each referenced Innie's work in turn: 1 * 2 * 3.
        "multiply-reference-list",
        {
            "HELLY": "LOAD 2\nWAFFLE",
            "MARK": "LOAD 3\nWAFFLE",
            "BURT": "LOAD 1\nMULTIPLY [HELLY, MARK]\nWAFFLE",
        },
        {"HELLY": 2, "MARK": 3, "BURT": 6},
    ),
    (
        # "If the condition is true, void your work output (reset to 0)."
        "wellness-check-true-voids",
        {"HELLY": "LOAD 10\nWAFFLE", "MARK": "LOAD 50\nWELLNESS_CHECK HELLY > 5\nWAFFLE"},
        {"HELLY": 10, "MARK": 0},
    ),
    (
        # The converse: a false condition leaves the work output alone.
        "wellness-check-false-keeps",
        {"HELLY": "LOAD 1\nWAFFLE", "MARK": "LOAD 50\nWELLNESS_CHECK HELLY > 5\nWAFFLE"},
        {"HELLY": 1, "MARK": 50},
    ),
    (
        # "Simple comparison: HELLY > 5 , MARK <= 10" -- the second operator the brief shows.
        "comparison-lte",
        {"HELLY": "LOAD 5\nWAFFLE", "MARK": "LOAD 9\nWELLNESS_CHECK HELLY <= 5\nWAFFLE"},
        {"HELLY": 5, "MARK": 0},
    ),
    (
        # "CONDITIONAL_ADD [list] IF <condition> - If the condition is true, wait for all
        #  Innies in [list] to finalize and add their work."
        "conditional-add-true",
        {
            "HELLY": "LOAD 4\nWAFFLE",
            "MARK": "LOAD 6\nWAFFLE",
            "IRVING": "LOAD 0\nCONDITIONAL_ADD [HELLY, MARK] IF HELLY > 3\nWAFFLE",
        },
        {"HELLY": 4, "MARK": 6, "IRVING": 10},
    ),
    (
        # "If the condition is false, skip the ADD."
        "conditional-add-false",
        {
            "HELLY": "LOAD 4\nWAFFLE",
            "MARK": "LOAD 6\nWAFFLE",
            "IRVING": "LOAD 0\nCONDITIONAL_ADD [HELLY, MARK] IF HELLY > 100\nWAFFLE",
        },
        {"HELLY": 4, "MARK": 6, "IRVING": 0},
    ),
    (
        # "Innies automatically wait for dependencies but only when needed."
        # PETEY's CONDITIONAL_ADD names GEMMA, and GEMMA waits on PETEY -- but the
        # condition is false, so PETEY never waits and no circular dependency exists.
        "dependencies-waited-on-only-when-needed",
        {
            "MILCHICK": "LOAD 2\nWAFFLE",
            "PETEY": "LOAD 5\nCONDITIONAL_ADD [GEMMA] IF MILCHICK > 100\nWAFFLE",
            "GEMMA": "LOAD 1\nADD PETEY\nWAFFLE",
        },
        {"MILCHICK": 2, "PETEY": 5, "GEMMA": 6},
    ),
    (
        # "ANY OF: HELLY > ANY OF [MARK, IRVING] - Unblock and evaluate as soon as ONE
        #  Innie's condition is met." 10 > 100 is false, 10 > 3 is true, so ANY holds.
        "any-of-true",
        {
            "HELLY": "LOAD 10\nWAFFLE",
            "MARK": "LOAD 100\nWAFFLE",
            "IRVING": "LOAD 3\nWAFFLE",
            "BURT": "LOAD 50\nWELLNESS_CHECK HELLY > ANY OF [MARK, IRVING]\nWAFFLE",
        },
        {"HELLY": 10, "MARK": 100, "IRVING": 3, "BURT": 0},
    ),
    (
        # No Innie in the list satisfies the comparison, so ANY OF is false.
        "any-of-false",
        {
            "HELLY": "LOAD 10\nWAFFLE",
            "MARK": "LOAD 100\nWAFFLE",
            "BURT": "LOAD 50\nWELLNESS_CHECK HELLY > ANY OF [MARK]\nWAFFLE",
        },
        {"HELLY": 10, "MARK": 100, "BURT": 50},
    ),
    (
        # "ALL OF: HELLY > ALL OF [MARK, IRVING] - Unblock and evaluate when ALL Innies'
        #  conditions are met". 10 > 3 and 10 > 1.
        "all-of-true",
        {
            "HELLY": "LOAD 10\nWAFFLE",
            "MARK": "LOAD 3\nWAFFLE",
            "IRVING": "LOAD 1\nWAFFLE",
            "BURT": "LOAD 50\nWELLNESS_CHECK HELLY > ALL OF [MARK, IRVING]\nWAFFLE",
        },
        {"HELLY": 10, "MARK": 3, "IRVING": 1, "BURT": 0},
    ),
    (
        # "...or short-circuit if one makes the statement false." 10 > 100 is false.
        "all-of-false",
        {
            "HELLY": "LOAD 10\nWAFFLE",
            "MARK": "LOAD 100\nWAFFLE",
            "IRVING": "LOAD 1\nWAFFLE",
            "BURT": "LOAD 50\nWELLNESS_CHECK HELLY > ALL OF [MARK, IRVING]\nWAFFLE",
        },
        {"HELLY": 10, "MARK": 100, "IRVING": 1, "BURT": 50},
    ),
    (
        # "If an Innie WAFFLEs multiple times, waiting Innies receive only the final
        #  product" -- and "no Innie should see the work of another until it's completely
        #  finished", which is what makes 3 the only answer MARK can get.
        "latest-work-product-wins",
        {
            "HELLY": "LOAD 1\nWAFFLE\nLOAD 2\nWAFFLE\nLOAD 3\nWAFFLE",
            "MARK": "LOAD 0\nADD HELLY\nWAFFLE",
        },
        {"HELLY": 3, "MARK": 3},
    ),
    (
        # "SHIFT <number> TIMES ... END_SHIFT - Repeat work instructions atomically."
        "shift-repeats",
        {"HELLY": "LOAD 0\nSHIFT 3 TIMES\nADD 2\nEND_SHIFT\nWAFFLE"},
        {"HELLY": 6},
    ),
    (
        # "Supporting nested work shifts with dynamic dependencies."
        # Each outer pass adds 1 three times and then 10, so (3 + 10) * 2.
        "nested-shifts",
        {
            "HELLY": (
                "LOAD 0\nSHIFT 2 TIMES\nSHIFT 3 TIMES\nADD 1\nEND_SHIFT\n"
                "ADD 10\nEND_SHIFT\nWAFFLE"
            )
        },
        {"HELLY": 26},
    ),
    (
        # A shift whose body waits on another Innie -- "dynamic dependencies".
        "shift-body-with-dependency",
        {
            "HELLY": "LOAD 4\nWAFFLE",
            "MARK": "LOAD 0\nSHIFT 3 TIMES\nADD HELLY\nEND_SHIFT\nWAFFLE",
        },
        {"HELLY": 4, "MARK": 12},
    ),
]


@pytest.mark.parametrize(
    ("innies", "expected"),
    [pytest.param(innies, expected, id=case_id) for case_id, innies, expected in RULE_CASES],
)
def test_stated_rule(tmp_path: Path, innies: dict[str, str], expected: dict[str, Any]) -> None:
    assert results_of(write_schedule(tmp_path, innies)) == expected


# --------------------------------------------------------------------------------------
# Circular dependencies
# --------------------------------------------------------------------------------------

# "Innies may create a circular dependency, in order to avoid this inefficiency and not to
#  waste time, when detected a circular dependency between innies, they will WAFFLE to the
#  value of -1."
DEADLOCK_VALUE = -1

DEADLOCK_CASES: list[tuple[str, dict[str, str], dict[str, Any]]] = [
    (
        "two-innie-cycle",
        {"HELLY": "LOAD 1\nADD MARK\nWAFFLE", "MARK": "LOAD 1\nADD HELLY\nWAFFLE"},
        {"HELLY": DEADLOCK_VALUE, "MARK": DEADLOCK_VALUE},
    ),
    (
        "three-innie-cycle",
        {
            "HELLY": "LOAD 1\nADD MARK\nWAFFLE",
            "MARK": "LOAD 1\nADD IRVING\nWAFFLE",
            "IRVING": "LOAD 1\nADD HELLY\nWAFFLE",
        },
        {"HELLY": DEADLOCK_VALUE, "MARK": DEADLOCK_VALUE, "IRVING": DEADLOCK_VALUE},
    ),
    (
        # A cycle formed through a reference list rather than a bare reference.
        "cycle-through-reference-list",
        {
            "HELLY": "LOAD 1\nADD [MARK]\nWAFFLE",
            "MARK": "LOAD 1\nMULTIPLY [HELLY]\nWAFFLE",
        },
        {"HELLY": DEADLOCK_VALUE, "MARK": DEADLOCK_VALUE},
    ),
    (
        # "Ensure no deadlocks or resource starvation (Innies must eventually clock out)":
        # an Innie with no part in the cycle finishes its own workday normally.
        "innie-outside-the-cycle-is-unaffected",
        {
            "HELLY": "LOAD 1\nADD MARK\nWAFFLE",
            "MARK": "LOAD 1\nADD HELLY\nWAFFLE",
            "DYLAN": "LOAD 7\nWAFFLE",
        },
        {"HELLY": DEADLOCK_VALUE, "MARK": DEADLOCK_VALUE, "DYLAN": 7},
    ),
    (
        # The deadlocked Innies "WAFFLE to the value of -1", and a WAFFLE is what makes a
        # work product "visible to other Innies" -- so a waiter outside the cycle adds -1.
        "waiter-on-a-cycle-receives-the-waffled-minus-one",
        {
            "HELLY": "LOAD 1\nADD MARK\nWAFFLE",
            "MARK": "LOAD 1\nADD HELLY\nWAFFLE",
            "IRVING": "LOAD 10\nADD HELLY\nWAFFLE",
        },
        {"HELLY": DEADLOCK_VALUE, "MARK": DEADLOCK_VALUE, "IRVING": 9},
    ),
    (
        # A cycle of length one. The brief says "a circular dependency between innies";
        # an Innie waiting on its own unfinished work is read here as the same situation.
        "self-referential-cycle",
        {"HELLY": "LOAD 1\nADD HELLY\nWAFFLE"},
        {"HELLY": DEADLOCK_VALUE},
    ),
]


@pytest.mark.parametrize(
    ("innies", "expected"),
    [pytest.param(innies, expected, id=case_id) for case_id, innies, expected in DEADLOCK_CASES],
)
def test_circular_dependency(
    tmp_path: Path, innies: dict[str, str], expected: dict[str, Any]
) -> None:
    assert results_of(write_schedule(tmp_path, innies)) == expected


# --------------------------------------------------------------------------------------
# Short-circuit evaluation
# --------------------------------------------------------------------------------------

# Requirement 4: "ANY OF and ALL OF should evaluate efficiently, unblocking as soon as the
# result is determined."
#
# Short-circuiting is normally invisible from outside, because an evaluator that stubbornly
# waits for every Innie in the list still reaches the same number. The one place it becomes
# observable in the results is when the operand that need not be consulted would have
# dragged the evaluating Innie into a circular dependency. In both schedules below the
# deciding Innie has no dependencies of its own, so its value is available immediately and
# the outcome is determined without ever consulting PETEY -- whichever order the list is
# walked in.


def test_any_of_short_circuits_before_forming_a_cycle(tmp_path: Path) -> None:
    """Spec: "Unblock and evaluate as soon as ONE Innie's condition is met."

    HELLY (10) > MARK (1) already settles ANY OF, so GEMMA never waits on PETEY and the
    GEMMA -> PETEY -> GEMMA cycle never forms.
    """
    innies = {
        "HELLY": "LOAD 10\nWAFFLE",
        "MARK": "LOAD 1\nWAFFLE",
        "GEMMA": "LOAD 50\nWELLNESS_CHECK HELLY > ANY OF [MARK, PETEY]\nWAFFLE",
        "PETEY": "LOAD 3\nADD GEMMA\nWAFFLE",
    }
    # The condition is true, so GEMMA is voided to 0 and PETEY adds that 0 to its 3.
    assert results_of(write_schedule(tmp_path, innies)) == {
        "HELLY": 10,
        "MARK": 1,
        "GEMMA": 0,
        "PETEY": 3,
    }


def test_all_of_short_circuits_before_forming_a_cycle(tmp_path: Path) -> None:
    """Spec: "...or short-circuit if one makes the statement false."

    HELLY (1) > MARK (100) is false, which settles ALL OF, so GEMMA never waits on PETEY.
    """
    innies = {
        "HELLY": "LOAD 1\nWAFFLE",
        "MARK": "LOAD 100\nWAFFLE",
        "GEMMA": "LOAD 50\nWELLNESS_CHECK HELLY > ALL OF [MARK, PETEY]\nWAFFLE",
        "PETEY": "LOAD 3\nADD GEMMA\nWAFFLE",
    }
    # The condition is false, so GEMMA keeps its 50 and PETEY adds it to its 3.
    assert results_of(write_schedule(tmp_path, innies)) == {
        "HELLY": 1,
        "MARK": 100,
        "GEMMA": 50,
        "PETEY": 53,
    }


# --------------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------------

# "Please keep in mind that work schedules are always deterministic, even if there is a
# deadlock", and Requirement 6: "Despite concurrent execution, results should be
# reproducible and consistent (the severed mind works the same way every time)."
#
# Repeating a run is the only black-box handle on this. Nothing here looks at elapsed time.


@pytest.mark.parametrize("sample", SAMPLE_PATHS, ids=lambda path: path.name)
def test_sample_results_are_reproducible(sample: Path) -> None:
    runs = [results_of(sample) for _ in range(DETERMINISM_RUNS)]
    assert all(run == runs[0] for run in runs), f"{sample.name} varied between runs: {runs}"


def test_results_are_reproducible_when_there_is_a_deadlock(tmp_path: Path) -> None:
    """The brief calls out determinism "even if there is a deadlock" specifically."""
    path = write_schedule(
        tmp_path,
        {
            # A cycle, an Innie feeding into the cycle's members, and an independent Innie
            # -- three different ways for a racy implementation to disagree with itself.
            "HELLY": "LOAD 1\nADD MARK\nWAFFLE",
            "MARK": "LOAD 2\nADD IRVING\nWAFFLE",
            "IRVING": "LOAD 3\nADD HELLY\nWAFFLE",
            "BURT": "LOAD 10\nADD [HELLY, MARK, IRVING]\nWAFFLE",
            "DYLAN": "LOAD 7\nMULTIPLY 3\nWAFFLE",
        },
    )
    runs = [results_of(path) for _ in range(DETERMINISM_RUNS)]
    assert all(run == runs[0] for run in runs), f"deadlocking schedule varied: {runs}"
