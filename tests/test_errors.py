from lumon.errors import (
    ArithmeticFault,
    Cancelled,
    DependencyFaulted,
    DoubleSettle,
    LumonError,
    NoWorkProduct,
    ParseError,
    ScheduleError,
    UnknownInnieError,
    WatchdogTimeout,
)


def test_all_errors_share_a_root() -> None:
    for cls in (
        ParseError,
        ScheduleError,
        UnknownInnieError,
        ArithmeticFault,
        NoWorkProduct,
        DependencyFaulted,
        DoubleSettle,
        Cancelled,
        WatchdogTimeout,
    ):
        assert issubclass(cls, LumonError)


def test_parse_error_reports_line_number() -> None:
    err = ParseError("bad opcode", line=7)
    assert err.line == 7
    assert "line 7" in str(err)


def test_unknown_innie_error_names_referrer_target_and_line() -> None:
    err = UnknownInnieError("BURT", "DYLAN", 3)
    assert (err.referrer, err.unknown, err.line) == ("BURT", "DYLAN", 3)
    assert "BURT" in str(err)
    assert "DYLAN" in str(err)


def test_innie_scoped_errors_carry_the_innie_id() -> None:
    for cls in (NoWorkProduct, DependencyFaulted, Cancelled):
        err = cls("HELLY")
        assert err.innie_id == "HELLY"
        assert "HELLY" in str(err)
