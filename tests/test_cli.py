"""The command line: JSON in, JSON out.

`--mode` is derived from the RUNNERS table rather than hard-coded, so the
mode tests double as the check that the OCP extension point is actually wired
up: a runner that registers itself becomes selectable without editing the CLI.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from lumon.cli import main

REPO = pathlib.Path(__file__).parent.parent
SAMPLES = REPO / "Hometask Backend"


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, Any]:
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_runs_a_sample_and_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, str(SAMPLES / "3.json"))
    assert code == 0
    assert out["innies"] == [
        {"id": "HELLY", "result": 8},
        {"id": "MARK", "result": 16},
        {"id": "IRVING", "result": 124},
        {"id": "BURT", "result": 497},
        {"id": "DYLAN", "result": 0},
    ]


@pytest.mark.parametrize("name", ["1.json", "2.json", "3.json"])
def test_serial_mode_agrees_with_concurrent(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    # The determinism invariant at the outermost seam: the two runners wait
    # very differently and must still print identical output.
    _, concurrent = run(capsys, str(SAMPLES / name))
    _, serial = run(capsys, str(SAMPLES / name), "--mode", "serial")
    assert concurrent == serial


def test_mode_choices_come_from_the_runners_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([str(SAMPLES / "1.json"), "--mode", "telepathic"])
    assert exit_info.value.code == 2
    assert "telepathic" in capsys.readouterr().err


def test_void_innie_reports_null(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "void.json"
    path.write_text(json.dumps({"innies": [{"id": "A", "schedule": "LOAD 5"}]}))
    code, out = run(capsys, str(path))
    assert code == 0
    assert out["innies"] == [{"id": "A", "result": None}]


def test_faulted_innie_reports_error_and_exits_nonzero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fault.json"
    path.write_text(
        json.dumps({"innies": [{"id": "A", "schedule": "LOAD 5\nMODULO 0\nWAFFLE"}]})
    )
    code, out = run(capsys, str(path))
    assert code == 1
    assert out["innies"][0]["result"] is None
    assert "MODULO by zero" in out["innies"][0]["error"]


def test_a_faults_cause_chain_is_reported(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # "BURT failed because DYLAN failed because MODULO by zero".
    path = tmp_path / "chain.json"
    path.write_text(
        json.dumps(
            {
                "innies": [
                    {"id": "DYLAN", "schedule": "LOAD 5\nMODULO 0\nWAFFLE"},
                    {"id": "BURT", "schedule": "LOAD 0\nADD DYLAN\nWAFFLE"},
                ]
            }
        )
    )
    code, out = run(capsys, str(path))
    assert code == 1
    assert out["innies"][1]["error"] == (
        "dependency DYLAN faulted because line 2: MODULO by zero"
    )


def test_deadlocked_innies_report_minus_one_and_exit_zero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A cycle is a value, not a fault. Nothing failed, so exit 0.
    path = tmp_path / "cycle.json"
    path.write_text(
        json.dumps({"innies": [{"id": "A", "schedule": "LOAD 5\nADD A\nWAFFLE"}]})
    )
    code, out = run(capsys, str(path), "--mode", "serial")
    assert code == 0
    assert out["innies"] == [{"id": "A", "result": -1}]


def test_load_error_exits_nonzero_with_a_message(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"innies": [{"id": "A", "schedule": "LOAD 1\nADD GHOST\nWAFFLE"}]})
    )
    code = main([str(path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "GHOST" in err


def test_missing_file_exits_nonzero_with_a_message(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(tmp_path / "nope.json")])
    assert code == 2
    assert "nope.json" in capsys.readouterr().err


def test_python_dash_m_lumon_runs_the_cli() -> None:
    """The only cover for `lumon/__main__.py`'s wiring -- importing it would
    execute the CLI as a side effect, so it has to be a subprocess."""
    completed = subprocess.run(
        [sys.executable, "-m", "lumon", str(SAMPLES / "1.json")],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["innies"][0] == {"id": "HELLY", "result": 20}
