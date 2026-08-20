"""The interpreter, exercised without a single thread.

Programs are built from ISA nodes rather than parsed from text: `lumon.parser`
is a sibling task, and the interpreter's contract is with `lumon.isa`, not with
the parser. Text -> value coverage arrives with the golden tests (Task 11).
"""

import ast
import pathlib
from collections.abc import Callable, Sequence
from typing import get_args

import pytest

from lumon.errors import ArithmeticFault
from lumon.interp import HANDLERS, Outcome, evaluate, execute, step
from lumon.interp.state import State
from lumon.isa import (
    Add,
    AnyInstruction,
    CmpOp,
    Condition,
    ConditionalAdd,
    Const,
    Instruction,
    Load,
    Modulo,
    Multiply,
    Program,
    Quantifier,
    Ref,
    RefList,
    Shift,
    Waffle,
    WellnessCheck,
)
from lumon.resolvers import Resolver
from tests.conftest import DictResolver


class NoRefsResolver(Resolver):
    """Every reference is a programming error for the tests that use it.

    Programs built from constants must reach the outside world zero times; if
    one of these methods fires, the interpreter resolved something it had no
    business resolving.
    """

    def value(self, innie_id: str) -> int:
        raise AssertionError(f"unexpected ref {innie_id}")

    def values(self, innie_ids: Sequence[str]) -> list[int]:
        raise AssertionError(f"unexpected refs {list(innie_ids)}")

    def any_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        raise AssertionError("unexpected ANY OF")

    def all_of(self, innie_ids: Sequence[str], pred: Callable[[int], bool]) -> bool:
        raise AssertionError("unexpected ALL OF")


def run(program: Program) -> Outcome:
    """Execute a constant-only program: any reference is a test failure."""
    return execute(program, NoRefsResolver())


def run_with(program: Program, values: dict[str, int]) -> tuple[Outcome, DictResolver]:
    resolver = DictResolver(values)
    return execute(program, resolver), resolver


def gt(lhs: Const | Ref, quantifier: Quantifier, rhs: Const | Ref | RefList) -> Condition:
    return Condition(lhs=lhs, op=CmpOp.GT, quantifier=quantifier, rhs=rhs)


# ------------------------------------------------------------- the DIP guard


def test_interp_never_imports_concurrency_or_storage() -> None:
    """DIP guard, and the hard constraint of this whole design.

    `lumon/interp/` may import `lumon.isa`, `lumon.errors` and
    `lumon.resolvers.base`. Nothing else: importing `cell` or `runners` would
    invert the dependency and make the interpreter untestable without threads.

    Checked over the parsed AST rather than the raw text, so a docstring that
    merely mentions `import threading` -- as this one does -- cannot fail it.
    """
    package = pathlib.Path(__file__).parent.parent / "lumon" / "interp"
    forbidden = {"threading", "lumon.cell", "lumon.runners", "lumon.waitgraph", "lumon.deadlock"}

    imported: set[str] = set()
    modules = sorted(package.glob("*.py"))
    assert modules, "no interpreter modules found to check"
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)

    for name in sorted(imported):
        root = name.split(".")[0]
        assert root != "threading", f"lumon/interp imports {name}"
        for banned in forbidden:
            assert name == banned or not name.startswith(f"{banned}."), (
                f"lumon/interp imports {name}"
            )


# --------------------------------------------------------- dispatch mechanics


def test_every_instruction_type_has_a_handler() -> None:
    for node in get_args(get_args(AnyInstruction)[0]):
        assert node in HANDLERS, f"no handler registered for {node.__name__}"


def test_an_instruction_without_a_handler_fails_loudly() -> None:
    """HANDLERS keys on the exact type, never isinstance: a new instruction
    with no handler must fail on first execution, not silently match its
    parent class and do nothing."""

    class Unregistered(Instruction):
        pass

    with pytest.raises(AssertionError, match="Unregistered"):
        step(Unregistered(line=1), State(), NoRefsResolver())


# ------------------------------------------------------- arithmetic and shape


