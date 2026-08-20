"""Every failure mode in the system, in one hierarchy."""


class LumonError(Exception):
    """Root of all Lumon errors."""


class ParseError(LumonError):
    """Malformed schedule text. Always carries a line number."""

    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


class ScheduleError(LumonError):
    """The work schedule JSON is structurally wrong: missing keys, wrong
    types, duplicate Innie ids."""


class UnknownInnieError(LumonError):
    """A schedule references an Innie ID that is not in the work schedule."""

    def __init__(self, referrer: str, unknown: str, line: int):
        super().__init__(f"{referrer} line {line}: references unknown Innie {unknown!r}")
        self.referrer = referrer
        self.unknown = unknown
        self.line = line


class ArithmeticFault(LumonError):
    """MODULO 0, or any other arithmetic that cannot produce a value."""


class NoWorkProduct(LumonError):
    """Read of an Innie that finished its workday without ever WAFFLEing."""

    def __init__(self, innie_id: str):
        super().__init__(f"{innie_id} finished without a WAFFLE (VOID)")
        self.innie_id = innie_id


class DependencyFaulted(LumonError):
    """A dependency faulted; chained via `raise ... from` to the cause."""

    def __init__(self, innie_id: str):
        super().__init__(f"dependency {innie_id} faulted")
        self.innie_id = innie_id


class DoubleSettle(LumonError):
    """A Cell was settled twice. Always an implementation bug."""


class Cancelled(LumonError):
    """Raised into a thread whose Innie was resolved as a deadlock member.

    Not a fault: the Cell already holds -1. The runner catches this and
    returns without settling.
    """

    def __init__(self, innie_id: str):
        super().__init__(f"{innie_id} cancelled as a deadlock cycle member")
        self.innie_id = innie_id


class WatchdogTimeout(LumonError):
    """The run made no progress and cells are still pending.

    This should never fire in a correct implementation. It is a bug
    detector, not a fallback.
    """
