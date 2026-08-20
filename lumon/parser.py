"""Schedule text -> Program. All validation that can happen without the
full Innie list happens here, with line numbers. Checking that a reference
names a real Innie needs that list, so it is the loader's job."""

from __future__ import annotations

import re
from collections.abc import Callable

from lumon.errors import ParseError
from lumon.isa import (
    Add,
    AnyInstruction,
    CmpOp,
    Comparand,
    Condition,
    ConditionalAdd,
    Const,
    Load,
    Modulo,
    Multiply,
    Operand,
    Program,
    Quantifier,
    Ref,
    RefList,
    Shift,
    Waffle,
    WellnessCheck,
)

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INT_RE = re.compile(r"^-?\d+$")


def parse_operand(token: str, line: int) -> Operand:
    """Const | Ref | RefList.

    An empty `[]` is rejected in every position. The spec only ever lists
    non-empty groups of Innies, so `ADD []` -- and `ANY OF []` -- is a typo,
    not a request for a vacuous quantifier.
    """
    token = token.strip()
    if not token:
        raise ParseError("missing operand", line)

    if token.startswith("["):
        if not token.endswith("]"):
            raise ParseError(f"unclosed '[' in operand {token!r}", line)
        inner = token[1:-1].strip()
        if not inner:
            raise ParseError("empty reference list", line)
        ids = [part.strip() for part in inner.split(",")]
        for part in ids:
            if not _ID_RE.match(part):
                raise ParseError(f"bad Innie ID {part!r}", line)
        return RefList(innie_ids=tuple(ids))

    if token.endswith("]"):
        raise ParseError(f"unmatched ']' in operand {token!r}", line)

    if _INT_RE.match(token):
        return Const(value=int(token))

    if _ID_RE.match(token):
        return Ref(innie_id=token)

    raise ParseError(f"bad operand {token!r}", line)


def strip_line(raw: str) -> str:
    """Remove comments and surrounding whitespace."""
    return raw.split("#", 1)[0].strip()


def split_opcode(text: str) -> tuple[str, str]:
    """Split a stripped line into (UPPERCASED opcode, rest).

    Opcodes are case-insensitive; Innie IDs in `rest` keep their case.
    """
    # `text` is a stripped, non-blank line; splitting on any whitespace run
    # means a tab between opcode and operand parses like a space.
    parts = text.split(maxsplit=1)
    return parts[0].upper(), parts[1].strip() if len(parts) == 2 else ""


# OPCODES is the extension point: a new work order is a new entry here and
# a new handler, never an edit to a growing if-chain (OCP).
ParseFn = Callable[[str, int], AnyInstruction]
OPCODES: dict[str, ParseFn] = {}


def opcode(name: str) -> Callable[[ParseFn], ParseFn]:
    def register(fn: ParseFn) -> ParseFn:
        OPCODES[name] = fn
        return fn

    return register


# The union of concrete classes, not `type[Instruction]`: the base class has
# no `operand` field, and a `Program` holds AnyInstruction, never Instruction.
OperandNode = type[Load] | type[Add] | type[Multiply] | type[Modulo]


def _arithmetic(node: OperandNode, name: str) -> ParseFn:
    def parse_fn(rest: str, line: int) -> AnyInstruction:
        if not rest:
            raise ParseError(f"{name} requires an operand", line)
        return node(operand=parse_operand(rest, line), line=line)

    return parse_fn


for _name, _node in (
    ("LOAD", Load),
    ("ADD", Add),
    ("MULTIPLY", Multiply),
    ("MODULO", Modulo),
):
    OPCODES[_name] = _arithmetic(_node, _name)


@opcode("WAFFLE")
def _parse_waffle(rest: str, line: int) -> AnyInstruction:
    if rest:
        raise ParseError("WAFFLE takes no operand", line)
    return Waffle(line=line)


# -------------------------------------------------------------- conditions

# Two-character operators must be tried first, or ">=" parses as ">".
_OPERATORS = ("<=", ">=", "==", "!=", "<", ">")

_QUANTIFIERS = {"ANY OF": Quantifier.ANY, "ALL OF": Quantifier.ALL}


def _parse_comparand(token: str, line: int) -> Comparand:
    operand = parse_operand(token, line)
    if isinstance(operand, RefList):
        raise ParseError("a reference list cannot appear on this side of a comparison", line)
    return operand