def test_load_add_multiply() -> None:
    program: Program = (
        Load(operand=Const(value=2), line=1),
        Add(operand=Const(value=3), line=2),
        Multiply(operand=Const(value=4), line=3),
        Waffle(line=4),
    )
    assert run(program) == Outcome(staged=20)


def test_accumulator_starts_at_zero() -> None:
    """S13: a program that never LOADs begins at 0."""
    program: Program = (Add(operand=Const(value=7), line=1), Waffle(line=2))
    assert run(program) == Outcome(staged=7)


def test_modulo_uses_python_semantics() -> None:
    """S4: `%` follows the sign of the divisor. C would give -1 here."""
    program: Program = (
        Load(operand=Const(value=-7), line=1),
        Modulo(operand=Const(value=3), line=2),
        Waffle(line=3),
    )
    assert run(program) == Outcome(staged=2)


def test_modulo_zero_faults() -> None:
    """S4/S10: a fault, not a deadlock, and not -1."""
    program: Program = (
        Load(operand=Const(value=5), line=1),
        Modulo(operand=Const(value=0), line=2),
        Waffle(line=3),
    )
    with pytest.raises(ArithmeticFault, match="line 2"):
        run(program)


def test_last_waffle_wins() -> None:
    """S1 / 2.json DYLAN: WAFFLE stages, it does not publish."""
    program: Program = (
        Load(operand=Const(value=1), line=1),
        Waffle(line=2),
        Load(operand=Const(value=100), line=3),
        Waffle(line=4),
    )
    assert run(program) == Outcome(staged=100)


def test_no_waffle_yields_void() -> None:
    """S2: legal, and it must settle rather than hang."""
    assert run((Load(operand=Const(value=42), line=1),)) == Outcome(staged=None)


def test_an_empty_program_is_void() -> None:
    assert run(()) == Outcome(staged=None)


def test_waffle_with_no_preceding_load_stages_zero() -> None:
    """S13, the other half: the staging slot picks up the initial 0."""
    assert run((Waffle(line=1),)) == Outcome(staged=0)


def test_bare_wellness_check_resets_to_zero() -> None:
    """S5: the unconditional form."""
    program: Program = (
        Load(operand=Const(value=99), line=1),
        WellnessCheck(condition=None, line=2),
        Waffle(line=3),
    )
    assert run(program) == Outcome(staged=0)


def test_shift_repeats_the_body() -> None:
    """1.json HELLY: 10 -> 15 -> 20."""
    program: Program = (
        Load(operand=Const(value=10), line=1),
        Shift(times=2, body=(Add(operand=Const(value=5), line=3),), line=2),
        Waffle(line=5),
    )
    assert run(program) == Outcome(staged=20)


def test_shift_zero_times_skips_the_body() -> None:
    """S12: legal, and the body never executes."""
    program: Program = (
        Load(operand=Const(value=5), line=1),
        Shift(times=0, body=(Add(operand=Const(value=100), line=3),), line=2),
        Waffle(line=5),
    )
    assert run(program) == Outcome(staged=5)


def test_nested_shifts() -> None:
    program: Program = (
        Load(operand=Const(value=0), line=1),
        Shift(
            times=3,
            body=(Shift(times=2, body=(Add(operand=Const(value=1), line=4),), line=3),),
            line=2,
        ),
        Waffle(line=7),
    )
    assert run(program) == Outcome(staged=6)


def test_waffle_inside_a_shift_stages_each_iteration_last_wins() -> None:
    """3.json HELLY: stages 2, 4, 8 -- only 8 survives (S1)."""
    program: Program = (
        Load(operand=Const(value=1), line=1),
        Shift(
            times=3,
            body=(Multiply(operand=Const(value=2), line=3), Waffle(line=4)),
            line=2,
        ),
    )
    assert run(program) == Outcome(staged=8)


# ------------------------------------------------------------- the three refs


def test_add_bare_ref() -> None:
    program: Program = (
        Load(operand=Const(value=5), line=1),
        Add(operand=Ref(innie_id="HELLY"), line=2),
        Waffle(line=3),
    )
    outcome, _ = run_with(program, {"HELLY": 10})
    assert outcome == Outcome(staged=15)


