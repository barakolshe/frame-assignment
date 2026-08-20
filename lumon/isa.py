"""Instruction set: frozen pydantic models the parser builds and interp walks.

Data only -- no execution behaviour lives here. Execution is a dispatch
table in `lumon/interp/handlers.py`, which keeps the ISA usable by the
parser, the loader's validation pass, and the interpreter without any of
them depending on each other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class _Node(BaseModel):
    """Shared config for every ISA node: immutable and hashable, so nodes
    compare by value and can live in sets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def refs(self) -> tuple[str, ...]:
        """Every Innie ID this node mentions. Overridden where relevant."""
        return ()


class CmpOp(StrEnum):
    GT = ">"
    LT = "<"
    GE = ">="
    LE = "<="
    EQ = "=="
    NE = "!="

    def apply(self, left: int, right: int) -> bool:
        return {
            CmpOp.GT: left > right,
            CmpOp.LT: left < right,
            CmpOp.GE: left >= right,
            CmpOp.LE: left <= right,
            CmpOp.EQ: left == right,
            CmpOp.NE: left != right,
        }[self]


class Quantifier(StrEnum):
    NONE = "NONE"  # plain comparison against a single operand
    ANY = "ANY"  # ANY OF [...]
    ALL = "ALL"  # ALL OF [...]


# ---------------------------------------------------------------- operands


class Const(_Node):
    value: int


class Ref(_Node):
    innie_id: str

    def refs(self) -> tuple[str, ...]:
        return (self.innie_id,)


class RefList(_Node):
    # Deliberately unconstrained: `ADD []` is a *parse* error, but an empty
    # list is legal here -- S7's vacuous quantifiers (ANY OF [] is false,
    # ALL OF [] is true) are evaluated over exactly this node.
    innie_ids: tuple[str, ...]

    def refs(self) -> tuple[str, ...]:
        return self.innie_ids


Operand = Const | Ref | RefList  # ADD / MULTIPLY / MODULO argument
Comparand = Const | Ref  # condition LHS, or non-quantified RHS


# --------------------------------------------------------------- condition


class Condition(_Node):
    lhs: Comparand
    op: CmpOp
    quantifier: Quantifier
    rhs: Const | Ref | RefList

    def refs(self) -> tuple[str, ...]:
        return self.lhs.refs() + self.rhs.refs()


# ------------------------------------------------------------ instructions


class Instruction(_Node):
    """Root of the instruction hierarchy.

    NEVER annotate a field with this type -- see AnyInstruction below.
    """

    # `Field(ge=1)` is a constraint, not a default: a FieldInfo without
    # `default=` leaves the field required. Same for `Shift.times` below.
    line: int = Field(ge=1)


class Load(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Add(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Multiply(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Modulo(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Waffle(Instruction):
    pass


class WellnessCheck(Instruction):
    # No default: callers pass `condition=None` explicitly for the bare
    # `WELLNESS_CHECK` form, so a forgotten condition is a ValidationError
    # rather than a silently unconditional reset.
    condition: Condition | None

    def refs(self) -> tuple[str, ...]:
        return self.condition.refs() if self.condition else ()


class ConditionalAdd(Instruction):
    targets: RefList
    condition: Condition

    def refs(self) -> tuple[str, ...]:
        return self.targets.refs() + self.condition.refs()


class Shift(Instruction):
    times: int = Field(ge=0)
    # MUST be the union, not `tuple[Instruction, ...]`. Annotating with the
    # base class makes pydantic serialize every child AS Instruction, dropping
    # `operand`, `condition`, and nested bodies. Pinned by a test.
    body: tuple[AnyInstruction, ...]

    def refs(self) -> tuple[str, ...]:
        out: tuple[str, ...] = ()
        for instr in self.body:
            out += instr.refs()
        return out


AnyInstruction = Annotated[
    Load | Add | Multiply | Modulo | Waffle | WellnessCheck | ConditionalAdd | Shift,
    Field(union_mode="left_to_right"),
]

Shift.model_rebuild()  # resolves the forward reference in `body`

Program = tuple[AnyInstruction, ...]


def all_refs(program: Program) -> tuple[str, ...]:
    """Every Innie ID mentioned anywhere in a program, including inside
    shifts and conditions. Used only for load-time validation (S11) --
    never for dependency ordering, which is dynamic (S6)."""
    out: tuple[str, ...] = ()
    for instr in program:
        out += instr.refs()
    return out
