"""Executes one Innie's workday. Knows nothing about threads.

Faults raised here -- `ArithmeticFault`, or anything a resolver raises while
waiting -- propagate straight out of `execute`. Settling them into a Cell is
the runner's job; the interpreter has no opinion on failure handling.
"""

from pydantic import BaseModel, ConfigDict

from lumon.interp.handlers import HANDLERS
from lumon.interp.state import State
from lumon.isa import Instruction, Program
from lumon.resolvers.base import Resolver


class Outcome(BaseModel):
    """What an Innie publishes at the end of its workday.

    `staged is None` means the Innie never WAFFLEd, i.e. VOID.
    """

    model_config = ConfigDict(frozen=True)

    staged: int | None = None


def step(instr: Instruction, state: State, resolver: Resolver) -> None:
    """Execute one instruction.

    The lookup is on `type(instr)` exactly, not `isinstance`. That is
    deliberate: a new instruction type without a handler fails loudly on first
    execution instead of silently matching a parent class and doing nothing.
    """
    try:
        handler = HANDLERS[type(instr)]
    except KeyError:
        raise AssertionError(f"no handler registered for {type(instr).__name__}") from None
    handler(instr, state, resolver)


def run_block(program: Program, state: State, resolver: Resolver) -> None:
    """Execute a sequence of instructions against shared state.

    Used for both the top-level program and a `Shift` body, which is why
    nesting costs nothing.
    """
    for instr in program:
        step(instr, state, resolver)


def execute(program: Program, resolver: Resolver) -> Outcome:
    """Run one Innie's whole workday and return what it staged."""
    state = State()
    run_block(program, state, resolver)
    return Outcome(staged=state.staged)
