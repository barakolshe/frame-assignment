import pathlib

import pytest

from lumon.errors import ParseError, ScheduleError, UnknownInnieError
from lumon.loader import load, load_path

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"


def test_load_preserves_input_order() -> None:
    innies = load(
        {
            "innies": [
                {"id": "B", "schedule": "LOAD 1\nWAFFLE"},
                {"id": "A", "schedule": "LOAD 2\nWAFFLE"},
            ]
        }
    )
    assert [i.id for i in innies] == ["B", "A"]


def test_load_parses_the_schedule_text() -> None:
    (innie,) = load({"innies": [{"id": "A", "schedule": "LOAD 1\nWAFFLE"}]})
    assert len(innie.program) == 2


def test_unknown_reference_reports_referrer_and_line() -> None:
    with pytest.raises(UnknownInnieError) as exc:
        load({"innies": [{"id": "A", "schedule": "LOAD 1\nADD GHOST\nWAFFLE"}]})
    assert exc.value.referrer == "A"
    assert exc.value.unknown == "GHOST"
    assert exc.value.line == 2


def test_unknown_reference_inside_a_shift_is_caught() -> None:
    with pytest.raises(UnknownInnieError):
        load({"innies": [{"id": "A", "schedule": "SHIFT 2 TIMES\nADD GHOST\nEND_SHIFT"}]})


def test_unknown_reference_inside_a_shift_reports_the_inner_line() -> None:
    # Not the SHIFT's line: every Instruction carries its own `line`, so the
    # report must point at the instruction that actually names the ghost.
    with pytest.raises(UnknownInnieError) as exc:
        load(
            {
                "innies": [
                    {
                        "id": "A",
                        "schedule": "LOAD 1\nSHIFT 2 TIMES\nSHIFT 1 TIMES\nADD GHOST\n"
                        "END_SHIFT\nEND_SHIFT",
                    }
                ]
            }
        )
    assert exc.value.line == 4


def test_unknown_reference_inside_a_condition_is_caught() -> None:
    with pytest.raises(UnknownInnieError):
        load({"innies": [{"id": "A", "schedule": "WELLNESS_CHECK A > ANY OF [GHOST]"}]})


def test_self_reference_is_accepted_at_load_time() -> None:
    # S9: a self-reference is a one-node cycle resolved to -1 at runtime,
    # NOT a load-time error.
    innies = load({"innies": [{"id": "A", "schedule": "LOAD 5\nADD A\nWAFFLE"}]})
    assert len(innies) == 1


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ScheduleError, match="duplicate"):
        load(
            {
                "innies": [
                    {"id": "A", "schedule": "WAFFLE"},
                    {"id": "A", "schedule": "WAFFLE"},
                ]
            }
        )


def test_missing_innies_key_is_rejected() -> None:
    with pytest.raises(ScheduleError):
        load({"workers": []})


def test_empty_schedule_is_rejected() -> None:
    with pytest.raises(ScheduleError):
        load({"innies": []})


def test_wrong_field_types_are_rejected_by_pydantic() -> None:
    with pytest.raises(ScheduleError):
        load({"innies": [{"id": 42, "schedule": "WAFFLE"}]})


def test_unexpected_field_is_rejected() -> None:
    with pytest.raises(ScheduleError):
        load({"innies": [{"id": "A", "schedule": "WAFFLE", "shift": "night"}]})


def test_parse_errors_propagate_with_their_line_number() -> None:
    with pytest.raises(ParseError) as exc:
        load({"innies": [{"id": "A", "schedule": "LOAD 1\nFROLIC 2"}]})
    assert exc.value.line == 2


@pytest.mark.parametrize("name", ["1.json", "2.json", "3.json"])
def test_sample_files_load(name: str) -> None:
    innies = load_path(SAMPLES / name)
    assert innies
