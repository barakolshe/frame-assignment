"""Random work schedules for the determinism fuzz test.

The generator's one job is to produce *interesting* schedules cheaply, not
realistic ones. Interesting here means: every language feature appears, the
dependency shapes vary, and the three knobs below turn on the exact hazards
the two runners could disagree about.

References only ever point at Innies defined *earlier*, so the base corpus is
a DAG and every result is well-defined by construction. `allow_cycles` then
injects back-edges, which is where the interesting cases live: a cycle's
membership is where a demand-driven walk and a quiescent wait-for graph could
each reach a different -- but individually plausible -- answer (S9).

Every knob is explicit at the call site. A corpus whose shape is decided by a
default is a corpus nobody can tell you the contents of.
"""

from __future__ import annotations

import random

_ARITHMETIC = ("ADD", "MULTIPLY", "MODULO")
_COMPARISONS = (">", "<", ">=", "<=", "==", "!=")


def generate(seed: int, *, n: int, allow_cycles: bool, allow_faults: bool) -> dict[str, object]:
    """One random work schedule, in the loader's input shape.

    * `allow_cycles` -- append a back-edge to a later Innie (or to itself,
      which S9 makes a legal cycle of length 1) with probability 0.15.
    * `allow_faults` -- let `MODULO 0` and Innies that never WAFFLE appear, so
      the corpus also covers fault propagation (S10) and an absorbing branch
      beating a faulted one inside a quantifier (S8a).

    Deterministic in `seed`: same arguments, same schedule, always.
    """
    rng = random.Random(seed)
    ids = [f"I{index}" for index in range(n)]
    innies: list[dict[str, str]] = []

    for index, innie_id in enumerate(ids):
        earlier = ids[:index]
        lines = [f"LOAD {rng.randint(-50, 50)}"]
        for _ in range(rng.randint(0, 4)):
            lines.extend(_body_line(rng, earlier, allow_faults=allow_faults))

        if allow_cycles and index > 0 and rng.random() < 0.15:
            # Self or later: the back-edge that turns the DAG into a graph.
            lines.append(f"ADD {rng.choice(ids[index:])}")

        # S2: an Innie with no WAFFLE publishes VOID, and its readers fault.
        if not (allow_faults and rng.random() < 0.05):
            lines.append("WAFFLE")

        innies.append({"id": innie_id, "schedule": "\n".join(lines)})

    return {"innies": innies}


def _body_line(rng: random.Random, earlier: list[str], *, allow_faults: bool) -> list[str]:
    """One instruction (or a whole SHIFT block) drawn from the weighted menu.

    Every branch that needs a reference needs `earlier` to be non-empty, so
    the first Innie of a schedule always lands in the arithmetic case.
    """
    choice = rng.random()

    if choice < 0.30 or not earlier:
        operand = rng.randint(1, 20)
        if allow_faults and rng.random() < 0.04:
            operand = 0  # MODULO 0 faults (S4); ADD 0 / MULTIPLY 0 do not
        return [f"{rng.choice(_ARITHMETIC)} {operand}"]

    if choice < 0.50:
        return [f"ADD {rng.choice(earlier)}"]

    if choice < 0.65:
        return [f"ADD [{_ref_list(rng, earlier)}]"]

    if choice < 0.78:
        quantifier = rng.choice(("ANY", "ALL"))
        return [
            f"WELLNESS_CHECK {rng.choice(earlier)} {rng.choice(_COMPARISONS)} "
            f"{quantifier} OF [{_ref_list(rng, earlier)}]"
        ]

    if choice < 0.90:
        return [
            f"CONDITIONAL_ADD [{_ref_list(rng, earlier)}] "
            f"IF {rng.choice(earlier)} > {rng.randint(-50, 50)}"
        ]

    return [
        f"SHIFT {rng.randint(0, 3)} TIMES",
        f"ADD {rng.choice(earlier)}",
        "END_SHIFT",
    ]


def _ref_list(rng: random.Random, earlier: list[str]) -> str:
    """One to three distinct earlier Innies, comma separated."""
    picks = rng.sample(earlier, min(len(earlier), rng.randint(1, 3)))
    return ", ".join(picks)