def test_add_ref_list_sums_all() -> None:
    """S3 / 1.json IRVING: 0 + 20 + 5."""
    program: Program = (
        Load(operand=Const(value=0), line=1),
        Add(operand=RefList(innie_ids=("HELLY", "MARK")), line=2),
        Waffle(line=3),
    )
    outcome, _ = run_with(program, {"HELLY": 20, "MARK": 5})
    assert outcome == Outcome(staged=25)


def test_multiply_ref_list_takes_the_product() -> None:
    """S3: 2 * 3 * 4."""
    program: Program = (
        Load(operand=Const(value=2), line=1),
        Multiply(operand=RefList(innie_ids=("A", "B")), line=2),
        Waffle(line=3),
    )
    outcome, _ = run_with(program, {"A": 3, "B": 4})
    assert outcome == Outcome(staged=24)


def test_load_ref_list_sums_all() -> None:
    """LOAD replaces the accumulator, so a list operand contributes its sum."""
    program: Program = (
        Load(operand=RefList(innie_ids=("A", "B")), line=1),
        Waffle(line=2),
    )
    outcome, _ = run_with(program, {"A": 3, "B": 4})
    assert outcome == Outcome(staged=7)


def test_ref_list_is_resolved_in_source_order() -> None:
    program: Program = (Add(operand=RefList(innie_ids=("B", "A")), line=1), Waffle(line=2))
    _, resolver = run_with(program, {"A": 1, "B": 2})
    assert resolver.touched == ["B", "A"]


def test_repeated_reads_of_one_innie_agree() -> None:
    """S1 / 3.json MARK: published values are immutable, so both reads see 8."""
    program: Program = (
        Load(operand=Const(value=0), line=1),
        Shift(times=2, body=(Add(operand=Ref(innie_id="HELLY"), line=3),), line=2),
        Waffle(line=5),
    )
    outcome, _ = run_with(program, {"HELLY": 8})
    assert outcome == Outcome(staged=16)


# ---------------------------------------------------------------- conditions


def test_wellness_check_all_of_true_resets() -> None:
    """1.json BURT: 25 > 20 and 25 > 5, so the accumulator is reset."""
    program: Program = (
        Load(operand=Const(value=100), line=1),
        WellnessCheck(
            condition=gt(
                Ref(innie_id="IRVING"), Quantifier.ALL, RefList(innie_ids=("HELLY", "MARK"))
            ),
            line=2,
        ),
        Waffle(line=3),
    )
    outcome, _ = run_with(program, {"IRVING": 25, "HELLY": 20, "MARK": 5})
    assert outcome == Outcome(staged=0)


def test_wellness_check_any_of_false_does_not_reset() -> None:
    """3.json IRVING, first work order: 8 > 16 is false."""
    program: Program = (
        Load(operand=Const(value=100), line=1),
        WellnessCheck(
            condition=gt(Ref(innie_id="HELLY"), Quantifier.ANY, RefList(innie_ids=("MARK",))),
            line=2,
        ),
        Waffle(line=3),
    )
    outcome, _ = run_with(program, {"HELLY": 8, "MARK": 16})
    assert outcome == Outcome(staged=100)


def test_plain_comparison_resolves_both_sides() -> None:
    program: Program = (
        Load(operand=Const(value=100), line=1),
        WellnessCheck(
            condition=gt(Ref(innie_id="A"), Quantifier.NONE, Ref(innie_id="B")),
            line=2,
        ),
        Waffle(line=3),
    )
    outcome, resolver = run_with(program, {"A": 9, "B": 4})
    assert outcome == Outcome(staged=0)
    assert resolver.touched == ["A", "B"]


def test_every_comparison_operator_is_applied() -> None:
    resolver = DictResolver({})
    for op, expected in (
        (CmpOp.GT, False),
        (CmpOp.LT, False),
        (CmpOp.GE, True),
        (CmpOp.LE, True),
        (CmpOp.EQ, True),
        (CmpOp.NE, False),
    ):
        condition = Condition(
            lhs=Const(value=5), op=op, quantifier=Quantifier.NONE, rhs=Const(value=5)
        )
        assert evaluate(condition, resolver) is expected, op