def parse_condition(text: str, line: int) -> Condition:
    """The condition grammar: `<comparand> OP <comparand> | ANY OF [...] | ALL OF [...]`."""
    text = text.strip()
    for symbol in _OPERATORS:
        idx = text.find(symbol)
        if idx == -1:
            continue
        lhs_text, rhs_text = text[:idx], text[idx + len(symbol) :]
        lhs = _parse_comparand(lhs_text, line)
        rhs_text = rhs_text.strip()

        for keyword, quant in _QUANTIFIERS.items():
            if not rhs_text.upper().startswith(keyword):
                continue
            list_text = rhs_text[len(keyword) :].strip()
            if not list_text.startswith("["):
                raise ParseError(f"{keyword} must be followed by a [list]", line)
            rhs = parse_operand(list_text, line)
            if not isinstance(rhs, RefList):
                raise ParseError(f"{keyword} requires a [list]", line)
            return Condition(lhs=lhs, op=CmpOp(symbol), quantifier=quant, rhs=rhs)

        return Condition(
            lhs=lhs,
            op=CmpOp(symbol),
            quantifier=Quantifier.NONE,
            rhs=_parse_comparand(rhs_text, line),
        )

    raise ParseError(f"no comparison operator in condition {text!r}", line)


@opcode("WELLNESS_CHECK")
def _parse_wellness_check(rest: str, line: int) -> AnyInstruction:
    condition = parse_condition(rest, line) if rest else None
    return WellnessCheck(condition=condition, line=line)


@opcode("CONDITIONAL_ADD")
def _parse_conditional_add(rest: str, line: int) -> AnyInstruction:
    # Split on the LAST " IF ". Matching uppercase-only is deliberate: `if`
    # is a legal Innie ID, so a case-insensitive split would mis-handle
    # `CONDITIONAL_ADD [A] IF if > 5`.
    head, sep, cond_text = rest.rpartition(" IF ")
    if not sep:
        raise ParseError("CONDITIONAL_ADD requires an IF <condition>", line)
    targets = parse_operand(head.strip(), line)
    if not isinstance(targets, RefList):
        raise ParseError("CONDITIONAL_ADD requires a [list] of Innies", line)
    return ConditionalAdd(
        targets=targets,
        condition=parse_condition(cond_text, line),
        line=line,
    )


def parse_simple(opcode_name: str, rest: str, line: int) -> AnyInstruction:
    """Dispatch one non-block instruction. SHIFT/END_SHIFT are handled by
    the block parser, so they never reach here."""
    try:
        parse_fn = OPCODES[opcode_name]
    except KeyError:
        raise ParseError(f"unknown work order {opcode_name!r}", line) from None
    return parse_fn(rest, line)


# ------------------------------------------------------------ SHIFT blocks

_SHIFT_RE = re.compile(r"^(-?\d+)\s+TIMES$", re.IGNORECASE)


def _parse_block(
    lines: list[tuple[int, str]], pos: int, opened_at: int | None
) -> tuple[Program, int]:
    """Parse instructions until END_SHIFT (or EOF at the top level).

    `opened_at` is the line of the SHIFT that opened this block, or None at
    the top level. Returns (program, next_position). Recursion makes nesting
    free and there are no jump targets to get wrong.
    """
    out: list[AnyInstruction] = []
    while pos < len(lines):
        lineno, text = lines[pos]
        opcode_name, rest = split_opcode(text)

        if opcode_name == "END_SHIFT":
            if rest:
                raise ParseError("END_SHIFT takes no operand", lineno)
            if opened_at is None:
                raise ParseError("END_SHIFT without a matching SHIFT", lineno)
            return tuple(out), pos + 1

        if opcode_name == "SHIFT":
            match = _SHIFT_RE.match(rest)
            if not match:
                raise ParseError("SHIFT must read 'SHIFT <number> TIMES'", lineno)
            times = int(match.group(1))
            if times < 0:
                raise ParseError(f"SHIFT count must not be negative (got {times})", lineno)
            body, pos = _parse_block(lines, pos + 1, opened_at=lineno)
            out.append(Shift(times=times, body=body, line=lineno))
            continue

        out.append(parse_simple(opcode_name, rest, lineno))
        pos += 1

    if opened_at is not None:
        raise ParseError("SHIFT is never closed by END_SHIFT", opened_at)
    return tuple(out), pos


def parse(text: str) -> Program:
    """Parse a full schedule into a (possibly nested) Program."""
    lines = [
        (lineno, stripped)
        for lineno, raw in enumerate(text.splitlines(), start=1)
        if (stripped := strip_line(raw))
    ]
    program, _ = _parse_block(lines, 0, opened_at=None)
    return program
