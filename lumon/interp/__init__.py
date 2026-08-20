"""The pure interpreter: an ISA program in, an `Outcome` out.

This package may import `lumon.isa`, `lumon.errors` and `lumon.resolvers.base`
-- and nothing else. It must never reach for threads, cells, or runners; every
concurrency concern lives behind the `Resolver` seam. A test in
`tests/test_interp.py` pins that rule against the parsed AST of these modules.
"""

from lumon.interp.engine import Outcome, execute, run_block, step
from lumon.interp.handlers import HANDLERS, evaluate

__all__ = ["HANDLERS", "Outcome", "evaluate", "execute", "run_block", "step"]
