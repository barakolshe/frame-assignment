"""Mutable per-workday state.

Deliberately a plain class, not a pydantic model: it is mutated once per
instruction and never validated, compared, or frozen, so `BaseModel` would buy
nothing and cost a validation pass per write.
"""


class State:
    """The accumulator plus the staging slot.

    `staged` is what keeps a workday atomic: `WAFFLE` copies the accumulator here
    rather than publishing it, so a reader can never observe a partial product.
    The runner publishes the slot once, at the end of the workday.
    """

    __slots__ = ("acc", "staged")

    def __init__(self) -> None:
        self.acc: int = 0  # a program that never LOADs still starts from 0
        self.staged: int | None = None  # still None at the end means VOID