def test_conditional_add_when_true() -> None:
    """3.json IRVING: 16 > 10, so 100 + 8 + 16."""
    program: Program = (
        Load(operand=Const(value=100), line=1),
        ConditionalAdd(
            targets=RefList(innie_ids=("HELLY", "MARK")),
            condition=gt(Ref(innie_id="MARK"), Quantifier.NONE, Const(value=10)),
            line=2,
        ),
        Waffle(line=3),
    )
    outcome, _ = run_with(program, {"HELLY": 8, "MARK": 16})
    assert outcome == Outcome(staged=124)


def test_conditional_add_when_false_never_touches_the_list() -> None:
    """S6, the load-bearing assertion for dynamic dependencies.

    GHOSTLY has no value at all: if the interpreter resolved the list before
    checking the condition, this would raise instead of returning 100.
    """
    program: Program = (
        Load(operand=Const(value=100), line=1),
        ConditionalAdd(
            targets=RefList(innie_ids=("HELLY", "GHOSTLY")),
            condition=gt(Ref(innie_id="MARK"), Quantifier.NONE, Const(value=1000)),
            line=2,
        ),
        Waffle(line=3),
    )
    outcome, resolver = run_with(program, {"HELLY": 8, "MARK": 16})
    assert outcome == Outcome(staged=100)
    assert resolver.touched == ["MARK"]


def test_any_of_short_circuits_and_never_reads_later_entries() -> None:
    """S8: true is absorbing, so UNREADABLE cannot change the answer."""
    resolver = DictResolver({"X": 100, "A": 1})
    condition = gt(Ref(innie_id="X"), Quantifier.ANY, RefList(innie_ids=("A", "UNREADABLE")))
    assert evaluate(condition, resolver) is True
    assert "UNREADABLE" not in resolver.touched


def test_all_of_short_circuits_on_first_false() -> None:
    """S8: false is absorbing."""
    resolver = DictResolver({"X": 1, "A": 100})
    condition = gt(Ref(innie_id="X"), Quantifier.ALL, RefList(innie_ids=("A", "UNREADABLE")))
    assert evaluate(condition, resolver) is False
    assert "UNREADABLE" not in resolver.touched


def test_vacuous_quantifiers() -> None:
    """S7: ANY OF [] is false, ALL OF [] is true -- the classic off-by-one."""
    resolver = DictResolver({})
    empty = RefList(innie_ids=())
    assert evaluate(gt(Const(value=1), Quantifier.ANY, empty), resolver) is False
    assert evaluate(gt(Const(value=1), Quantifier.ALL, empty), resolver) is True
    assert resolver.touched == []


def test_a_quantified_condition_resolves_its_lhs_unconditionally() -> None:
    """S7: the LHS always resolves, even when the list is empty."""
    resolver = DictResolver({"X": 1})
    condition = gt(Ref(innie_id="X"), Quantifier.ANY, RefList(innie_ids=()))
    assert evaluate(condition, resolver) is False
    assert resolver.touched == ["X"]


def test_full_3json_irving_by_hand() -> None:
    """The whole Innie: a false WELLNESS_CHECK then a true CONDITIONAL_ADD."""
    program: Program = (
        Load(operand=Const(value=100), line=1),
        WellnessCheck(
            condition=gt(Ref(innie_id="HELLY"), Quantifier.ANY, RefList(innie_ids=("MARK",))),
            line=2,
        ),
        ConditionalAdd(
            targets=RefList(innie_ids=("HELLY", "MARK")),
            condition=gt(Ref(innie_id="MARK"), Quantifier.NONE, Const(value=10)),
            line=3,
        ),
        Waffle(line=4),
    )
    outcome, _ = run_with(program, {"HELLY": 8, "MARK": 16})
    assert outcome == Outcome(staged=124)
