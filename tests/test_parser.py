import json
import pathlib

import pytest

from lumon.errors import ParseError
from lumon.isa import (
    Add,
    CmpOp,
    Condition,
    ConditionalAdd,
    Const,
    Load,
    Modulo,
    Multiply,
    Quantifier,
    Ref,
    RefList,
    Shift,
    Waffle,
    WellnessCheck,
)
from lumon.parser import parse, parse_condition

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"


def test_blank_lines_and_comments_are_stripped() -> None:
    prog = parse("LOAD 1\n\n  # a comment\nWAFFLE\n")
    assert len(prog) == 2
    assert isinstance(prog[0], Load)
    assert isinstance(prog[1], Waffle)


def test_literal_operand() -> None:
    (instr,) = parse("LOAD 10")
    assert isinstance(instr, Load)
    assert instr.operand == Const(value=10)
    assert instr.line == 1


def test_negative_literal() -> None:
    (instr,) = parse("ADD -5")
    assert isinstance(instr, Add)
    assert instr.operand == Const(value=-5)


def test_bare_ref_operand() -> None:
    (instr,) = parse("ADD HELLY")
    assert isinstance(instr, Add)
    assert instr.operand == Ref(innie_id="HELLY")


def test_ref_list_operand_tolerates_whitespace() -> None:
    (instr,) = parse("MULTIPLY [ HELLY ,MARK,  IRVING ]")
    assert isinstance(instr, Multiply)
    assert instr.operand == RefList(innie_ids=("HELLY", "MARK", "IRVING"))


def test_modulo_parses() -> None:
    (instr,) = parse("MODULO 7")
    assert isinstance(instr, Modulo)
    assert instr.operand == Const(value=7)


def test_waffle_takes_no_operand() -> None:
    with pytest.raises(ParseError) as exc:
        parse("WAFFLE 5")
    assert exc.value.line == 1


def test_unknown_opcode_reports_line() -> None:
    with pytest.raises(ParseError) as exc:
        parse("LOAD 1\nFROLIC 2\nWAFFLE")
    assert exc.value.line == 2
    assert "FROLIC" in str(exc.value)


def test_missing_operand_reports_line() -> None:
    with pytest.raises(ParseError) as exc:
        parse("LOAD")
    assert exc.value.line == 1


def test_empty_ref_list_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse("ADD []")


def test_malformed_bracket_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse("ADD [HELLY, MARK")


def test_opcodes_are_case_insensitive_but_ids_are_not() -> None:
    (instr,) = parse("add HELLY")
    assert isinstance(instr, Add)
    assert instr.operand == Ref(innie_id="HELLY")


def test_simple_comparison() -> None:
    cond = parse_condition("HELLY > 5", 1)
    assert cond == Condition(
        lhs=Ref(innie_id="HELLY"),
        op=CmpOp.GT,
        quantifier=Quantifier.NONE,
        rhs=Const(value=5),
    )


def test_two_character_operator_wins_over_one() -> None:
    cond = parse_condition("MARK <= 10", 1)
    assert cond.op is CmpOp.LE
    assert cond.rhs == Const(value=10)


def test_all_comparison_operators() -> None:
    for text, op in [
        ("A > 1", CmpOp.GT),
        ("A < 1", CmpOp.LT),
        ("A >= 1", CmpOp.GE),
        ("A <= 1", CmpOp.LE),
        ("A == 1", CmpOp.EQ),
        ("A != 1", CmpOp.NE),
    ]:
        assert parse_condition(text, 1).op is op


def test_any_of_quantifier() -> None:
    cond = parse_condition("HELLY > ANY OF [MARK, IRVING]", 1)
    assert cond.quantifier is Quantifier.ANY
    assert cond.rhs == RefList(innie_ids=("MARK", "IRVING"))


def test_all_of_quantifier() -> None:
    cond = parse_condition("IRVING > ALL OF [HELLY, MARK]", 1)
    assert cond.quantifier is Quantifier.ALL


def test_literal_lhs_is_allowed() -> None:
    cond = parse_condition("5 > MARK", 1)
    assert cond.lhs == Const(value=5)


