"""Execution behaviour for each instruction type.

Instructions stay inert data in `isa.py`; behaviour lives here. That keeps the
parser's output usable by the loader's validation pass without dragging
execution along with it.

`HANDLERS` is the OCP extension point: a new work order is a new handler
function, never an edit to a growing `isinstance` chain.
"""

import math
from collections.abc import Callable
from typing import cast

from lumon.errors import ArithmeticFault
from lumon.interp.state import State
from lumon.isa import (
    Add,
    Comparand,
    Condition,
    ConditionalAdd,
    Const,
    Instruction,
    Load,
    Modulo,
    Multiply,
    Operand,
    Quantifier,
    Ref,
    RefList,
    Shift,
    Waffle,
    WellnessCheck,
)
from lumon.resolvers.base import Resolver

type Handler = Callable[[Instruction, State, Resolver], None]
type TypedHandler[InstrT: Instruction] = Callable[[InstrT, State, Resolver], None]

HANDLERS: dict[type[Instruction], Handler] = {}


def handles[InstrT: Instruction](
    node: type[InstrT],
) -> Callable[[TypedHandler[InstrT]], TypedHandler[InstrT]]:
    """Register the handler for one instruction type.

    The cast is the one unavoidable piece of unsoundness here: `HANDLERS` is
    keyed by the base class, but each handler accepts its own subclass. `step`
    looks up by exact type, so the narrower parameter is always satisfied.
    """

    def register(fn: TypedHandler[InstrT]) -> TypedHandler[InstrT]:
        HANDLERS[node] = cast(Handler, fn)
        return fn

    return register


# ------------------------------------------------------------- operand help


def operand_values(operand: Operand, resolver: Resolver) -> list[int]:
    """An operand contributes one value (Const/Ref) or many (RefList)."""
    if isinstance(operand, Const):
        return [operand.value]
    if isinstance(operand, Ref):
        return [resolver.value(operand.innie_id)]
    return resolver.values(operand.innie_ids)


def comparand_value(comparand: Comparand, resolver: Resolver) -> int:
    if isinstance(comparand, Const):
        return comparand.value
    return resolver.value(comparand.innie_id)


def evaluate(condition: Condition, resolver: Resolver) -> bool:
    """Short-circuit lives in the resolver, never here.

    The LHS always resolves -- an unconditional wait when it is a `Ref`. The
    quantified forms hand a predicate to the resolver, which is free to stop
    waiting on an absorbing value but not to return a different answer.
    """
    left = comparand_value(condition.lhs, resolver)

    if condition.quantifier is Quantifier.NONE:
        right = comparand_value(cast(Comparand, condition.rhs), resolver)
        return condition.op.apply(left, right)

    rhs = condition.rhs
    if not isinstance(rhs, RefList):
        # A parser invariant; failing loudly beats a silently wrong boolean.
        raise AssertionError(f"{condition.quantifier} OF requires a reference list")

    def pred(right: int) -> bool:
        return condition.op.apply(left, right)

    if condition.quantifier is Quantifier.ANY:
        return resolver.any_of(rhs.innie_ids, pred)  # ANY OF [] -> False
    return resolver.all_of(rhs.innie_ids, pred)  # ALL OF [] -> True


# ---------------------------------------------------------------- handlers


@handles(Load)
def _load(instr: Load, state: State, resolver: Resolver) -> None:
    values = operand_values(instr.operand, resolver)
    state.acc = values[0] if len(values) == 1 else sum(values)


@handles(Add)
def _add(instr: Add, state: State, resolver: Resolver) -> None:
    state.acc += sum(operand_values(instr.operand, resolver))


@handles(Multiply)
def _multiply(instr: Multiply, state: State, resolver: Resolver) -> None:
    state.acc *= math.prod(operand_values(instr.operand, resolver))


@handles(Modulo)
def _modulo(instr: Modulo, state: State, resolver: Resolver) -> None:
    for divisor in operand_values(instr.operand, resolver):
        if divisor == 0:
            raise ArithmeticFault(f"line {instr.line}: MODULO by zero")
        state.acc %= divisor  # Python semantics -- the sign follows the divisor


@handles(Waffle)
def _waffle(instr: Waffle, state: State, resolver: Resolver) -> None:
    # Stage rather than publish: a reader must never see a partial work product.
    state.staged = state.acc


@handles(WellnessCheck)
def _wellness_check(instr: WellnessCheck, state: State, resolver: Resolver) -> None:
    # The bare form resets unconditionally; the conditional form asks first.
    if instr.condition is None or evaluate(instr.condition, resolver):
        state.acc = 0


@handles(ConditionalAdd)
def _conditional_add(instr: ConditionalAdd, state: State, resolver: Resolver) -> None:
    # Evaluate the condition FIRST; if false, never resolve the list. This
    # is what makes the dependency graph dynamic rather than statically known.
    if evaluate(instr.condition, resolver):
        state.acc += sum(resolver.values(instr.targets.innie_ids))


@handles(Shift)
def _shift(instr: Shift, state: State, resolver: Resolver) -> None:
    from lumon.interp.engine import run_block  # local import breaks a cycle

    for _ in range(instr.times):  # `times >= 0` is enforced by the model
        run_block(instr.body, state, resolver)
