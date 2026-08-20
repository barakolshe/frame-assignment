"""JSON work schedule -> validated Innies.

This is the trust boundary: the JSON is external input, so pydantic does
the shape validation and the module only has to do the cross-Innie checks
pydantic cannot express (duplicate ids, unknown references).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lumon.errors import ScheduleError, UnknownInnieError
from lumon.isa import AnyInstruction, Program, Shift
from lumon.parser import parse

# ------------------------------------------------------- input models (JSON)


class InnieSpec(BaseModel):
    """One entry of the incoming JSON. Shape only -- the schedule string is
    still raw text at this point."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    schedule: str


class WorkSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    innies: list[InnieSpec] = Field(min_length=1)


# ------------------------------------------------------ parsed domain model


class Innie(BaseModel):
    """An Innie with its schedule parsed. Frozen: nothing mutates a program
    after load."""

    model_config = ConfigDict(frozen=True)

    id: str
    program: Program


def _walk(program: Program) -> Iterator[AnyInstruction]:
    """Yield every instruction, descending into SHIFT bodies."""
    for instr in program:
        yield instr
        if isinstance(instr, Shift):
            yield from _walk(instr.body)


def _validate_refs(innie: Innie, known: set[str]) -> None:
    """S11: every referenced ID must exist. Line numbers survive nesting
    because each Instruction carries its own `line`."""
    for instr in _walk(innie.program):
        if isinstance(instr, Shift):
            # A Shift's refs() is exactly the union of its body's, and the
            # body is walked in its own right below -- checking the Shift
            # itself would blame the SHIFT line for a nested bad reference.
            continue
        for ref in instr.refs():
            if ref not in known:
                raise UnknownInnieError(innie.id, ref, instr.line)


def load(data: object) -> list[Innie]:
    """Validate a work schedule and parse every Innie's program.

    Input order is preserved and is the output order: the CLI reports
    results in the order the schedule listed them.
    """
    try:
        schedule = WorkSchedule.model_validate(data)
    except ValidationError as error:
        raise ScheduleError(f"malformed work schedule: {error}") from error

    innies: list[Innie] = []
    seen: set[str] = set()
    for spec in schedule.innies:
        if spec.id in seen:
            raise ScheduleError(f"duplicate Innie id {spec.id!r}")
        seen.add(spec.id)
        innies.append(Innie(id=spec.id, program=parse(spec.schedule)))

    for innie in innies:
        _validate_refs(innie, seen)
    return innies


def load_path(path: str | Path) -> list[Innie]:
    return load(json.loads(Path(path).read_text(encoding="utf-8")))