def test_ref_list_lhs_is_rejected() -> None:
    with pytest.raises(ParseError):
        parse_condition("[A, B] > 5", 1)


def test_bare_wellness_check() -> None:
    (instr,) = parse("WELLNESS_CHECK")
    assert isinstance(instr, WellnessCheck)
    assert instr.condition is None


def test_conditional_wellness_check() -> None:
    (instr,) = parse("WELLNESS_CHECK IRVING > ALL OF [HELLY, MARK]")
    assert isinstance(instr, WellnessCheck)
    assert instr.condition is not None
    assert instr.condition.quantifier is Quantifier.ALL


def test_conditional_add() -> None:
    (instr,) = parse("CONDITIONAL_ADD [HELLY, MARK] IF MARK > 10")
    assert isinstance(instr, ConditionalAdd)
    assert instr.targets == RefList(innie_ids=("HELLY", "MARK"))
    assert instr.condition.lhs == Ref(innie_id="MARK")


def test_conditional_add_without_if_is_an_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse("CONDITIONAL_ADD [HELLY]")
    assert "IF" in str(exc.value)


def test_conditional_add_without_a_list_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse("CONDITIONAL_ADD HELLY IF MARK > 10")


def test_condition_without_operator_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse_condition("HELLY MARK", 1)


def test_quantifier_without_brackets_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse_condition("HELLY > ANY OF MARK", 1)


def test_empty_quantifier_list_is_an_error() -> None:
    # The spec only ever quantifies over non-empty groups of Innies, so an
    # empty list in schedule text is a typo, not a vacuous quantifier.
    with pytest.raises(ParseError):
        parse_condition("HELLY > ANY OF []", 1)


def test_shift_wraps_its_body() -> None:
    prog = parse("LOAD 10\nSHIFT 2 TIMES\nADD 5\nEND_SHIFT\nWAFFLE")
    assert len(prog) == 3
    shift = prog[1]
    assert isinstance(shift, Shift)
    assert shift.times == 2
    assert len(shift.body) == 1
    body = shift.body[0]
    assert isinstance(body, Add)
    assert body.operand == Const(value=5)
    assert body.line == 3


def test_shifts_nest() -> None:
    prog = parse(
        "SHIFT 3 TIMES\n"
        "  SHIFT 2 TIMES\n"
        "    ADD 1\n"
        "  END_SHIFT\n"
        "  MULTIPLY 2\n"
        "END_SHIFT"
    )
    outer = prog[0]
    assert isinstance(outer, Shift)
    assert outer.times == 3
    assert len(outer.body) == 2
    inner = outer.body[0]
    assert isinstance(inner, Shift)
    assert inner.times == 2
    assert isinstance(outer.body[1], Multiply)


def test_shift_zero_times_is_legal() -> None:
    prog = parse("SHIFT 0 TIMES\nADD 1\nEND_SHIFT")
    shift = prog[0]
    assert isinstance(shift, Shift)
    assert shift.times == 0
    assert len(shift.body) == 1


def test_negative_shift_count_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse("SHIFT -1 TIMES\nADD 1\nEND_SHIFT")


def test_shift_without_times_keyword_is_an_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse("SHIFT 2\nADD 1\nEND_SHIFT")
    assert "TIMES" in str(exc.value)


def test_unclosed_shift_reports_the_opening_line() -> None:
    with pytest.raises(ParseError) as exc:
        parse("LOAD 1\nSHIFT 2 TIMES\nADD 5")
    assert exc.value.line == 2
    assert "END_SHIFT" in str(exc.value)


def test_unmatched_end_shift_is_an_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse("LOAD 1\nEND_SHIFT")
    assert exc.value.line == 2


def test_waffle_inside_a_shift_parses() -> None:
    prog = parse("LOAD 1\nSHIFT 3 TIMES\nMULTIPLY 2\nWAFFLE\nEND_SHIFT")
    shift = prog[1]
    assert isinstance(shift, Shift)
    assert len(shift.body) == 2


@pytest.mark.parametrize("name", ["1.json", "2.json", "3.json"])
def test_sample_schedules_parse(name: str) -> None:
    data = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    for innie in data["innies"]:
        assert parse(innie["schedule"])
