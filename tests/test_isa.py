import pytest
from pydantic import ValidationError

from lumon.isa import (
    Add,
    CmpOp,
    Condition,
    ConditionalAdd,
    Const,
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
    all_refs,
)


def test_nodes_are_frozen() -> None:
    const = Const(value=1)
    with pytest.raises(ValidationError):
        const.value = 2


def test_nodes_are_hashable_and_compare_by_value() -> None:
    assert Const(value=5) == Const(value=5)
    assert len({Ref(innie_id="A"), Ref(innie_id="A")}) == 1


def test_const_and_ref_carry_their_payload() -> None:
    assert Const(value=5).value == 5
    assert Ref(innie_id="HELLY").innie_id == "HELLY"
    assert RefList(innie_ids=("HELLY", "MARK")).innie_ids == ("HELLY", "MARK")


def test_bad_payload_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        Const(value="not an int")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Shift(times=-1, body=(), line=1)  # parser rejects too; belt and braces
    with pytest.raises(ValidationError):
        Waffle(line=0)  # line numbers are 1-based


def test_unknown_field_is_rejected() -> None:
    """extra="forbid" turns a typo into a loud error, not a dropped value."""
    with pytest.raises(ValidationError):
        Ref(innie_id="HELLY", inne_id="typo")  # type: ignore[call-arg]


def test_refs_collects_referenced_ids() -> None:
    assert Const(value=3).refs() == ()
    assert Ref(innie_id="HELLY").refs() == ("HELLY",)
    assert RefList(innie_ids=("A", "B")).refs() == ("A", "B")
    assert Waffle(line=1).refs() == ()


def test_condition_refs_include_lhs_and_quantified_list() -> None:
    cond = Condition(
        lhs=Ref(innie_id="HELLY"),
        op=CmpOp.GT,
        quantifier=Quantifier.ANY,
        rhs=RefList(innie_ids=("MARK", "IRVING")),
    )
    assert set(cond.refs()) == {"HELLY", "MARK", "IRVING"}


def test_a_quantified_condition_may_hold_an_empty_ref_list() -> None:
    """The vacuous quantifiers -- ANY OF [] is false, ALL OF [] is true --
    are evaluated later, but the node has to be constructible now."""
    cond = Condition(
        lhs=Const(value=1),
        op=CmpOp.GT,
        quantifier=Quantifier.ALL,
        rhs=RefList(innie_ids=()),
    )
    assert cond.refs() == ()


def test_conditional_add_refs_include_targets_and_condition() -> None:
    instr = ConditionalAdd(
        targets=RefList(innie_ids=("HELLY", "MARK")),
        condition=Condition(
            lhs=Ref(innie_id="MARK"),
            op=CmpOp.GT,
            quantifier=Quantifier.NONE,
            rhs=Const(value=10),
        ),
        line=1,
    )
    assert instr.refs() == ("HELLY", "MARK", "MARK")


def test_shift_holds_a_nested_body() -> None:
    inner = Shift(times=2, body=(Add(operand=Const(value=1), line=3),), line=2)
    outer = Shift(times=3, body=(inner,), line=1)
    nested = outer.body[0]
    assert isinstance(nested, Shift)
    add = nested.body[0]
    assert isinstance(add, Add)
    assert add.operand == Const(value=1)


def test_shift_body_does_not_coerce_children_to_the_base_class() -> None:
    """The pydantic v2 LSP trap. If `body` were annotated
    `tuple[Instruction, ...]`, pydantic would rebuild this Add as a bare
    Instruction and `.operand` would vanish. Annotating with the
    AnyInstruction union preserves the concrete type."""
    shift = Shift(times=1, body=(Add(operand=Const(value=7), line=2),), line=1)
    assert type(shift.body[0]) is Add
    assert shift.body[0].operand == Const(value=7)


def test_shift_body_keeps_subclass_fields_through_serialization() -> None:
    """The pin with teeth. `revalidate_instances` defaults to "never", so
    a subclass *instance* survives a base-class annotation untouched --
    the two type-identity assertions above pass either way. Serialization
    is where the base annotation actually bites: pydantic serializes the
    field with the *annotated* model's serializer and silently drops
    `operand`. Flip `body` to `tuple[Instruction, ...]` and this fails."""
    shift = Shift(times=1, body=(Add(operand=Const(value=7), line=2),), line=1)
    assert shift.model_dump()["body"][0] == {"line": 2, "operand": {"value": 7}}


def test_deeply_nested_shifts_keep_their_types() -> None:
    prog = Shift(
        times=1,
        body=(
            Shift(
                times=1,
                body=(Multiply(operand=RefList(innie_ids=("A",)), line=3),),
                line=2,
            ),
        ),
        line=1,
    )
    inner = prog.body[0]
    assert isinstance(inner, Shift)
    assert type(inner.body[0]) is Multiply


def test_all_refs_walks_shifts_and_conditions_in_order() -> None:
    """Order is preserved because the loader reports the *first* unknown
    reference it finds, so `all_refs` returns a tuple, not a set."""
    program: Program = (
        Load(operand=Ref(innie_id="HELLY"), line=1),
        Add(operand=RefList(innie_ids=("MARK", "IRVING")), line=2),
        WellnessCheck(
            condition=Condition(
                lhs=Ref(innie_id="BURT"),
                op=CmpOp.GT,
                quantifier=Quantifier.ANY,
                rhs=RefList(innie_ids=("DYLAN",)),
            ),
            line=3,
        ),
        Shift(
            times=2,
            body=(
                Shift(
                    times=1,
                    body=(Modulo(operand=Ref(innie_id="GEMMA"), line=6),),
                    line=5,
                ),
            ),
            line=4,
        ),
        Waffle(line=8),
    )
    assert all_refs(program) == (
        "HELLY",
        "MARK",
        "IRVING",
        "BURT",
        "DYLAN",
        "GEMMA",
    )


def test_all_refs_of_a_program_without_references_is_empty() -> None:
    program: Program = (
        Load(operand=Const(value=10), line=1),
        WellnessCheck(condition=None, line=2),
        Waffle(line=3),
    )
    assert all_refs(program) == ()
