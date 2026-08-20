# Lumon Innie Task Scheduler — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a concurrent interpreter for a JSON "work schedule" of Innies — each an assembly-like program that may block on other Innies' published results — producing deterministic results despite thread-per-Innie execution.

**Architecture:** Three layers with a hard seam between them. (1) A **pure interpreter** that executes one Innie's instruction tree and knows nothing about threads — it reaches the outside world only through a `Resolver` interface. (2) A **Registry of Cells**, one per Innie: write-once publish/subscribe slots, plus a runtime wait-for graph for deadlock detection. (3) A **scheduler** that runs one thread per Innie and supplies a concurrent `Resolver`. Because the interpreter is concurrency-free, a second `Resolver` — single-threaded, lazy, memoized, recursive — drives the *same* interpreter as a reference oracle. Asserting both modes produce identical registries is the test that guards the whole design.

**Tech Stack:** Python 3.14, pydantic v2 for all data types, stdlib `threading` / `enum` / `json` / `argparse` / `re` for everything else. pytest for tests.

**Spec:** `Hometask Backend/excersice.txt` (authoritative). Samples: `Hometask Backend/1.json`, `2.json`, `3.json`.

---

## Global Constraints

- **pydantic v2 is the type layer.** Every value type — ISA nodes, conditions, loader input models, `Result`, `Outcome` — is a `BaseModel`. The exceptions are deliberate and listed in "Where pydantic does not go" below. Beyond pydantic, runtime code is stdlib only; pytest is the sole additional dev dependency. Repetition tests use plain Python loops, not `pytest-repeat`.
- **Three abstractions have their own package, base class in its own file.** `Resolver`, `Runner`, and `DeadlockDetector` each get `<pkg>/base.py` holding the interface and one file per implementation. Nothing else gets an ABC — see "Where abstraction does not go".
- **Concurrency is mandatory.** The spec requires "multiple Outies (threads/workers)". The serial evaluator exists as a test oracle and a `--mode serial` debugging aid — never as the default.
- **One thread per Innie, not a pool.** A bounded pool reintroduces starvation deadlock: N blocked Innies in an N-worker pool can starve the very Innies they wait on. Threads are `daemon=True` so a hung run cannot wedge the test suite.
- **Determinism is a hard requirement.** Same input JSON → identical output, every run, in both modes. Any design that trades determinism for speed is rejected.
- **Every Innie settles exactly once.** No escape path — success, arithmetic fault, deadlock, or unhandled exception — may leave a Cell pending. A pending Cell hangs every dependent.
- **Deadlock is a value, not an error.** Spec: "when detected a circular dependency between innies, they will WAFFLE to the value of -1."
- **Integer arithmetic only.** Python ints are arbitrary precision; no overflow handling.
- **Opcodes and keywords are uppercase.** Innie IDs match case-sensitively as given in the JSON.

---

## Semantics: Decisions the Spec Leaves Implicit

The spec is prose. These are the readings this plan commits to, with evidence. **Read this section before writing code** — most of the hard bugs here are semantic, not concurrent.

### S1. One publish per Innie, at end of workday, carrying the *last* WAFFLE's value

Three spec lines converge:
- "Each Innie's workday executes atomically from start to finish"
- "no partial work products are visible"
- "If an Innie WAFFLEs multiple times, waiting Innies receive only the final product"

So `WAFFLE` does **not** publish. It **stages**: copies the accumulator into a staging slot. At end of workday the staging slot is published to the Cell — once, atomically. A reader either sees nothing (and blocks) or sees the final value. Never an intermediate.

`2.json` discriminates this from every other reading:

```
DYLAN: LOAD 1, WAFFLE, LOAD 100, WAFFLE      -> stages 1, then 100; publishes 100
BURT:  LOAD 0, ADD [DYLAN], WAFFLE,
              ADD [DYLAN], WAFFLE             -> 0+100=100 (stage), 100+100=200 (stage); publishes 200
```

Note what this buys: **BURT reads DYLAN twice and gets 100 both times.** Under eager publish, BURT's reads could return 1 and 100 depending on timing — non-deterministic, and forbidden. Published values are immutable, so repeated reads within one workday always agree.

### S2. An Innie with zero WAFFLEs publishes VOID

"Zero or more WAFFLE commands" is explicit, so this is legal and must not hang. If the staging slot is still empty at end of workday, publish `VOID`.

- Reading a VOID Innie raises `NoWorkProduct`, which faults the reader (S10).
- The CLI reports VOID as `null`.
- The Cell is still **settled** — never left pending. That is the whole point.

### S3. Three operand forms

The spec's example JSON uses bare `ADD HELLY` alongside `ADD [HELLY, MARK]`; `3.json` uses both.

| Form | Example | Meaning |
|---|---|---|
| `Const(int)` | `ADD 5` | plain arithmetic |
| `Ref(id)` | `ADD HELLY` | wait for HELLY, use its value |
| `RefList([id, ...])` | `ADD [HELLY, MARK]` | wait for **all**, then combine |

`ADD [A, B]` = `acc + value(A) + value(B)`. `MULTIPLY [A, B]` = `acc * value(A) * value(B)`. Both operations are commutative and associative over integers, so combining order cannot affect the result — one less determinism hazard.

`ADD []` is a parse error.

### S4. MODULO uses Python semantics; MODULO 0 faults

`MODULO 0` raises `ArithmeticFault` — a fault, not -1; it is not a circular dependency. Python's `%` follows the sign of the divisor (`-7 % 3 == 2`; C gives `-1`). Pick Python's, document it, and pin it with a test so nobody "fixes" it later.

### S5. WELLNESS_CHECK has two forms

- `WELLNESS_CHECK` (bare) → unconditionally set accumulator to 0.
- `WELLNESS_CHECK <condition>` → set accumulator to 0 **iff** the condition is true.

Both appear under the same spec heading; both must parse.

### S6. CONDITIONAL_ADD does not touch its list when the condition is false

`CONDITIONAL_ADD [list] IF <condition>` — evaluate the condition first. If true, wait for every Innie in `[list]` and add their sum. If false, **skip entirely and never resolve `[list]`**.

This is semantics, not optimization. Spec Requirement 3: "Innies automatically wait for dependencies **but only when needed**." It is also what makes the dependency graph *dynamic* — an Innie's real dependency set is not knowable from source text alone. **This single fact is why deadlock detection must run on the runtime wait-for graph, never on a static scan.**

### S7. Condition grammar

```
condition := operand OP rhs
rhs       := operand | "ANY OF" "[" refs "]" | "ALL OF" "[" refs "]"
operand   := INT | ID
OP        := ">" | "<" | ">=" | "<=" | "==" | "!="
refs      := ID ("," ID)*
```

- `x > ANY OF [A, B]` means `∃ r : x > value(r)`.
- `x > ALL OF [A, B]` means `∀ r : x > value(r)`.
- The LHS always resolves — an unconditional wait when it is a `Ref`.
- Vacuous quantifiers: `ANY OF []` is `false`, `ALL OF []` is `true`. Pin both with tests; they are the classic off-by-one in quantifier code.

Only `>` appears in the samples, but `MARK <= 10` appears in the spec prose, so implement the full operator set.

### S8. The short-circuit determinism invariant — the most important rule in this project

> **Short-circuiting may change how long you wait. It may never change what you compute.**

The spec demands both "unblock as soon as the result is determined" (Req. 4) and "deterministic results" (Req. 6). These are compatible only because the boolean algebra is order-independent:

- `ANY OF` is an OR-fold. **`true` is absorbing**: once any comparison is true, the answer is `true` regardless of the rest.
- `ALL OF` is an AND-fold. **`false` is absorbing**: once any comparison is false, the answer is `false`.

So the concurrent resolver may await all referenced Cells simultaneously and return the instant an absorbing value lands, while the serial resolver walks strictly left-to-right — and **both produce the same boolean**. Different waiting, identical answer.

Two places order could leak, and each needs an explicit rule.

**(a) A branch that would fault.** Rule: **an absorbing result wins over a pending fault.** `x > ANY OF [GOOD, VOID_INNIE]` is `true` when `x > value(GOOD)`, regardless of `VOID_INNIE`. Order-independent, therefore deterministic.

**(b) A branch that cycles back to the asking Innie.** This one is subtle and it is where a naive serial oracle silently disagrees with the concurrent run. Consider:

```
DYLAN: LOAD 1  WAFFLE
HELLY: LOAD 100  WELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]  WAFFLE
MARK:  LOAD 0  ADD HELLY  WAFFLE
```

`MARK` cycles back to `HELLY`; `DYLAN` does not. Concurrently, HELLY awaits both branches, DYLAN settles first, `5 > 1` is true, HELLY resets to 0 — no deadlock, because HELLY always had a way forward (Task 13). But a serial oracle that walks strictly left-to-right recurses into `MARK` first, finds `HELLY` on the stack, declares a cycle, and publishes `-1` for both. **Same input, two different answers.** That is exactly the class of bug Task 16 exists to catch, so the oracle must not have it built in.

Rule: **a branch that would re-enter an Innie already being evaluated is deferred, not resolved.** Try every non-re-entrant branch first; only if none yields an absorbing result do the deferred branches get resolved (to `-1`, per S9). Both modes then compute the same fold over the same multiset of branch values, so both agree. `SerialResolver` implements this in Task 14; `ConcurrentResolver` gets it for free, because a cyclic branch cannot settle until the detector fires, so a non-cyclic branch always settles first.

The unifying statement of both rules: **the value of a quantified condition is the fold over *all* branches; short-circuit is permitted only on an absorbing value, which by definition cannot be overturned by the branches it skips.**

Every condition test asserts serial and concurrent agree.

### S9. Deadlock resolution

"When detected a circular dependency between innies, they will WAFFLE to the value of -1. Please keep in mind that work schedules are always deterministic, even if there is a deadlock."

- Only Innies **on the cycle** publish -1.
- Innies merely *waiting on* a cycle member are not deadlocked. Cycle members publish -1, which unblocks them, and they consume -1 as ordinary data. The deadlock resolves outward.
- **A self-reference is a cycle of length 1, not a parse error.** `HELLY: "LOAD 5\nADD HELLY\nWAFFLE"` publishes -1.

> **Correction to the original draft plan.** It said: *"Don't let a task read its own cell. Reject at parse time, or it deadlocks against itself."* Right instinct, wrong remedy — the spec assigns self-reference a defined runtime value, so rejecting at parse time returns a wrong answer on a legal input. Detect it, don't reject it. Full mechanism in Task 12.

### S10. Faults propagate; they are distinct from deadlock

Two failure kinds, deliberately not conflated:

| Kind | Cause | Cell contents | Dependent sees |
|---|---|---|---|
| **Deadlock** | circular dependency | value `-1` | the integer `-1` |
| **Fault** | `MODULO 0`, reading a VOID Innie, internal bug | exception | re-raised, chained |

A dependent reading a faulted Cell faults too, via `raise ... from cause`, so the traceback reads "BURT failed because DYLAN failed because MODULO 0". The CLI reports faulted Innies as `null` plus an error string and exits non-zero.

### S11. Invalid references fail at load time

Every Innie ID is known before execution starts. A reference to an unknown ID is caught by a pre-flight pass over all parsed programs and reported as `UnknownInnieError` naming the referring Innie, the line number, and the bad ID. Pre-populating the Registry from the Innie list means an unknown reference can never become a silent hang.

### S12. SHIFT is a nested block, not a jump

The spec calls for "nested work shifts". Parse `SHIFT n TIMES ... END_SHIFT` into a `Shift(times, body)` node holding a child instruction list. Recursion makes nesting free and there are no jump targets to get wrong.

- `SHIFT 0 TIMES` is legal; the body never executes.
- Negative counts, non-integer counts, missing `TIMES`, unclosed `SHIFT`, unmatched `END_SHIFT` — all parse errors with line numbers.
- `WAFFLE` inside a shift stages once per iteration; only the last survives (S1).

### S13. Accumulator starts at 0

A program that never `LOAD`s begins at 0. `WAFFLE` with no preceding `LOAD` stages 0.

---

## Golden Values (hand-computed — these are the acceptance tests)

**`1.json`**
```
HELLY:  LOAD 10; SHIFT 2 { ADD 5 }                 -> 15, 20            => 20
MARK:   LOAD 2; ADD 3                                                   => 5
IRVING: LOAD 0; ADD [HELLY, MARK] = 0+20+5                              => 25
BURT:   LOAD 100; WELLNESS_CHECK 25 > ALL OF [20, 5] -> true -> reset    => 0
```

**`2.json`**
```
DYLAN: stages 1 then 100                                                => 100
BURT:  0+100=100 (stage); 100+100=200 (stage)                           => 200
```

**`3.json`**
```
HELLY:  LOAD 1; SHIFT 3 { MULTIPLY 2; WAFFLE }     -> 2, 4, 8           => 8
MARK:   LOAD 0; SHIFT 2 { ADD HELLY }              -> 0+8=8, 8+8=16     => 16
IRVING: LOAD 100
        WELLNESS_CHECK HELLY > ANY OF [MARK]       -> 8 > 16 false -> no reset
        CONDITIONAL_ADD [HELLY, MARK] IF MARK > 10 -> 16>10 true -> +8+16
                                                                        => 124
BURT:   LOAD 1; SHIFT 4 { ADD IRVING; WAFFLE }     -> 125,249,373,497   => 497
DYLAN:  LOAD 0; ADD [HELLY,MARK,IRVING,BURT] = 8+16+124+497 = 645
        MULTIPLY 2 -> 1290
        WELLNESS_CHECK 497 > ALL OF [8, 16, 124] -> true -> reset       => 0
```

`3.json` exercises the whole language: shifts, both operand forms, both quantifiers, `CONDITIONAL_ADD`, `WELLNESS_CHECK`, and a five-node graph. If `3.json` passes, the implementation is essentially done.

---

## Design Review — SOLID, without the ceremony

A pass over the design before any code exists. Each item is either a change this plan makes or a temptation it explicitly declines.

### Single Responsibility

**The one real violation, and the fix.** In the first draft, `Registry` did three unrelated jobs: store Cells, track who-waits-on-whom, and run the cycle-detection algorithm. That is why Task 13 read like a rewrite of Task 12 rather than an addition to it. Split into three:

| Type | Responsibility | Needs a lock? | Needs threads to test? |
|---|---|---|---|
| `Registry` | own the Cells, hand them out | yes (shared `Condition`) | yes |
| `WaitGraph` | plain data: AND-edges and OR-groups | no — caller holds the lock | **no** |
| `DeadlockDetector` | pure function: graph → cycle members | no | **no** |

The payoff is immediate: `DeadlockDetector` becomes a pure function over a hand-built `WaitGraph`, so every cycle case — two-node, three-node, self-reference, overlapping SCCs, OR-escape — is unit-tested with **zero threads**. Concurrency tests then only have to prove the edges get registered correctly, not that the algorithm is right.

Also split: `cell.py` was holding `Result` + `Cell` + `Registry` + deadlock. Now four files.

### Open/Closed

Three if-chains in the draft close a function against extension. Each becomes a table:

| Draft | Replacement | Adding a case now means |
|---|---|---|
| `_step`'s `isinstance` chain (`interp.py`) | `HANDLERS: dict[type[Instruction], Handler]` | register a handler |
| `parse_simple`'s opcode `if`-chain | `OPCODES: dict[str, ParseFn]` | add a dict entry |
| CLI's `run_concurrent if mode == ... else run_serial` | `RUNNERS: dict[str, type[Runner]]` | add a dict entry; `--mode` choices derive from the keys |

**What this plan does *not* do:** give each instruction an `execute()` method. That would put behaviour on the ISA models and couple the parser's output to the interpreter, collapsing a seam that is currently clean. A dispatch table gets the same OCP benefit and keeps instructions as inert data. It also does not split the nine handlers into nine files — they are ten lines each and share `_State`; one `handlers.py` is right.

### Liskov

Two places where substitutability is load-bearing, one of them a live hazard:

- **The pydantic base-class trap.** In pydantic v2, a field annotated with a base model coerces a subclass instance *down to the base, silently discarding subclass fields*. `Shift.body: tuple[Instruction, ...]` would turn every `Add` inside a shift into a bare `Instruction` and drop its operand. Annotate with the **union** `AnyInstruction`, never the base. This is checked by a test in Task 2 — it is the single most likely way the pydantic migration breaks something quietly.
- **`SerialResolver` vs `ConcurrentResolver` must be behaviourally, not just structurally, substitutable.** Same input, same output — different waiting. That is exactly what S8 states and what Task 16 asserts across 200 seeds. Framed correctly: **the determinism test *is* the LSP test for the `Resolver` hierarchy.**

### Interface Segregation

`Resolver` has four methods and every implementation genuinely needs all four — no fat interface. The live temptation is to add `is_cancelled()` to it, since Task 12 needs cancellation. Don't: cancellation is a concurrency concept, `SerialResolver` would have to stub it, and the interpreter would gain a concurrency-shaped hole. Cancellation surfaces as an exception raised *through* the existing methods instead.

### Dependency Inversion

The draft already had the important one — `interp` depends on the `Resolver` abstraction, not on `Registry` or `threading`. Extended in two more places: `Runner` depends on `DeadlockDetector`, not on a concrete algorithm; `cli` depends on `Runner`, not on either concrete runner.

Import direction is one-way and enforced by a test in Task 7:

```
cli -> runners -> resolvers -> cell/waitgraph/deadlock
                     \
interp --------------> resolvers/base   (and isa, errors — nothing else)
```

`lumon/interp/` may import `isa`, `errors`, and `resolvers.base`. Never `cell`, `runners`, or `threading`.

### Where abstraction does not go

Declining these is as much a design decision as adding the others. No ABC, no base class, one implementation each:

- **`Cell`** — one implementation, no plausible second.
- **`Registry`, `WaitGraph`** — data holders, not policy.
- **`Parser`, `Loader`** — one input format. A `YamlLoader` is YAGNI until someone asks.
- **Individual instruction types** — `Instruction` is already the shared root; a second layer buys nothing.
- **No factory for `Cell`, no strategy object for arithmetic, no DI container.** The three `base.py` files are the whole abstraction budget.

---

## File Structure

```
lumon/
  __init__.py
  errors.py            exception hierarchy — every failure mode in one place
  isa.py               pydantic ISA: Const/Ref/RefList, Condition, Instruction
  parser.py            schedule text -> Program; OPCODES dispatch table
  loader.py            pydantic input models -> list[Innie]; ref validation
  cell.py              Result, Cell, Registry  (no deadlock logic)
  waitgraph.py         WaitGraph — plain data, no lock, no threads
  interp/
    __init__.py        re-exports execute, evaluate, Outcome, HANDLERS
    engine.py          execute(program, resolver) -> Outcome; NO threading
    handlers.py        HANDLERS dispatch table, one function per instruction
    state.py           State — mutable accumulator + staging slot
  resolvers/
    base.py            Resolver ABC — the seam
    concurrent.py      ConcurrentResolver: blocks on the Registry condition
    serial.py          SerialResolver: lazy recursive, memoized
  deadlock/
    base.py            DeadlockDetector ABC — WaitGraph -> cycle members
    scc.py             SccDetector: AND-waits only (Task 12)
    andor.py           AndOrDetector: stuck-set fixpoint + SCC (Task 13)
  runners/
    base.py            Runner ABC — list[Innie] -> Registry; RUNNERS table
    concurrent.py      thread-per-Innie, watchdog
    serial.py          single-threaded reference oracle
  cli.py               argparse; JSON in, JSON out; --mode from RUNNERS keys
  __main__.py          `python -m lumon <schedule.json>`
tests/
  conftest.py          DictResolver fake, jitter fixture
  corpus.py            random-DAG schedule generator for the fuzz test
  test_isa.py
  test_parser.py
  test_loader.py
  test_interp.py
  test_cell.py
  test_waitgraph.py    detectors against hand-built graphs — no threads
  test_deadlock.py     detection through the real scheduler
  test_scheduler.py
  test_serial.py
  test_golden.py       1.json / 2.json / 3.json
  test_determinism.py  concurrent vs serial over the fuzz corpus
  test_cli.py
```

**The one rule that matters:** `lumon/interp/` must never import `threading`, `cell`, or `runners`. Enforced by a test (Task 7, Step 1). Every concurrency concern lives behind `resolvers/base.py`.

### Where pydantic does not go

`Cell`, `Registry`, and the resolvers are **plain classes**. They are mutable, they hold locks and thread state, and they are constructed internally — validation buys nothing and `BaseModel` fights `threading.Condition` as an attribute. Pydantic is for *values*: things that are frozen, compared, and either crossed a trust boundary or benefit from construction-time checking.

| Pydantic `BaseModel` | Plain class |
|---|---|
| `isa.py` — every node, frozen | `Cell`, `Registry`, `WaitGraph` |
| `loader.py` — `InnieSpec`, `WorkSchedule` (untrusted JSON) | `ConcurrentResolver`, `SerialResolver` |
| `Result`, `Outcome` | the detectors and runners (behaviour, not data) |

---

# Task 1: Skeleton, test loop, error hierarchy

**Files:**
- Create: `pyproject.toml`, `lumon/__init__.py`, `lumon/errors.py`, `tests/__init__.py`
- Test: `tests/test_errors.py`

**Interfaces:**
- Produces: the exception hierarchy every later task raises.

- [ ] **Step 1: Initialize the repo and install pytest**

The project is not currently a git repo. Run:

```bash
cd "C:/Users/barak/Projects/frame_assigment"
git init
python -m pip install "pydantic>=2.7" pytest
printf '__pycache__/\n.pytest_cache/\n*.pyc\n' > .gitignore
```

Confirm pydantic v2, not v1 — the model config syntax below is v2-only:

```bash
python -c "import pydantic; print(pydantic.VERSION)"
```
Expected: `2.x`

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "lumon"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pydantic>=2.7"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --timeout-method=thread"
```

Drop the `addopts` line if `pytest-timeout` is not installed; the watchdog in Task 16 covers hangs regardless.

- [ ] **Step 3: Write the failing test**

```python
# tests/test_errors.py
import pytest
from lumon.errors import (
    LumonError, ParseError, ScheduleError, UnknownInnieError,
    ArithmeticFault, NoWorkProduct, DependencyFaulted,
    DoubleSettle, Cancelled, WatchdogTimeout,
)

def test_all_errors_share_a_root():
    for cls in (ParseError, ScheduleError, UnknownInnieError,
                ArithmeticFault, NoWorkProduct, DependencyFaulted,
                DoubleSettle, Cancelled, WatchdogTimeout):
        assert issubclass(cls, LumonError)

def test_parse_error_reports_line_number():
    err = ParseError("bad opcode", line=7)
    assert err.line == 7
    assert "line 7" in str(err)
```

- [ ] **Step 4: Run it and confirm it fails**

Run: `python -m pytest tests/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.errors'`

- [ ] **Step 5: Implement `lumon/errors.py`**

```python
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
        super().__init__(
            f"{referrer} line {line}: references unknown Innie {unknown!r}"
        )
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
    detector, not a fallback — see Task 16.
    """
```

- [ ] **Step 6: Run tests and confirm they pass**

Run: `python -m pytest tests/test_errors.py -v`
Expected: 2 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore lumon/ tests/
git commit -m "feat: project skeleton and error hierarchy"
```

---

# Task 2: The ISA — instruction, operand, and condition types

**Files:**
- Create: `lumon/isa.py`
- Test: `tests/test_isa.py`

**Interfaces:**
- Produces: `CmpOp`, `Quantifier`, `Const`, `Ref`, `RefList`, `Operand`, `Comparand`, `Condition`, the `Instruction` subclasses, `AnyInstruction`, `Program`. The parser builds these; the interpreter consumes them.

All models are frozen `BaseModel`s. Two pydantic decisions worth stating before the code:

- **Positional construction is gone.** `BaseModel.__init__` is keyword-only, so `Const(value=1)` becomes `Const(value=1)` throughout. Every call site in this plan already uses keywords where it matters; the ones that did not are updated.
- **Never annotate a field with the `Instruction` base class.** Pydantic v2 validates a subclass instance *against the annotated model* and rebuilds it as that model, silently dropping subclass fields. `Shift.body: tuple[Instruction, ...]` would reduce every nested `Add` to a bare `Instruction` and lose its operand. Annotate with the `AnyInstruction` union instead. Step 1 pins this with a test, because it fails quietly rather than loudly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_isa.py
import pytest
from pydantic import ValidationError
from lumon.isa import (
    Const, Ref, RefList, Condition, CmpOp, Quantifier,
    Load, Add, Multiply, Modulo, Waffle, WellnessCheck,
    ConditionalAdd, Shift,
)


def test_nodes_are_frozen():
    const = Const(value=1)
    with pytest.raises(ValidationError):
        const.value = 2


def test_nodes_are_hashable_and_compare_by_value():
    assert Const(value=5) == Const(value=5)
    assert len({Ref(innie_id="A"), Ref(innie_id="A")}) == 1


def test_const_and_ref_carry_their_payload():
    assert Const(value=5).value == 5
    assert Ref(innie_id="HELLY").innie_id == "HELLY"
    assert RefList(innie_ids=("HELLY", "MARK")).innie_ids == ("HELLY", "MARK")


def test_bad_payload_is_rejected_at_construction():
    with pytest.raises(ValidationError):
        Const(value="not an int")
    with pytest.raises(ValidationError):
        Shift(times=-1, body=(), line=1)      # parser rejects too; belt and braces


def test_refs_collects_referenced_ids():
    assert Const(value=3).refs() == ()
    assert Ref(innie_id="HELLY").refs() == ("HELLY",)
    assert RefList(innie_ids=("A", "B")).refs() == ("A", "B")


def test_condition_refs_include_lhs_and_quantified_list():
    cond = Condition(
        lhs=Ref(innie_id="HELLY"), op=CmpOp.GT,
        quantifier=Quantifier.ANY, rhs=RefList(innie_ids=("MARK", "IRVING")),
    )
    assert set(cond.refs()) == {"HELLY", "MARK", "IRVING"}


def test_shift_holds_a_nested_body():
    inner = Shift(times=2, body=(Add(operand=Const(value=1), line=3),), line=2)
    outer = Shift(times=3, body=(inner,), line=1)
    assert outer.body[0].body[0].operand.value == 1


def test_shift_body_does_not_coerce_children_to_the_base_class():
    """The pydantic v2 LSP trap. If `body` were annotated
    `tuple[Instruction, ...]`, pydantic would rebuild this Add as a bare
    Instruction and `.operand` would vanish. Annotating with the
    AnyInstruction union preserves the concrete type."""
    shift = Shift(times=1, body=(Add(operand=Const(value=7), line=2),), line=1)
    assert type(shift.body[0]) is Add
    assert shift.body[0].operand == Const(value=7)


def test_deeply_nested_shifts_keep_their_types():
    prog = Shift(times=1, body=(
        Shift(times=1, body=(
            Multiply(operand=RefList(innie_ids=("A",)), line=3),
        ), line=2),
    ), line=1)
    assert type(prog.body[0].body[0]) is Multiply
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_isa.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.isa'`

- [ ] **Step 3: Implement `lumon/isa.py`**

```python
"""Instruction set: frozen pydantic models the parser builds and interp walks.

Data only — no execution behaviour lives here. Execution is a dispatch
table in `lumon/interp/handlers.py`, which keeps the ISA usable by the
parser, the loader's validation pass, and the interpreter without any of
them depending on each other.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Union

from pydantic import BaseModel, ConfigDict, Field


class _Node(BaseModel):
    """Shared config for every ISA node: immutable and hashable, so nodes
    compare by value and can live in sets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def refs(self) -> tuple[str, ...]:
        """Every Innie ID this node mentions. Overridden where relevant."""
        return ()


class CmpOp(str, Enum):
    GT = ">"
    LT = "<"
    GE = ">="
    LE = "<="
    EQ = "=="
    NE = "!="

    def apply(self, left: int, right: int) -> bool:
        return {
            CmpOp.GT: left > right,
            CmpOp.LT: left < right,
            CmpOp.GE: left >= right,
            CmpOp.LE: left <= right,
            CmpOp.EQ: left == right,
            CmpOp.NE: left != right,
        }[self]


class Quantifier(str, Enum):
    NONE = "NONE"   # plain comparison against a single operand
    ANY = "ANY"     # ANY OF [...]
    ALL = "ALL"     # ALL OF [...]


# ---------------------------------------------------------------- operands

class Const(_Node):
    value: int


class Ref(_Node):
    innie_id: str

    def refs(self) -> tuple[str, ...]:
        return (self.innie_id,)


class RefList(_Node):
    innie_ids: tuple[str, ...]

    def refs(self) -> tuple[str, ...]:
        return self.innie_ids


Operand = Union[Const, Ref, RefList]     # ADD / MULTIPLY / MODULO argument
Comparand = Union[Const, Ref]            # condition LHS, or non-quantified RHS


# --------------------------------------------------------------- condition

class Condition(_Node):
    lhs: Comparand
    op: CmpOp
    quantifier: Quantifier
    rhs: Union[Const, Ref, RefList]

    def refs(self) -> tuple[str, ...]:
        return self.lhs.refs() + self.rhs.refs()


# ------------------------------------------------------------ instructions

class Instruction(_Node):
    """Root of the instruction hierarchy.

    NEVER annotate a field with this type — see AnyInstruction below.
    """

    line: int = Field(ge=1)


class Load(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Add(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Multiply(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Modulo(Instruction):
    operand: Operand

    def refs(self) -> tuple[str, ...]:
        return self.operand.refs()


class Waffle(Instruction):
    pass


class WellnessCheck(Instruction):
    condition: Condition | None = None      # None == bare WELLNESS_CHECK

    def refs(self) -> tuple[str, ...]:
        return self.condition.refs() if self.condition else ()


class ConditionalAdd(Instruction):
    targets: RefList
    condition: Condition

    def refs(self) -> tuple[str, ...]:
        return self.targets.refs() + self.condition.refs()


class Shift(Instruction):
    times: int = Field(ge=0)
    # MUST be the union, not `tuple[Instruction, ...]`. Annotating with the
    # base class makes pydantic rebuild every child AS Instruction, dropping
    # `operand`, `condition`, and nested bodies. Pinned by a test.
    body: tuple["AnyInstruction", ...]

    def refs(self) -> tuple[str, ...]:
        out: tuple[str, ...] = ()
        for instr in self.body:
            out += instr.refs()
        return out


AnyInstruction = Annotated[
    Union[Load, Add, Multiply, Modulo, Waffle, WellnessCheck,
          ConditionalAdd, Shift],
    Field(union_mode="left_to_right"),
]

Shift.model_rebuild()          # resolves the forward reference in `body`

Program = tuple[AnyInstruction, ...]


def all_refs(program: Program) -> tuple[str, ...]:
    """Every Innie ID mentioned anywhere in a program, including inside
    shifts and conditions. Used only for load-time validation (S11) —
    never for dependency ordering, which is dynamic (S6)."""
    out: tuple[str, ...] = ()
    for instr in program:
        out += instr.refs()
    return out
```

Three pydantic details that matter here:

- **`union_mode="left_to_right"`** makes pydantic try union members in declaration order and take the first that validates, instead of "smart" mode's best-match scoring. `Waffle` has only `line`, so under smart mode it can win against a richer sibling. Left-to-right with `extra="forbid"` makes the choice predictable.
- **`extra="forbid"`** turns a typo'd field name into a `ValidationError` instead of a silently ignored attribute.
- **`Shift.model_rebuild()`** is required because `body` forward-references `AnyInstruction`, which is defined after the class.

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_isa.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add lumon/isa.py tests/test_isa.py
git commit -m "feat: pydantic instruction set"
```

---

# Task 3: Parser — basic opcodes and the three operand forms

**Files:**
- Create: `lumon/parser.py`
- Test: `tests/test_parser.py`

**Interfaces:**
- Consumes: `lumon.isa` (Task 2), `lumon.errors.ParseError` (Task 1).
- Produces: `parse(text: str) -> Program`. Also `parse_operand(token: str, line: int) -> Operand` for reuse in Tasks 4 and 5.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parser.py
import pytest
from lumon.errors import ParseError
from lumon.isa import Load, Add, Multiply, Modulo, Waffle, Const, Ref, RefList
from lumon.parser import parse


def test_blank_lines_and_comments_are_stripped():
    prog = parse("LOAD 1\n\n  # a comment\nWAFFLE\n")
    assert len(prog) == 2
    assert isinstance(prog[0], Load)
    assert isinstance(prog[1], Waffle)


def test_literal_operand():
    (instr,) = parse("LOAD 10")
    assert instr.operand == Const(value=10)
    assert instr.line == 1


def test_negative_literal():
    (instr,) = parse("ADD -5")
    assert instr.operand == Const(value=-5)


def test_bare_ref_operand():
    (instr,) = parse("ADD HELLY")
    assert instr.operand == Ref(innie_id="HELLY")


def test_ref_list_operand_tolerates_whitespace():
    (instr,) = parse("MULTIPLY [ HELLY ,MARK,  IRVING ]")
    assert instr.operand == RefList(innie_ids=("HELLY", "MARK", "IRVING"))


def test_modulo_parses():
    (instr,) = parse("MODULO 7")
    assert isinstance(instr, Modulo)
    assert instr.operand == Const(value=7)


def test_waffle_takes_no_operand():
    with pytest.raises(ParseError) as exc:
        parse("WAFFLE 5")
    assert exc.value.line == 1


def test_unknown_opcode_reports_line():
    with pytest.raises(ParseError) as exc:
        parse("LOAD 1\nFROLIC 2\nWAFFLE")
    assert exc.value.line == 2
    assert "FROLIC" in str(exc.value)


def test_missing_operand_reports_line():
    with pytest.raises(ParseError) as exc:
        parse("LOAD")
    assert exc.value.line == 1


def test_empty_ref_list_is_an_error():
    with pytest.raises(ParseError):
        parse("ADD []")


def test_malformed_bracket_is_an_error():
    with pytest.raises(ParseError):
        parse("ADD [HELLY, MARK")


def test_opcodes_are_case_insensitive_but_ids_are_not():
    (instr,) = parse("add HELLY")
    assert isinstance(instr, Add)
    assert instr.operand == Ref(innie_id="HELLY")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.parser'`

- [ ] **Step 3: Implement `lumon/parser.py`**

```python
"""Schedule text -> Program. All validation that can happen without the
full Innie list happens here, with line numbers (S11 handles the rest)."""

from __future__ import annotations

import re
from typing import Callable

from lumon.errors import ParseError
from lumon.isa import (
    Add, Const, Instruction, Load, Modulo, Multiply, Operand,
    Program, Ref, RefList, Waffle,
)

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INT_RE = re.compile(r"^-?\d+$")


def parse_operand(token: str, line: int) -> Operand:
    """Const | Ref | RefList (S3)."""
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


# OPCODES is the extension point: a new work order is a new entry here and
# a new handler, never an edit to a growing if-chain (OCP).
ParseFn = Callable[[str, int], Instruction]
OPCODES: dict[str, ParseFn] = {}


def opcode(name: str) -> Callable[[ParseFn], ParseFn]:
    def register(fn: ParseFn) -> ParseFn:
        OPCODES[name] = fn
        return fn
    return register


def _arithmetic(node: type[Instruction], name: str) -> ParseFn:
    def parse_fn(rest: str, line: int) -> Instruction:
        if not rest:
            raise ParseError(f"{name} requires an operand", line)
        return node(operand=parse_operand(rest, line), line=line)
    return parse_fn


for _name, _node in (("LOAD", Load), ("ADD", Add),
                     ("MULTIPLY", Multiply), ("MODULO", Modulo)):
    OPCODES[_name] = _arithmetic(_node, _name)


@opcode("WAFFLE")
def _parse_waffle(rest: str, line: int) -> Instruction:
    if rest:
        raise ParseError("WAFFLE takes no operand", line)
    return Waffle(line=line)


def parse_simple(opcode_name: str, rest: str, line: int) -> Instruction:
    """Dispatch one non-block instruction. SHIFT/END_SHIFT are handled by
    the block parser in Task 5, so they never reach here."""
    try:
        parse_fn = OPCODES[opcode_name]
    except KeyError:
        raise ParseError(f"unknown work order {opcode_name!r}", line) from None
    return parse_fn(rest, line)


def split_opcode(text: str) -> tuple[str, str]:
    head, _, rest = text.partition(" ")
    return head.upper(), rest.strip()


def parse(text: str) -> Program:
    """Parse a full schedule. Extended with conditions in Task 4 and
    SHIFT blocks in Task 5."""
    out: list[Instruction] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = strip_line(raw)
        if not stripped:
            continue
        opcode_name, rest = split_opcode(stripped)
        out.append(parse_simple(opcode_name, rest, lineno))
    return tuple(out)
```

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_parser.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add lumon/parser.py tests/test_parser.py
git commit -m "feat: parse basic opcodes and the three operand forms"
```

---

# Task 4: Parser — conditions, WELLNESS_CHECK, CONDITIONAL_ADD

**Files:**
- Modify: `lumon/parser.py`
- Modify: `tests/test_parser.py`

**Interfaces:**
- Consumes: `parse_operand` (Task 3), `Condition`/`CmpOp`/`Quantifier` (Task 2).
- Produces: `parse_condition(text: str, line: int) -> Condition`; `parse` now handles `WELLNESS_CHECK` and `CONDITIONAL_ADD`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_parser.py
from lumon.isa import (
    Condition, CmpOp, Quantifier, WellnessCheck, ConditionalAdd,
)
from lumon.parser import parse_condition


def test_simple_comparison():
    cond = parse_condition("HELLY > 5", 1)
    assert cond == Condition(lhs=Ref(innie_id="HELLY"), op=CmpOp.GT,
                             quantifier=Quantifier.NONE, rhs=Const(value=5))


def test_two_character_operator_wins_over_one():
    cond = parse_condition("MARK <= 10", 1)
    assert cond.op is CmpOp.LE
    assert cond.rhs == Const(value=10)


def test_all_comparison_operators():
    for text, op in [("A > 1", CmpOp.GT), ("A < 1", CmpOp.LT),
                     ("A >= 1", CmpOp.GE), ("A <= 1", CmpOp.LE),
                     ("A == 1", CmpOp.EQ), ("A != 1", CmpOp.NE)]:
        assert parse_condition(text, 1).op is op


def test_any_of_quantifier():
    cond = parse_condition("HELLY > ANY OF [MARK, IRVING]", 1)
    assert cond.quantifier is Quantifier.ANY
    assert cond.rhs == RefList(innie_ids=("MARK", "IRVING"))


def test_all_of_quantifier():
    cond = parse_condition("IRVING > ALL OF [HELLY, MARK]", 1)
    assert cond.quantifier is Quantifier.ALL


def test_literal_lhs_is_allowed():
    cond = parse_condition("5 > MARK", 1)
    assert cond.lhs == Const(value=5)


def test_ref_list_lhs_is_rejected():
    with pytest.raises(ParseError):
        parse_condition("[A, B] > 5", 1)


def test_bare_wellness_check():
    (instr,) = parse("WELLNESS_CHECK")
    assert isinstance(instr, WellnessCheck)
    assert instr.condition is None


def test_conditional_wellness_check():
    (instr,) = parse("WELLNESS_CHECK IRVING > ALL OF [HELLY, MARK]")
    assert instr.condition.quantifier is Quantifier.ALL


def test_conditional_add():
    (instr,) = parse("CONDITIONAL_ADD [HELLY, MARK] IF MARK > 10")
    assert isinstance(instr, ConditionalAdd)
    assert instr.targets == RefList(innie_ids=("HELLY", "MARK"))
    assert instr.condition.lhs == Ref(innie_id="MARK")


def test_conditional_add_without_if_is_an_error():
    with pytest.raises(ParseError) as exc:
        parse("CONDITIONAL_ADD [HELLY]")
    assert "IF" in str(exc.value)


def test_condition_without_operator_is_an_error():
    with pytest.raises(ParseError):
        parse_condition("HELLY MARK", 1)


def test_quantifier_without_brackets_is_an_error():
    with pytest.raises(ParseError):
        parse_condition("HELLY > ANY OF MARK", 1)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_parser.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_condition'`

- [ ] **Step 3: Extend `lumon/parser.py`**

Add these imports and functions, and replace `parse_simple`'s final `raise`:

```python
from lumon.isa import (
    CmpOp, Comparand, Condition, ConditionalAdd, Quantifier, WellnessCheck,
)

# Two-character operators must be tried first, or ">=" parses as ">".
_OPERATORS = ("<=", ">=", "==", "!=", "<", ">")

_QUANTIFIERS = {"ANY OF": Quantifier.ANY, "ALL OF": Quantifier.ALL}


def _parse_comparand(token: str, line: int) -> Comparand:
    operand = parse_operand(token, line)
    if isinstance(operand, RefList):
        raise ParseError(
            "a reference list cannot appear on this side of a comparison", line
        )
    return operand


def parse_condition(text: str, line: int) -> Condition:
    """`<comparand> OP <comparand> | ANY OF [...] | ALL OF [...]`  (S7)."""
    text = text.strip()
    for symbol in _OPERATORS:
        idx = text.find(symbol)
        if idx == -1:
            continue
        lhs_text, rhs_text = text[:idx], text[idx + len(symbol):]
        lhs = _parse_comparand(lhs_text, line)
        rhs_text = rhs_text.strip()

        for keyword, quant in _QUANTIFIERS.items():
            if rhs_text.upper().startswith(keyword):
                list_text = rhs_text[len(keyword):].strip()
                if not list_text.startswith("["):
                    raise ParseError(
                        f"{keyword} must be followed by a [list]", line
                    )
                rhs = parse_operand(list_text, line)
                if not isinstance(rhs, RefList):
                    raise ParseError(f"{keyword} requires a [list]", line)
                return Condition(lhs=lhs, op=CmpOp(symbol),
                                 quantifier=quant, rhs=rhs)

        return Condition(
            lhs=lhs, op=CmpOp(symbol), quantifier=Quantifier.NONE,
            rhs=_parse_comparand(rhs_text, line),
        )

    raise ParseError(f"no comparison operator in condition {text!r}", line)
```

Now register the two new work orders. Note that `parse_simple` is **not
touched** — that is the point of the `OPCODES` table from Task 3:

```python
@opcode("WELLNESS_CHECK")
def _parse_wellness_check(rest: str, line: int) -> Instruction:
    condition = parse_condition(rest, line) if rest else None
    return WellnessCheck(condition=condition, line=line)


@opcode("CONDITIONAL_ADD")
def _parse_conditional_add(rest: str, line: int) -> Instruction:
    # Split on the LAST " IF " so a condition containing IF-like text
    # in an ID cannot confuse the split.
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
```

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_parser.py -v`
Expected: 25 passed

- [ ] **Step 5: Commit**

```bash
git add lumon/parser.py tests/test_parser.py
git commit -m "feat: parse conditions, WELLNESS_CHECK, CONDITIONAL_ADD"
```

---

# Task 5: Parser — SHIFT blocks with nesting

**Files:**
- Modify: `lumon/parser.py` (rewrite `parse` as a recursive block parser)
- Modify: `tests/test_parser.py`

**Interfaces:**
- Produces: `parse` returns a nested `Program` where `Shift.body` is a sub-`Program` (S12).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_parser.py
from lumon.isa import Shift


def test_shift_wraps_its_body():
    prog = parse("LOAD 10\nSHIFT 2 TIMES\nADD 5\nEND_SHIFT\nWAFFLE")
    assert len(prog) == 3
    shift = prog[1]
    assert isinstance(shift, Shift)
    assert shift.times == 2
    assert len(shift.body) == 1
    assert shift.body[0].operand == Const(value=5)


def test_shifts_nest():
    prog = parse(
        "SHIFT 3 TIMES\n"
        "  SHIFT 2 TIMES\n"
        "    ADD 1\n"
        "  END_SHIFT\n"
        "  MULTIPLY 2\n"
        "END_SHIFT"
    )
    outer = prog[0]
    assert outer.times == 3
    assert len(outer.body) == 2
    assert isinstance(outer.body[0], Shift)
    assert outer.body[0].times == 2


def test_shift_zero_times_is_legal():
    prog = parse("SHIFT 0 TIMES\nADD 1\nEND_SHIFT")
    assert prog[0].times == 0
    assert len(prog[0].body) == 1


def test_negative_shift_count_is_an_error():
    with pytest.raises(ParseError):
        parse("SHIFT -1 TIMES\nADD 1\nEND_SHIFT")


def test_shift_without_times_keyword_is_an_error():
    with pytest.raises(ParseError) as exc:
        parse("SHIFT 2\nADD 1\nEND_SHIFT")
    assert "TIMES" in str(exc.value)


def test_unclosed_shift_reports_the_opening_line():
    with pytest.raises(ParseError) as exc:
        parse("LOAD 1\nSHIFT 2 TIMES\nADD 5")
    assert exc.value.line == 2
    assert "END_SHIFT" in str(exc.value)


def test_unmatched_end_shift_is_an_error():
    with pytest.raises(ParseError) as exc:
        parse("LOAD 1\nEND_SHIFT")
    assert exc.value.line == 2


def test_waffle_inside_a_shift_parses():
    prog = parse("LOAD 1\nSHIFT 3 TIMES\nMULTIPLY 2\nWAFFLE\nEND_SHIFT")
    assert len(prog[1].body) == 2
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_parser.py -k shift -v`
Expected: FAIL — `SHIFT` hits `unknown work order 'SHIFT'`

- [ ] **Step 3: Replace `parse` in `lumon/parser.py`**

```python
from lumon.isa import Shift

_SHIFT_RE = re.compile(r"^(-?\d+)\s+TIMES$", re.IGNORECASE)


def _parse_block(
    lines: list[tuple[int, str]], pos: int, opened_at: int | None
) -> tuple[Program, int]:
    """Parse instructions until END_SHIFT (or EOF at the top level).

    `opened_at` is the line of the SHIFT that opened this block, or None
    at the top level. Returns (program, next_position).
    """
    out: list[Instruction] = []
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
                raise ParseError(
                    "SHIFT must read 'SHIFT <number> TIMES'", lineno
                )
            times = int(match.group(1))
            if times < 0:
                raise ParseError(
                    f"SHIFT count must not be negative (got {times})", lineno
                )
            body, pos = _parse_block(lines, pos + 1, opened_at=lineno)
            out.append(Shift(times=times, body=body, line=lineno))
            continue

        out.append(parse_simple(opcode_name, rest, lineno))
        pos += 1

    if opened_at is not None:
        raise ParseError("SHIFT is never closed by END_SHIFT", opened_at)
    return tuple(out), pos


def parse(text: str) -> Program:
    lines = [
        (lineno, stripped)
        for lineno, raw in enumerate(text.splitlines(), start=1)
        if (stripped := strip_line(raw))
    ]
    program, _ = _parse_block(lines, 0, opened_at=None)
    return program
```

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_parser.py -v`
Expected: 33 passed

- [ ] **Step 5: Parse all three sample schedules as a smoke test**

```python
# append to tests/test_parser.py
import json
import pathlib

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"


@pytest.mark.parametrize("name", ["1.json", "2.json", "3.json"])
def test_sample_schedules_parse(name):
    data = json.loads((SAMPLES / name).read_text())
    for innie in data["innies"]:
        assert parse(innie["schedule"])
```

Run: `python -m pytest tests/test_parser.py -v`
Expected: 36 passed

- [ ] **Step 6: Commit**

```bash
git add lumon/parser.py tests/test_parser.py
git commit -m "feat: parse nested SHIFT blocks; all sample schedules parse"
```

---

# Task 6: Loader — JSON to Innies, with unknown-reference validation

**Files:**
- Create: `lumon/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: `parse` (Task 5), `all_refs` (Task 2), `UnknownInnieError` (Task 1).
- Produces: `Innie(id: str, program: Program)`, `load(data: dict) -> list[Innie]`, `load_path(path) -> list[Innie]`. Input order is preserved and is the output order (S11, and the CLI in Task 15).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_loader.py
import json
import pathlib
import pytest
from lumon.errors import UnknownInnieError, ParseError, ScheduleError
from lumon.loader import Innie, load, load_path

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"


def test_load_preserves_input_order():
    innies = load({"innies": [
        {"id": "B", "schedule": "LOAD 1\nWAFFLE"},
        {"id": "A", "schedule": "LOAD 2\nWAFFLE"},
    ]})
    assert [i.id for i in innies] == ["B", "A"]


def test_unknown_reference_reports_referrer_and_line():
    with pytest.raises(UnknownInnieError) as exc:
        load({"innies": [
            {"id": "A", "schedule": "LOAD 1\nADD GHOST\nWAFFLE"},
        ]})
    assert exc.value.referrer == "A"
    assert exc.value.unknown == "GHOST"
    assert exc.value.line == 2


def test_unknown_reference_inside_a_shift_is_caught():
    with pytest.raises(UnknownInnieError):
        load({"innies": [
            {"id": "A", "schedule": "SHIFT 2 TIMES\nADD GHOST\nEND_SHIFT"},
        ]})


def test_unknown_reference_inside_a_condition_is_caught():
    with pytest.raises(UnknownInnieError):
        load({"innies": [
            {"id": "A", "schedule": "WELLNESS_CHECK A > ANY OF [GHOST]"},
        ]})


def test_self_reference_is_accepted_at_load_time():
    # S9: a self-reference is a one-node cycle resolved to -1 at runtime,
    # NOT a load-time error.
    innies = load({"innies": [
        {"id": "A", "schedule": "LOAD 5\nADD A\nWAFFLE"},
    ]})
    assert len(innies) == 1


def test_duplicate_ids_are_rejected():
    with pytest.raises(ScheduleError, match="duplicate"):
        load({"innies": [
            {"id": "A", "schedule": "WAFFLE"},
            {"id": "A", "schedule": "WAFFLE"},
        ]})


def test_missing_innies_key_is_rejected():
    with pytest.raises(ScheduleError):
        load({"workers": []})


def test_wrong_field_types_are_rejected_by_pydantic():
    with pytest.raises(ScheduleError):
        load({"innies": [{"id": 42, "schedule": "WAFFLE"}]})


def test_unexpected_field_is_rejected():
    with pytest.raises(ScheduleError):
        load({"innies": [{"id": "A", "schedule": "WAFFLE", "shift": "night"}]})


@pytest.mark.parametrize("name", ["1.json", "2.json", "3.json"])
def test_sample_files_load(name):
    innies = load_path(SAMPLES / name)
    assert innies
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_loader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.loader'`

- [ ] **Step 3: Implement `lumon/loader.py`**

```python
"""JSON work schedule -> validated Innies.

This is the trust boundary: the JSON is external input, so pydantic does
the shape validation and the module only has to do the cross-Innie checks
pydantic cannot express (duplicate ids, unknown references).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lumon.errors import ScheduleError, UnknownInnieError
from lumon.isa import Program, Shift
from lumon.parser import parse


# ------------------------------------------------------- input models (JSON)

class InnieSpec(BaseModel):
    """One entry of the incoming JSON. Shape only — the schedule string is
    still raw text at this point."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    schedule: str


class WorkSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    innies: list[InnieSpec] = Field(min_length=1)


# ------------------------------------------------------ parsed domain model

class Innie(BaseModel):
    """An Innie with its schedule parsed. Frozen: nothing mutates a program
    after load."""

    model_config = ConfigDict(frozen=True)

    id: str
    program: Program


def _walk(program: Program):
    """Yield every instruction, descending into SHIFT bodies."""
    for instr in program:
        yield instr
        if isinstance(instr, Shift):
            yield from _walk(instr.body)


def _validate_refs(innie: Innie, known: set[str]) -> None:
    """S11: every referenced ID must exist. Line numbers survive nesting
    because each Instruction carries its own `line`."""
    for instr in _walk(innie.program):
        for ref in instr.refs():
            if ref not in known:
                raise UnknownInnieError(innie.id, ref, instr.line)


def load(data: dict) -> list[Innie]:
    try:
        schedule = WorkSchedule.model_validate(data)
    except ValidationError as error:
        raise ScheduleError(f"malformed work schedule: {error}") from error

    innies: list[Innie] = []
    seen: set[str] = set()
    for spec in schedule.innies:
        if spec.id in seen:
            raise ScheduleError(f"duplicate Innie id {spec.id!r}")
        seen.add(spec.id)
        innies.append(Innie(id=spec.id, program=parse(spec.schedule)))

    for innie in innies:
        _validate_refs(innie, seen)
    return innies


def load_path(path: str | Path) -> list[Innie]:
    return load(json.loads(Path(path).read_text(encoding="utf-8")))
```

Two notes on the pydantic split here:

- **`InnieSpec`/`WorkSchedule` are input DTOs; `Innie` is the domain model.** Keeping them separate means the wire format can change without touching anything downstream, and `Innie.program` holds parsed instructions rather than a string.
- **`Program` is `tuple[AnyInstruction, ...]`**, so `Innie` validates its own program against the instruction union on construction. A parser bug that produces a malformed node fails here rather than three tasks later.

Note: `Instruction.refs()` on a `Shift` already recurses into its body, so `_walk` plus `refs()` double-covers nested instructions. That is harmless (validation is idempotent) but the line number reported for a nested bad ref comes from whichever instruction is visited first. `_walk` visits the inner instruction in its own right, so the precise line is always reported — which is what `test_unknown_reference_inside_a_shift_is_caught` relies on.

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_loader.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add lumon/loader.py tests/test_loader.py
git commit -m "feat: JSON loader with unknown-reference validation"
```

---

# Task 7: The Resolver seam and the interpreter — arithmetic only

**Files:**
- Create: `lumon/resolvers/__init__.py`, `lumon/resolvers/base.py`
- Create: `lumon/interp/__init__.py`, `lumon/interp/engine.py`, `lumon/interp/handlers.py`
- Test: `tests/test_interp.py`

**Interfaces:**
- Produces:
  - `Resolver` ABC (`lumon/resolvers/base.py`) with `value(id) -> int`, `values(ids) -> list[int]`, `any_of(ids, pred) -> bool`, `all_of(ids, pred) -> bool`.
  - `Outcome(staged: int | None)` — `None` means VOID (S2).
  - `execute(program, resolver) -> Outcome` and `evaluate(condition, resolver) -> bool`, re-exported from `lumon.interp`.
  - `HANDLERS: dict[type[Instruction], Handler]` — the OCP extension point for new work orders.

> **Why the seam is wider than `resolve: str -> int`.** The original draft plan used `resolve(task_id) -> int`. That signature cannot express short-circuit: it forces the interpreter to block on every reference one at a time, which defeats Requirement 4. Putting `any_of`/`all_of` on the resolver keeps *all* waiting strategy on the concurrency side, and makes the determinism invariant (S8) directly testable — the serial and concurrent resolvers must return the same boolean from the same call.

- [ ] **Step 1: Write the failing test, including the no-threading guard**

```python
# tests/test_interp.py
import pytest
from lumon.errors import ArithmeticFault
from lumon.interp import Outcome, execute
from lumon.parser import parse


class NoRefsResolver:
    """Every reference is a programming error at this stage."""
    def value(self, innie_id): raise AssertionError(f"unexpected ref {innie_id}")
    def values(self, ids): raise AssertionError(f"unexpected refs {ids}")
    def any_of(self, ids, pred): raise AssertionError("unexpected ANY OF")
    def all_of(self, ids, pred): raise AssertionError("unexpected ALL OF")


def run(text):
    return execute(parse(text), NoRefsResolver())


def test_interp_never_imports_concurrency_or_storage():
    """DIP guard. lumon/interp/ may import isa, errors, and resolvers.base.
    Nothing else — importing cell or runners would invert the dependency
    and make the interpreter untestable without threads."""
    import pathlib
    pkg = pathlib.Path(__file__).parent.parent / "lumon" / "interp"
    forbidden = ("threading", "lumon.cell", "lumon.runners",
                 "lumon.waitgraph", "lumon.deadlock")
    for module in pkg.glob("*.py"):
        src = module.read_text()
        for name in forbidden:
            assert f"import {name}" not in src, f"{module.name} imports {name}"
            assert f"from {name}" not in src, f"{module.name} imports {name}"


def test_load_add_multiply():
    assert run("LOAD 2\nADD 3\nMULTIPLY 4\nWAFFLE") == Outcome(staged=20)


def test_accumulator_starts_at_zero():
    assert run("ADD 7\nWAFFLE") == Outcome(staged=7)


def test_modulo_uses_python_semantics():
    assert run("LOAD -7\nMODULO 3\nWAFFLE") == Outcome(staged=2)


def test_modulo_zero_faults():
    with pytest.raises(ArithmeticFault):
        run("LOAD 5\nMODULO 0\nWAFFLE")


def test_last_waffle_wins():
    # S1 / 2.json: DYLAN stages 1 then 100; only 100 is published.
    assert run("LOAD 1\nWAFFLE\nLOAD 100\nWAFFLE") == Outcome(staged=100)


def test_no_waffle_yields_void():
    assert run("LOAD 42") == Outcome(staged=None)


def test_bare_wellness_check_resets_to_zero():
    assert run("LOAD 99\nWELLNESS_CHECK\nWAFFLE") == Outcome(staged=0)


def test_shift_repeats_the_body():
    # 1.json HELLY
    assert run("LOAD 10\nSHIFT 2 TIMES\nADD 5\nEND_SHIFT\nWAFFLE") == Outcome(20)


def test_shift_zero_times_skips_the_body():
    assert run("LOAD 5\nSHIFT 0 TIMES\nADD 100\nEND_SHIFT\nWAFFLE") == Outcome(5)


def test_nested_shifts():
    assert run(
        "LOAD 0\nSHIFT 3 TIMES\nSHIFT 2 TIMES\nADD 1\nEND_SHIFT\nEND_SHIFT\nWAFFLE"
    ) == Outcome(6)


def test_waffle_inside_a_shift_stages_each_iteration_last_wins():
    # 3.json HELLY
    assert run("LOAD 1\nSHIFT 3 TIMES\nMULTIPLY 2\nWAFFLE\nEND_SHIFT") == Outcome(8)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_interp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.interp'`

- [ ] **Step 3: Implement `lumon/resolvers/base.py`**

```python
"""The seam between the interpreter and the outside world.

Two implementations exist: ConcurrentResolver and SerialResolver. They
differ in *how long they wait*, never in *what they return* — that is the
S8 invariant, and Task 16 is the test that enforces it. In SOLID terms,
the determinism test IS the Liskov check for this hierarchy.

Deliberately NOT on this interface: anything about cancellation, cells, or
threads. Adding `is_cancelled()` here would force SerialResolver to carry a
meaningless stub and leak concurrency into the interpreter (ISP).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Sequence


class Resolver(ABC):
    @abstractmethod
    def value(self, innie_id: str) -> int:
        """Wait for one Innie and return its published value."""

    @abstractmethod
    def values(self, innie_ids: Sequence[str]) -> list[int]:
        """Wait for ALL of these Innies. Result order matches `innie_ids`."""

    @abstractmethod
    def any_of(
        self, innie_ids: Sequence[str], pred: Callable[[int], bool]
    ) -> bool:
        """True if `pred` holds for ANY of these Innies' values.
        May stop waiting once one satisfies it (`true` is absorbing)."""

    @abstractmethod
    def all_of(
        self, innie_ids: Sequence[str], pred: Callable[[int], bool]
    ) -> bool:
        """True if `pred` holds for ALL of these Innies' values.
        May stop waiting once one fails it (`false` is absorbing)."""
```

```python
# lumon/resolvers/__init__.py
from lumon.resolvers.base import Resolver

__all__ = ["Resolver"]
```

Concrete resolvers ship with the runners that need them: `concurrent.py` in
Task 10, `serial.py` in Task 14. Importing them here would create a cycle
(`resolvers` -> `cell` -> ...), so `__init__` exports only the base.

- [ ] **Step 4: Implement `lumon/interp/state.py` and `lumon/interp/handlers.py`**

```python
# lumon/interp/state.py
"""Mutable per-workday state. Deliberately a plain class, not a model: it
is mutated once per instruction and never validated, compared, or frozen."""

from __future__ import annotations


class State:
    __slots__ = ("acc", "staged")

    def __init__(self) -> None:
        self.acc: int = 0            # S13
        self.staged: int | None = None
```

One handler function per instruction, registered in a table. Adding a work
order means adding a handler — never editing a dispatch chain (OCP).

```python
# lumon/interp/handlers.py
"""Execution behaviour for each instruction type.

Instructions stay inert data in isa.py; behaviour lives here. That keeps
the parser's output usable by the loader's validation pass without
dragging execution along with it.
"""

from __future__ import annotations

import math
from typing import Callable, TypeVar

from lumon.errors import ArithmeticFault
from lumon.interp.state import State
from lumon.isa import (
    Add, Comparand, Condition, ConditionalAdd, Const, Instruction, Load,
    Modulo, Multiply, Quantifier, Ref, Shift, Waffle, WellnessCheck,
)
from lumon.resolvers.base import Resolver

Handler = Callable[[Instruction, State, Resolver], None]
HANDLERS: dict[type[Instruction], Handler] = {}

I = TypeVar("I", bound=Instruction)


def handles(node: type[I]) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[node] = fn
        return fn
    return register


# ------------------------------------------------------------- operand help

def operand_values(operand, resolver: Resolver) -> list[int]:
    """S3: an operand contributes one value (Const/Ref) or many (RefList)."""
    if isinstance(operand, Const):
        return [operand.value]
    if isinstance(operand, Ref):
        return [resolver.value(operand.innie_id)]
    return resolver.values(operand.innie_ids)


def comparand_value(comparand: Comparand, resolver: Resolver) -> int:
    if isinstance(comparand, Const):
        return comparand.value
    return resolver.value(comparand.innie_id)


def evaluate(condition: Condition, resolver: Resolver) -> bool:
    """S7 + S8. Short-circuit lives in the resolver, not here."""
    left = comparand_value(condition.lhs, resolver)

    if condition.quantifier is Quantifier.NONE:
        right = comparand_value(condition.rhs, resolver)
        return condition.op.apply(left, right)

    def pred(right: int) -> bool:
        return condition.op.apply(left, right)

    ids = condition.rhs.innie_ids
    if condition.quantifier is Quantifier.ANY:
        return resolver.any_of(ids, pred)      # ANY OF [] -> False
    return resolver.all_of(ids, pred)          # ALL OF [] -> True


# ----------------------------------------------------------------- handlers

@handles(Load)
def _load(instr: Load, state: State, resolver: Resolver) -> None:
    values = operand_values(instr.operand, resolver)
    state.acc = values[0] if len(values) == 1 else sum(values)


@handles(Add)
def _add(instr: Add, state: State, resolver: Resolver) -> None:
    state.acc += sum(operand_values(instr.operand, resolver))


@handles(Multiply)
def _multiply(instr: Multiply, state: State, resolver: Resolver) -> None:
    state.acc *= math.prod(operand_values(instr.operand, resolver))


@handles(Modulo)
def _modulo(instr: Modulo, state: State, resolver: Resolver) -> None:
    for divisor in operand_values(instr.operand, resolver):
        if divisor == 0:
            raise ArithmeticFault(f"line {instr.line}: MODULO by zero")
        state.acc %= divisor                   # S4: Python semantics


@handles(Waffle)
def _waffle(instr: Waffle, state: State, resolver: Resolver) -> None:
    state.staged = state.acc                   # S1: stage, do not publish


@handles(WellnessCheck)
def _wellness_check(
    instr: WellnessCheck, state: State, resolver: Resolver
) -> None:
    if instr.condition is None or evaluate(instr.condition, resolver):
        state.acc = 0                          # S5


@handles(ConditionalAdd)
def _conditional_add(
    instr: ConditionalAdd, state: State, resolver: Resolver
) -> None:
    # S6: evaluate the condition FIRST; if false, never touch the list.
    if evaluate(instr.condition, resolver):
        state.acc += sum(resolver.values(instr.targets.innie_ids))


@handles(Shift)
def _shift(instr: Shift, state: State, resolver: Resolver) -> None:
    from lumon.interp.engine import run_block   # local import breaks a cycle
    for _ in range(instr.times):                # S12; times >= 0 by model
        run_block(instr.body, state, resolver)
```

- [ ] **Step 5: Implement `lumon/interp/engine.py`**

```python
"""Executes one Innie's workday. Knows nothing about threads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from lumon.interp.handlers import HANDLERS
from lumon.interp.state import State
from lumon.isa import Instruction, Program
from lumon.resolvers.base import Resolver


class Outcome(BaseModel):
    """What an Innie publishes. `staged is None` means VOID (S2)."""

    model_config = ConfigDict(frozen=True)

    staged: int | None = None


def step(instr: Instruction, state: State, resolver: Resolver) -> None:
    try:
        handler = HANDLERS[type(instr)]
    except KeyError:
        raise AssertionError(
            f"no handler registered for {type(instr).__name__}"
        ) from None
    handler(instr, state, resolver)


def run_block(program: Program, state: State, resolver: Resolver) -> None:
    for instr in program:
        step(instr, state, resolver)


def execute(program: Program, resolver: Resolver) -> Outcome:
    state = State()
    run_block(program, state, resolver)
    return Outcome(staged=state.staged)
```

```python
# lumon/interp/__init__.py
from lumon.interp.engine import Outcome, execute, run_block, step
from lumon.interp.handlers import HANDLERS, evaluate

__all__ = ["Outcome", "execute", "run_block", "step", "HANDLERS", "evaluate"]
```

`HANDLERS` keys on `type(instr)` exactly, not `isinstance`. That is
deliberate: a new instruction type without a handler fails loudly on first
execution instead of silently matching a parent class.

- [ ] **Step 6: Run tests and confirm they pass**

Run: `python -m pytest tests/test_interp.py -v`
Expected: 12 passed

- [ ] **Step 7: Commit**

```bash
git add lumon/resolvers/ lumon/interp/ tests/test_interp.py
git commit -m "feat: Resolver seam and thread-free interpreter"
```

---

# Task 8: Interpreter — references and conditions, driven by a fake resolver

**Files:**
- Modify: `tests/test_interp.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: `DictResolver` test fixture — a `Resolver` backed by a plain dict, with left-to-right short-circuit and a call log. Reused in Tasks 13 and 16.

No production code changes. This task proves the interpreter handles every reference-bearing instruction *before* threads enter the picture.

- [ ] **Step 1: Write `tests/conftest.py`**

```python
import pytest


class DictResolver:
    """A Resolver over a fixed dict. Left-to-right short-circuit, and it
    records which Innies were actually consulted so tests can assert on
    S6 (a false CONDITIONAL_ADD must not touch its list)."""

    def __init__(self, values: dict[str, int]):
        self._values = values
        self.touched: list[str] = []

    def value(self, innie_id):
        self.touched.append(innie_id)
        if innie_id not in self._values:
            raise KeyError(f"no value for {innie_id}")
        return self._values[innie_id]

    def values(self, innie_ids):
        return [self.value(i) for i in innie_ids]

    def any_of(self, innie_ids, pred):
        for i in innie_ids:
            if pred(self.value(i)):
                return True          # true is absorbing
        return False                 # ANY OF [] -> False

    def all_of(self, innie_ids, pred):
        for i in innie_ids:
            if not pred(self.value(i)):
                return False         # false is absorbing
        return True                  # ALL OF [] -> True


@pytest.fixture
def dict_resolver():
    return DictResolver
```

- [ ] **Step 2: Write the failing tests**

```python
# append to tests/test_interp.py
from lumon.interp import evaluate
from tests.conftest import DictResolver


def run_with(text, values):
    r = DictResolver(values)
    return execute(parse(text), r), r


def test_add_bare_ref():
    outcome, _ = run_with("LOAD 5\nADD HELLY\nWAFFLE", {"HELLY": 10})
    assert outcome == Outcome(15)


def test_add_ref_list_sums_all():
    outcome, _ = run_with("LOAD 0\nADD [HELLY, MARK]\nWAFFLE",
                          {"HELLY": 20, "MARK": 5})
    assert outcome == Outcome(25)          # 1.json IRVING


def test_multiply_ref_list_takes_the_product():
    outcome, _ = run_with("LOAD 2\nMULTIPLY [A, B]\nWAFFLE", {"A": 3, "B": 4})
    assert outcome == Outcome(24)


def test_repeated_reads_of_one_innie_agree():
    # S1: published values are immutable, so both reads see 8.
    outcome, _ = run_with("LOAD 0\nSHIFT 2 TIMES\nADD HELLY\nEND_SHIFT\nWAFFLE",
                          {"HELLY": 8})
    assert outcome == Outcome(16)          # 3.json MARK


def test_wellness_check_all_of_true_resets():
    # 1.json BURT: 25 > 20 and 25 > 5
    outcome, _ = run_with(
        "LOAD 100\nWELLNESS_CHECK IRVING > ALL OF [HELLY, MARK]\nWAFFLE",
        {"IRVING": 25, "HELLY": 20, "MARK": 5},
    )
    assert outcome == Outcome(0)


def test_wellness_check_any_of_false_does_not_reset():
    # 3.json IRVING first instruction: 8 > 16 is false
    outcome, _ = run_with(
        "LOAD 100\nWELLNESS_CHECK HELLY > ANY OF [MARK]\nWAFFLE",
        {"HELLY": 8, "MARK": 16},
    )
    assert outcome == Outcome(100)


def test_conditional_add_when_true():
    outcome, _ = run_with(
        "LOAD 100\nCONDITIONAL_ADD [HELLY, MARK] IF MARK > 10\nWAFFLE",
        {"HELLY": 8, "MARK": 16},
    )
    assert outcome == Outcome(124)         # 3.json IRVING


def test_conditional_add_when_false_never_touches_the_list():
    # S6 — the load-bearing assertion for dynamic dependencies.
    outcome, resolver = run_with(
        "LOAD 100\nCONDITIONAL_ADD [HELLY, GHOSTLY] IF MARK > 1000\nWAFFLE",
        {"HELLY": 8, "MARK": 16},
    )
    assert outcome == Outcome(100)
    assert "GHOSTLY" not in resolver.touched
    assert "HELLY" not in resolver.touched


def test_any_of_short_circuits_and_never_reads_later_entries():
    r = DictResolver({"X": 100, "A": 1})
    cond = parse("WELLNESS_CHECK X > ANY OF [A, UNREADABLE]")[0].condition
    assert evaluate(cond, r) is True
    assert "UNREADABLE" not in r.touched


def test_all_of_short_circuits_on_first_false():
    r = DictResolver({"X": 1, "A": 100})
    cond = parse("WELLNESS_CHECK X > ALL OF [A, UNREADABLE]")[0].condition
    assert evaluate(cond, r) is False
    assert "UNREADABLE" not in r.touched


def test_vacuous_quantifiers():
    # The parser rejects an empty [list], so build these directly.
    from lumon.isa import Condition, CmpOp, Quantifier, Const, RefList
    r = DictResolver({})
    empty_any = Condition(lhs=Const(value=1), op=CmpOp.GT,
                          quantifier=Quantifier.ANY, rhs=RefList(innie_ids=()))
    empty_all = Condition(lhs=Const(value=1), op=CmpOp.GT,
                          quantifier=Quantifier.ALL, rhs=RefList(innie_ids=()))
    assert evaluate(empty_any, r) is False   # S7
    assert evaluate(empty_all, r) is True    # S7


def test_full_3json_irving_by_hand():
    outcome, _ = run_with(
        "LOAD 100\n"
        "WELLNESS_CHECK HELLY > ANY OF [MARK]\n"
        "CONDITIONAL_ADD [HELLY, MARK] IF MARK > 10\n"
        "WAFFLE",
        {"HELLY": 8, "MARK": 16},
    )
    assert outcome == Outcome(124)
```

- [ ] **Step 3: Run tests and confirm they pass**

These should pass against the Task 7 implementation. If any fail, the interpreter is wrong — fix `interp.py`, not the tests.

Run: `python -m pytest tests/test_interp.py -v`
Expected: 24 passed

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/test_interp.py
git commit -m "test: interpreter handles all reference and condition forms"
```

---

# Task 9: Cell and Registry — the synchronization primitive, tested without the interpreter

**Files:**
- Create: `lumon/cell.py`
- Test: `tests/test_cell.py`

**Interfaces:**
- Produces: `Result` (frozen `BaseModel`), `Cell` (`commit`, `commit_void`, `fault`, `is_settled`, `get`, `peek`), `Registry` (`cell`, `ids`, `snapshot`), `unwrap`, `DEADLOCK_VALUE`.

`Registry` here owns Cells and nothing else. The wait-for graph is a separate
type (`waitgraph.py`, Task 12) and the detection algorithm is a third
(`deadlock/`, Tasks 12-13). That split is what lets every cycle case be
unit-tested with zero threads.

> **Refinement to the supplied Phase 6 design.** The supplied sketch used a separate `_detect_lock` alongside per-Cell locks, and released the lock before blocking. This plan instead gives the **Registry a single `threading.Condition` that every Cell shares**. Two reasons: (1) the required property — "registering the wait edges and running detection happen inside a single lock acquisition" — becomes structural rather than a discipline you must remember; and (2) `Condition.wait()` releases the lock while blocked, so registration, detection, and blocking happen in one uninterrupted critical section with no release/reacquire gap for another thread to slip through. There is no lock-ordering question left to get wrong. At Lumon scale (tens of Innies) the contention cost is nil.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cell.py
import threading
import pytest
from lumon.cell import Cell, Registry, Result, VOID
from lumon.errors import DoubleSettle, NoWorkProduct, DependencyFaulted, LumonError


def make_registry(*ids):
    return Registry(list(ids))


def test_commit_then_get_returns_the_value():
    reg = make_registry("A")
    reg.cell("A").commit(42)
    assert reg.cell("A").get() == 42


def test_get_on_a_pending_cell_blocks_until_commit():
    reg = make_registry("A")
    started = threading.Barrier(2)
    seen = []

    def reader():
        started.wait()
        seen.append(reg.cell("A").get())

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    started.wait()
    assert not seen                    # still blocked
    reg.cell("A").commit(7)
    t.join(timeout=2)
    assert seen == [7]


def test_ten_blocked_threads_all_wake_with_the_same_value():
    reg = make_registry("A")
    ready = threading.Barrier(11)
    seen = []
    lock = threading.Lock()

    def reader():
        ready.wait()
        v = reg.cell("A").get()
        with lock:
            seen.append(v)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(10)]
    for t in threads:
        t.start()
    ready.wait()
    reg.cell("A").commit(99)
    for t in threads:
        t.join(timeout=2)
    assert seen == [99] * 10


def test_double_settle_raises():
    reg = make_registry("A")
    reg.cell("A").commit(1)
    with pytest.raises(DoubleSettle):
        reg.cell("A").commit(2)
    with pytest.raises(DoubleSettle):
        reg.cell("A").commit_void()


def test_fault_then_get_raises_chained():
    reg = make_registry("A")
    cause = ValueError("boom")
    reg.cell("A").fault(cause)
    with pytest.raises(DependencyFaulted) as exc:
        reg.cell("A").get()
    assert exc.value.__cause__ is cause


def test_thread_blocked_before_a_fault_also_raises():
    reg = make_registry("A")
    ready = threading.Barrier(2)
    caught = []

    def reader():
        ready.wait()
        try:
            reg.cell("A").get()
        except DependencyFaulted as e:
            caught.append(e)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    ready.wait()
    reg.cell("A").fault(ValueError("late boom"))
    t.join(timeout=2)
    assert len(caught) == 1


def test_void_cell_raises_no_work_product():
    reg = make_registry("A")
    reg.cell("A").commit_void()
    with pytest.raises(NoWorkProduct):
        reg.cell("A").get()


def test_registry_is_prepopulated_so_unknown_ids_fail_fast():
    reg = make_registry("A")
    with pytest.raises(KeyError):
        reg.cell("GHOST")


def test_snapshot_preserves_construction_order():
    reg = make_registry("B", "A", "C")
    reg.cell("A").commit(1)
    reg.cell("B").commit(2)
    reg.cell("C").commit_void()
    assert [r.innie_id for r in reg.snapshot()] == ["B", "A", "C"]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_cell.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.cell'`

- [ ] **Step 3: Implement `lumon/cell.py`** (deadlock machinery arrives in Task 12)

```python
"""Cells: write-once publish/subscribe slots, one per Innie.

All Cells in a Registry share ONE threading.Condition. See the note in
Task 9 of the plan for why.

Cell and Registry are plain classes, not models: they are mutable, they
hold lock state, and they are only ever constructed internally. Result IS
a model — it is an immutable value that gets compared, snapshotted, and
rendered by the CLI.
"""

from __future__ import annotations

import threading
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from lumon.errors import DependencyFaulted, DoubleSettle, NoWorkProduct

DEADLOCK_VALUE = -1


class Result(BaseModel):
    """value is None and error is None  -> VOID (S2)
       error is not None                -> fault (S10)"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    innie_id: str
    value: int | None = None
    error: BaseException | None = None

    @property
    def is_void(self) -> bool:
        return self.value is None and self.error is None

    @property
    def is_fault(self) -> bool:
        return self.error is not None


class Cell:
    """One Innie's published work product. Written once, read many times."""

    def __init__(self, innie_id: str, cond: threading.Condition):
        self.id = innie_id
        self._cond = cond
        self._result: Result | None = None

    # -- writes -----------------------------------------------------------

    def commit(self, value: int) -> None:
        self._settle(Result(innie_id=self.id, value=value))

    def commit_void(self) -> None:
        self._settle(Result(innie_id=self.id))

    def fault(self, error: BaseException) -> None:
        self._settle(Result(innie_id=self.id, error=error))

    def _settle(self, result: Result) -> None:
        with self._cond:
            self._settle_locked(result)

    def _settle_locked(self, result: Result) -> None:
        """Caller holds the shared condition. Used by deadlock resolution
        (Task 12), which is already inside the critical section."""
        if self._result is not None:
            raise DoubleSettle(f"{self.id} already settled as {self._result!r}")
        self._result = result
        self._cond.notify_all()

    # -- reads ------------------------------------------------------------

    def is_settled(self) -> bool:
        with self._cond:
            return self._result is not None

    def is_settled_locked(self) -> bool:
        return self._result is not None

    def peek(self) -> Result | None:
        with self._cond:
            return self._result

    def get(self) -> int:
        with self._cond:
            while self._result is None:
                self._cond.wait()
            result = self._result
        return unwrap(result)


def unwrap(result: Result) -> int:
    """Result -> int, or the appropriate exception (S2, S10)."""
    if result.is_fault:
        raise DependencyFaulted(result.innie_id) from result.error
    if result.is_void:
        raise NoWorkProduct(result.innie_id)
    return result.value


class Registry:
    """One Cell per Innie, pre-populated at construction (S11) so an unknown
    reference is a KeyError, never a hang.

    Single responsibility: own the Cells and the lock they share. It does
    not know what a cycle is — that lives in waitgraph.py and deadlock/.
    """

    def __init__(self, innie_ids: Iterable[str]):
        self.cond = threading.Condition()
        self._order: list[str] = list(innie_ids)
        self._cells: dict[str, Cell] = {i: Cell(i, self.cond) for i in self._order}

    def cell(self, innie_id: str) -> Cell:
        try:
            return self._cells[innie_id]
        except KeyError:
            raise KeyError(f"no such Innie: {innie_id!r}") from None

    def ids(self) -> list[str]:
        return list(self._order)

    def all_settled_locked(self, innie_ids: Iterable[str]) -> bool:
        """Caller holds self.cond."""
        return all(self._cells[i].is_settled_locked() for i in innie_ids)

    def pending_locked(self, innie_ids: Iterable[str]) -> set[str]:
        """Caller holds self.cond."""
        return {i for i in innie_ids if not self._cells[i].is_settled_locked()}

    def snapshot(self) -> list[Result]:
        """Settled results in construction order. Pending Innies surface as a
        fault carrying `PendingAtSnapshot`; a correct run produces none."""
        with self.cond:
            return [
                self._cells[i]._result
                or Result(innie_id=i, error=PendingAtSnapshot(i))
                for i in self._order
            ]


class PendingAtSnapshot(RuntimeError):
    """A Cell was still unsettled when the registry was snapshotted.
    Always a bug — the watchdog in Task 16 reports it."""

    def __init__(self, innie_id: str):
        super().__init__(f"{innie_id} never settled")
        self.innie_id = innie_id
```

Three notes on the pydantic choices here:

- **`arbitrary_types_allowed=True`** is required because `error: BaseException | None` is not a type pydantic knows how to validate. It stores the exception as-is, which is what we want.
- **`Result` is frozen**, so a settled Cell cannot be mutated through the reference a reader is holding. That is the immutability S1 depends on, now enforced by the type rather than by convention.
- **`PendingAtSnapshot` is a named exception**, not a bare `RuntimeError`. The watchdog in Task 16 filters on it; matching on a bare `RuntimeError` would also catch genuine faults that happen to be `RuntimeError`.

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_cell.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the whole suite 200 times to shake out flakiness**

```bash
python -c "
import subprocess, sys
for i in range(200):
    r = subprocess.run([sys.executable,'-m','pytest','tests/test_cell.py','-q'],
                       capture_output=True)
    if r.returncode: print('FAILED on iteration', i); print(r.stdout.decode()); sys.exit(1)
print('200/200 clean')
"
```
Expected: `200/200 clean`

- [ ] **Step 6: Commit**

```bash
git add lumon/cell.py tests/test_cell.py
git commit -m "feat: Cell and Registry with a shared condition variable"
```

---

# Task 10: Scheduler — thread per Innie, AND-waits only

**Files:**
- Create: `lumon/resolvers/concurrent.py`
- Create: `lumon/runners/__init__.py`, `lumon/runners/base.py`, `lumon/runners/concurrent.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Registry`/`Cell`/`unwrap` (Task 9), `execute` (Task 7), `Innie` (Task 6).
- Produces: `ConcurrentResolver(registry, me)`; `Runner` ABC with `run(innies) -> Registry`; `ConcurrentRunner`; `RUNNERS: dict[str, type[Runner]]`; convenience `run_concurrent(innies)`.

`Runner` is the second of the three abstractions. It exists because there are
genuinely two execution strategies — threads and the serial oracle — and the
CLI must pick between them without knowing either. `RUNNERS` is the OCP
extension point: a third strategy is a dict entry, not an edit to the CLI.

Deadlock detection is deliberately absent — Task 12 adds it. Everything here works for acyclic schedules, which is all three samples.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler.py
import pytest
from lumon.errors import DependencyFaulted, NoWorkProduct
from lumon.loader import load
from lumon.runners.concurrent import run_concurrent


def results(schedule_dict):
    reg = run_concurrent(load(schedule_dict))
    return {r.innie_id: r for r in reg.snapshot()}


def values(schedule_dict):
    return {k: v.value for k, v in results(schedule_dict).items()}


def test_independent_innies_all_complete():
    assert values({"innies": [
        {"id": "A", "schedule": "LOAD 1\nWAFFLE"},
        {"id": "B", "schedule": "LOAD 2\nWAFFLE"},
    ]}) == {"A": 1, "B": 2}


def test_a_dependent_innie_waits_and_reads():
    assert values({"innies": [
        {"id": "A", "schedule": "LOAD 10\nWAFFLE"},
        {"id": "B", "schedule": "LOAD 5\nADD A\nWAFFLE"},
    ]}) == {"A": 10, "B": 15}


def test_multiple_waffles_publish_only_the_last():
    # 2.json — the S1 acceptance test.
    assert values({"innies": [
        {"id": "DYLAN", "schedule": "LOAD 1\nWAFFLE\nLOAD 100\nWAFFLE"},
        {"id": "BURT",
         "schedule": "LOAD 0\nADD [DYLAN]\nWAFFLE\nADD [DYLAN]\nWAFFLE"},
    ]}) == {"DYLAN": 100, "BURT": 200}


def test_deep_chain_of_dependencies():
    n = 40
    innies = [{"id": "N0", "schedule": "LOAD 1\nWAFFLE"}]
    innies += [{"id": f"N{i}", "schedule": f"LOAD 0\nADD N{i-1}\nADD 1\nWAFFLE"}
               for i in range(1, n)]
    out = values({"innies": innies})
    assert out[f"N{n-1}"] == n


def test_an_innie_with_no_waffle_is_void_not_pending():
    res = results({"innies": [{"id": "A", "schedule": "LOAD 5"}]})
    assert res["A"].is_void


def test_reading_a_void_innie_faults_the_reader():
    res = results({"innies": [
        {"id": "A", "schedule": "LOAD 5"},
        {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
    ]})
    assert res["A"].is_void
    assert isinstance(res["B"].error, DependencyFaulted)
    assert isinstance(res["B"].error.__cause__, NoWorkProduct)


def test_faults_propagate_down_a_chain_with_chained_causes():
    res = results({"innies": [
        {"id": "A", "schedule": "LOAD 5\nMODULO 0\nWAFFLE"},
        {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
        {"id": "C", "schedule": "LOAD 0\nADD B\nWAFFLE"},
    ]})
    assert res["A"].is_fault and res["B"].is_fault and res["C"].is_fault
    assert isinstance(res["C"].error.__cause__, DependencyFaulted)


def test_repeated_runs_are_identical():
    schedule = {"innies": [
        {"id": "A", "schedule": "LOAD 10\nWAFFLE"},
        {"id": "B", "schedule": "LOAD 2\nWAFFLE"},
        {"id": "C", "schedule": "LOAD 0\nADD [A, B]\nWAFFLE"},
        {"id": "D", "schedule": "LOAD 100\nWELLNESS_CHECK C > ALL OF [A, B]\nWAFFLE"},
    ]}
    first = values(schedule)
    for _ in range(500):
        assert values(schedule) == first
    assert first == {"A": 10, "B": 2, "C": 12, "D": 0}
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.runners'`

- [ ] **Step 3: Implement the resolver, the Runner base, and the concurrent runner**

```python
"""Resolver backed by a live Registry. Blocks on the shared condition."""

from __future__ import annotations

from typing import Callable, Sequence

from lumon.cell import Registry, unwrap
from lumon.errors import Cancelled
from lumon.resolvers.base import Resolver


class ConcurrentResolver(Resolver):
    def __init__(self, registry: Registry, me: str):
        self.registry = registry
        self.me = me

    # -- AND-wait ---------------------------------------------------------

    def value(self, innie_id: str) -> int:
        return self.values([innie_id])[0]

    def values(self, innie_ids: Sequence[str]) -> list[int]:
        cells = [self.registry.cell(i) for i in innie_ids]   # KeyError if unknown
        with self.registry.cond:
            while not self.registry.all_settled_locked(innie_ids):
                self.registry.cond.wait()
            results = [self.registry.result_locked(i) for i in innie_ids]
        return [unwrap(r) for r in results]

    # -- OR-wait ----------------------------------------------------------
    # Task 13 replaces these with genuinely concurrent short-circuit waits
    # plus OR-group registration. Sequential for now: correct, just less
    # eager than the spec's Requirement 4.

    def any_of(self, innie_ids, pred: Callable[[int], bool]) -> bool:
        for innie_id in innie_ids:
            if pred(self.value(innie_id)):
                return True
        return False

    def all_of(self, innie_ids, pred: Callable[[int], bool]) -> bool:
        for innie_id in innie_ids:
            if not pred(self.value(innie_id)):
                return False
        return True
```

```python
# lumon/runners/base.py
"""A Runner turns a list of Innies into a settled Registry.

Two strategies exist: threads (concurrent.py) and the single-threaded
reference oracle (serial.py). The CLI depends on this abstraction, never on
either concrete runner (DIP).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lumon.cell import Registry
from lumon.loader import Innie


class Runner(ABC):
    name: str

    @abstractmethod
    def run(self, innies: list[Innie]) -> Registry:
        """Execute every Innie and return the settled Registry.

        Contract shared by all implementations: every Cell is settled on
        return, and the result depends only on `innies` (S8, S9).
        """


RUNNERS: dict[str, type[Runner]] = {}


def register(runner: type[Runner]) -> type[Runner]:
    RUNNERS[runner.name] = runner
    return runner
```

```python
# lumon/runners/concurrent.py
"""One Outie (thread) per Innie."""

from __future__ import annotations

import threading

from lumon.cell import Registry
from lumon.errors import Cancelled
from lumon.interp import execute
from lumon.loader import Innie
from lumon.resolvers.concurrent import ConcurrentResolver
from lumon.runners.base import Runner, register


def run_innie(innie: Innie, registry: Registry) -> None:
    """The body of one Outie thread.

    The try/except covering everything is not optional: any escape path that
    does not settle the Cell hangs every dependent.
    """
    cell = registry.cell(innie.id)
    resolver = ConcurrentResolver(registry, innie.id)
    try:
        outcome = execute(innie.program, resolver)
    except Cancelled:
        return                      # Task 12: cell already holds -1
    except BaseException as error:  # noqa: BLE001 - deliberate catch-all
        cell.fault(error)
        return
    if outcome.staged is None:
        cell.commit_void()          # S2
    else:
        cell.commit(outcome.staged)


@register
class ConcurrentRunner(Runner):
    name = "concurrent"

    def run(self, innies: list[Innie]) -> Registry:
        registry = Registry(i.id for i in innies)
        threads = [
            threading.Thread(
                target=run_innie, args=(innie, registry),
                name=f"outie-{innie.id}", daemon=True,
            )
            for innie in innies
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return registry


def run_concurrent(innies: list[Innie]) -> Registry:
    return ConcurrentRunner().run(innies)
```

```python
# lumon/runners/__init__.py
from lumon.runners.base import RUNNERS, Runner
from lumon.runners.concurrent import ConcurrentRunner, run_concurrent
from lumon.runners.serial import SerialRunner, run_serial   # added in Task 14

__all__ = [
    "RUNNERS", "Runner",
    "ConcurrentRunner", "run_concurrent",
    "SerialRunner", "run_serial",
]
```

Until Task 14 lands, drop the `serial` line from `__init__.py`; the tests in
this task import `run_concurrent` directly from `lumon.runners.concurrent`.

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: 8 passed (the last one takes a few seconds — 500 iterations)

- [ ] **Step 5: Commit**

```bash
git add lumon/resolvers/concurrent.py lumon/runners/ tests/test_scheduler.py
git commit -m "feat: thread-per-Innie runner with concurrent resolver"
```

---

# Task 11: Golden tests for all three sample schedules

**Files:**
- Create: `tests/test_golden.py`

**Interfaces:**
- Consumes: `load_path` (Task 6), `run_concurrent` (Task 10).

These are the acceptance tests. They must pass before deadlock work begins — none of the samples contains a cycle, so nothing in Task 12 should change these numbers.

- [ ] **Step 1: Write the test**

```python
# tests/test_golden.py
import pathlib
import pytest
from lumon.loader import load_path
from lumon.runners.concurrent import run_concurrent

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"

EXPECTED = {
    "1.json": {"HELLY": 20, "MARK": 5, "IRVING": 25, "BURT": 0},
    "2.json": {"DYLAN": 100, "BURT": 200},
    "3.json": {"HELLY": 8, "MARK": 16, "IRVING": 124, "BURT": 497, "DYLAN": 0},
}


def values(name):
    registry = run_concurrent(load_path(SAMPLES / name))
    return {r.innie_id: r.value for r in registry.snapshot()}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_matches_hand_computed_values(name):
    assert values(name) == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_is_stable_across_200_runs(name):
    expected = EXPECTED[name]
    for _ in range(200):
        assert values(name) == expected
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_golden.py -v`
Expected: 6 passed

If a value disagrees with `EXPECTED`, re-derive it by hand from the Golden Values section above before touching either the test or the code. The hand computation is the authority.

- [ ] **Step 3: Commit**

```bash
git add tests/test_golden.py
git commit -m "test: golden values for 1.json, 2.json, 3.json"
```

---

# Task 12: Deadlock detection via the wait-for graph

**Files:**
- Create: `lumon/waitgraph.py`, `lumon/deadlock/__init__.py`, `lumon/deadlock/base.py`, `lumon/deadlock/scc.py`
- Modify: `lumon/cell.py` (Registry gains the blocking protocol)
- Modify: `lumon/resolvers/concurrent.py` (route waits through `await_innies`)
- Test: `tests/test_waitgraph.py` (no threads), `tests/test_deadlock.py` (through the scheduler)

**Interfaces:**
- Produces: `WaitGraph` (`wait_on`, `clear`, `blocked`, `and_edges`), `DeadlockDetector` ABC with `find_cycle(graph) -> list[str] | None`, `SccDetector`, and on `Registry`: `await_innies(me, targets) -> None`, `is_cancelled(id) -> bool`.

**The SRP split.** `WaitGraph` is plain data — no lock, no threads, no
algorithm. `DeadlockDetector` is a pure function from a graph to the members
of a cycle. `Registry` holds the lock and calls them. The payoff is that every
cycle case is unit-tested against a hand-built graph in `test_waitgraph.py`
with zero concurrency, and `test_deadlock.py` only has to prove the edges get
registered — not that the algorithm is right.

**Goal:** when Innies form a circular dependency, every Innie in that cycle commits `-1` instead of hanging. The set receiving `-1` must be identical on every run of the same input.

### Why not static analysis

Do **not** build a dependency graph by scanning source and cycle-checking before execution. `CONDITIONAL_ADD [X] IF <cond>` creates an edge only when the condition evaluates true at runtime (S6), so a static graph over-approximates: it reports cycles that never form and assigns `-1` to Innies that would otherwise complete normally. Detection must run against edges that have actually materialized.

### Why the SCC, not the path

The cycle members are the **strongly-connected component**. Computing the SCC — rather than the particular path the detecting thread happened to walk — is what makes the result deterministic: SCC membership is a property of the graph, independent of which thread ran the search or in what order it visited nodes. This is stated as a contract on `DeadlockDetector.find_cycle` and checked by `test_result_is_independent_of_insertion_order`.

### The critical property

Registering the wait edges and running detection happen inside a **single lock acquisition**. If they were separate, two threads could each register, each see a graph missing the other's edge, and both conclude there is no cycle. Because Task 9 gave the Registry one shared `Condition`, and `Condition.wait()` releases it atomically, registration → detection → blocking is one uninterrupted critical section.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deadlock.py
import pytest
from lumon.loader import load
from lumon.runners.concurrent import run_concurrent


def values(schedule_dict):
    reg = run_concurrent(load(schedule_dict))
    return {r.innie_id: r.value for r in reg.snapshot()}


def sched(*pairs):
    return {"innies": [{"id": i, "schedule": s} for i, s in pairs]}


def test_two_node_cycle_both_get_minus_one():
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
    )) == {"A": -1, "B": -1}


def test_three_node_cycle_all_get_minus_one():
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD C\nWAFFLE"),
        ("C", "LOAD 0\nADD A\nWAFFLE"),
    )) == {"A": -1, "B": -1, "C": -1}


def test_self_reference_is_a_one_node_cycle():
    # S9 — legal input with a defined answer, NOT a parse error.
    assert values(sched(("A", "LOAD 5\nADD A\nWAFFLE"))) == {"A": -1}


def test_a_dependent_of_a_cycle_receives_minus_one_and_computes_with_it():
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
        ("C", "LOAD 100\nADD A\nWAFFLE"),     # 100 + (-1)
    )) == {"A": -1, "B": -1, "C": 99}


def test_two_disjoint_cycles_resolve_independently():
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
        ("C", "LOAD 0\nADD D\nWAFFLE"),
        ("D", "LOAD 0\nADD C\nWAFFLE"),
    )) == {"A": -1, "B": -1, "C": -1, "D": -1}


def test_overlapping_cycles_form_one_scc():
    # A<->B and B<->C share B; all three are one SCC.
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD [A, C]\nWAFFLE"),
        ("C", "LOAD 0\nADD B\nWAFFLE"),
    )) == {"A": -1, "B": -1, "C": -1}


def test_partial_dependency_set_only_the_cycle_member_is_cancelled():
    # X waits on {A, HEALTHY}. Only A is in a cycle. X is not a member:
    # it waits for both, gets A = -1 and HEALTHY's real value.
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
        ("HEALTHY", "LOAD 7\nWAFFLE"),
        ("X", "LOAD 0\nADD [A, HEALTHY]\nWAFFLE"),
    )) == {"A": -1, "B": -1, "HEALTHY": 7, "X": 6}


def test_conditional_edge_that_never_fires_forms_no_cycle():
    # THE test that proves detection runs on the runtime graph, not the
    # static one. A statically references B and B references A, but A's
    # edge is guarded by a condition that is false, so no cycle forms and
    # both Innies complete normally.
    assert values(sched(
        ("GATE", "LOAD 0\nWAFFLE"),
        ("A", "LOAD 10\nCONDITIONAL_ADD [B] IF GATE > 100\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
    )) == {"GATE": 0, "A": 10, "B": 10}


def test_conditional_edge_that_does_fire_forms_a_cycle():
    assert values(sched(
        ("GATE", "LOAD 1000\nWAFFLE"),
        ("A", "LOAD 10\nCONDITIONAL_ADD [B] IF GATE > 100\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
    )) == {"GATE": 1000, "A": -1, "B": -1}


def test_cycle_plus_healthy_subgraph_leaves_the_healthy_part_alone():
    assert values(sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD A\nWAFFLE"),
        ("P", "LOAD 3\nWAFFLE"),
        ("Q", "LOAD 0\nADD P\nMULTIPLY 2\nWAFFLE"),
    )) == {"A": -1, "B": -1, "P": 3, "Q": 6}


def test_deadlocked_schedule_is_byte_identical_across_500_runs():
    schedule = sched(
        ("A", "LOAD 0\nADD B\nWAFFLE"),
        ("B", "LOAD 0\nADD [A, C]\nWAFFLE"),
        ("C", "LOAD 0\nADD B\nWAFFLE"),
        ("D", "LOAD 50\nADD A\nWAFFLE"),
        ("E", "LOAD 1\nWAFFLE"),
    )
    first = values(schedule)
    for _ in range(500):
        assert values(schedule) == first
    assert first == {"A": -1, "B": -1, "C": -1, "D": 49, "E": 1}
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_deadlock.py -v -x --timeout=20`
Expected: HANG or timeout on the first test — there is no detection yet. If `pytest-timeout` is unavailable, run the single test with `timeout 20 python -m pytest ...` and expect it to be killed. This is the correct starting state.

- [ ] **Step 3: Write `lumon/waitgraph.py` — plain data, no lock, no threads**

```python
"""The wait-for graph: who is blocked on whom, right now.

Distinct from the static reference graph and much smaller — an entry
exists only while an Innie is actually blocked. Deliberately a plain
mutable class with no lock of its own: the Registry owns the lock and
every method here is called with it held. Keeping the data separate from
both the lock and the algorithm is what makes the detectors testable
without threads.
"""

from __future__ import annotations

from typing import Iterable


class WaitGraph:
    def __init__(self) -> None:
        # AND-waits: `_and[A] == {B, C}` means A needs BOTH B and C.
        self._and: dict[str, set[str]] = {}
        # OR-waits (Task 13): `_or[A] == [{B, C}]` means A needs ANY of B, C.
        self._or: dict[str, list[set[str]]] = {}

    # -- AND-waits --------------------------------------------------------

    def wait_on(self, waiter: str, targets: Iterable[str]) -> None:
        self._and[waiter] = set(targets)

    def and_edges(self, node: str) -> set[str]:
        return self._and.get(node, set())

    # -- OR-waits (populated in Task 13) ----------------------------------

    def push_or_group(self, waiter: str, targets: Iterable[str]) -> None:
        self._or.setdefault(waiter, []).append(set(targets))

    def pop_or_group(self, waiter: str) -> None:
        groups = self._or.get(waiter)
        if groups:
            groups.pop()
            if not groups:
                self._or.pop(waiter, None)

    def or_groups(self, node: str) -> list[set[str]]:
        return self._or.get(node, [])

    # -- shared -----------------------------------------------------------

    def clear(self, waiter: str) -> None:
        self._and.pop(waiter, None)
        self._or.pop(waiter, None)

    def blocked(self) -> set[str]:
        return set(self._and) | set(self._or)
```

- [ ] **Step 4: Write `lumon/deadlock/base.py` and `lumon/deadlock/scc.py`**

```python
# lumon/deadlock/base.py
"""Deadlock detection as a pure function of the wait-for graph.

Two implementations: SccDetector (AND-waits, Task 12) and AndOrDetector
(adds OR-waits, Task 13). Both are pure — no locks, no threads, no Cells —
so they are unit-tested against hand-built graphs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lumon.waitgraph import WaitGraph


class DeadlockDetector(ABC):
    @abstractmethod
    def find_cycle(self, graph: WaitGraph) -> list[str] | None:
        """Return the Innies that must be resolved to -1, sorted, or None.

        Contract: the result depends only on `graph`, never on which thread
        called this or in what order nodes were visited. That is what makes
        deadlock resolution deterministic (S9).
        """
```

```python
# lumon/deadlock/scc.py
"""AND-wait detection: the strongly-connected component containing a cycle."""

from __future__ import annotations

from lumon.deadlock.base import DeadlockDetector
from lumon.waitgraph import WaitGraph


class SccDetector(DeadlockDetector):
    """Finds cycle members as an SCC, not as the path a search happened to
    walk. SCC membership is a property of the graph — independent of which
    thread ran the search and in what order it visited nodes — and that is
    exactly what makes the answer deterministic.

    The graph has at most one node per blocked Innie, so the naive O(V*E)
    form is fine at this scale; Tarjan would be a correct substitute.
    """

    def find_cycle(self, graph: WaitGraph) -> list[str] | None:
        for node in sorted(graph.blocked()):
            forward = self._reachable_from(graph, node)
            if node not in forward:
                continue                       # cannot reach itself: no cycle
            scc = {
                n for n in forward
                if node in self._reachable_from(graph, n)
            }
            return sorted(scc | {node})        # sorted for stable ordering
        return None

    @staticmethod
    def _reachable_from(graph: WaitGraph, start: str) -> set[str]:
        seen: set[str] = set()
        frontier = list(graph.and_edges(start))
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(graph.and_edges(node))
        return seen
```

```python
# lumon/deadlock/__init__.py
from lumon.deadlock.base import DeadlockDetector
from lumon.deadlock.scc import SccDetector

__all__ = ["DeadlockDetector", "SccDetector"]
```

- [ ] **Step 5: Unit-test the detector with no threads at all**

```python
# tests/test_waitgraph.py
from lumon.deadlock import SccDetector
from lumon.waitgraph import WaitGraph


def graph(**edges):
    g = WaitGraph()
    for waiter, targets in edges.items():
        g.wait_on(waiter, targets)
    return g


def test_no_edges_means_no_cycle():
    assert SccDetector().find_cycle(WaitGraph()) is None


def test_a_chain_is_not_a_cycle():
    assert SccDetector().find_cycle(graph(A="B", B="C")) is None


def test_two_node_cycle():
    assert SccDetector().find_cycle(graph(A="B", B="A")) == ["A", "B"]


def test_three_node_cycle():
    assert SccDetector().find_cycle(graph(A="B", B="C", C="A")) == ["A", "B", "C"]


def test_self_reference_is_a_one_node_cycle():
    assert SccDetector().find_cycle(graph(A="A")) == ["A"]


def test_overlapping_cycles_form_one_scc():
    # A<->B and B<->C share B.
    g = graph(A="B", C="B")
    g.wait_on("B", {"A", "C"})
    assert SccDetector().find_cycle(g) == ["A", "B", "C"]


def test_a_node_waiting_on_a_cycle_is_not_a_member():
    g = graph(A="B", B="A", X="A")
    assert SccDetector().find_cycle(g) == ["A", "B"]


def test_result_is_independent_of_insertion_order():
    forward = SccDetector().find_cycle(graph(A="B", B="C", C="A"))
    g = WaitGraph()
    for waiter, target in (("C", "A"), ("B", "C"), ("A", "B")):
        g.wait_on(waiter, {target})
    assert SccDetector().find_cycle(g) == forward


def test_clear_removes_a_waiter():
    g = graph(A="B", B="A")
    g.clear("B")
    assert SccDetector().find_cycle(g) is None
```

Run: `python -m pytest tests/test_waitgraph.py -v`
Expected: 9 passed — and note not one of them imports `threading`.

- [ ] **Step 6: Give `Registry` the blocking protocol**

```python
# add to lumon/cell.py

from lumon.deadlock.base import DeadlockDetector
from lumon.deadlock.scc import SccDetector
from lumon.waitgraph import WaitGraph


class Registry:
    def __init__(
        self,
        innie_ids: Iterable[str],
        detector: DeadlockDetector | None = None,
    ):
        self.cond = threading.Condition()
        self._order = list(innie_ids)
        self._cells = {i: Cell(i, self.cond) for i in self._order}
        self._graph = WaitGraph()
        self._cancelled: set[str] = set()
        # DIP: the Registry depends on the abstraction, so Task 13 swaps the
        # algorithm by passing a different detector — no edit to this file.
        self._detector = detector or SccDetector()

    # -- blocking protocol ------------------------------------------------

    def await_innies(self, me: str, targets: set[str]) -> None:
        """Block until every target has settled, detecting cycles first.

        Every point where an Innie blocks on other Innies goes through here:
        ADD/MULTIPLY/MODULO over a list, a bare Ref, CONDITIONAL_ADD targets,
        and every condition operand.

        The critical property: registering the wait edges and running
        detection happen inside a SINGLE lock acquisition. If they were
        separate, two threads could each register, each see a graph missing
        the other's edge, and both conclude there is no cycle. Because all
        Cells share this Condition and `wait()` releases it atomically,
        registration -> detection -> blocking is one uninterrupted critical
        section.
        """
        with self.cond:
            if not self.pending_locked(targets):
                return

            self._graph.wait_on(me, self.pending_locked(targets))
            try:
                cycle = self._detector.find_cycle(self._graph)
                if cycle:
                    self._resolve_cycle(cycle)

                while not self.all_settled_locked(targets):
                    if me in self._cancelled:
                        return
                    self.cond.wait()
            finally:
                self._graph.clear(me)

    def is_cancelled(self, innie_id: str) -> bool:
        with self.cond:
            return innie_id in self._cancelled

    def result_locked(self, innie_id: str) -> Result:
        """Caller holds self.cond."""
        return self._cells[innie_id]._result

    def _resolve_cycle(self, members: list[str]) -> None:
        """Caller holds self.cond. Commits -1 to every member."""
        for innie_id in members:
            self._cancelled.add(innie_id)
            self._graph.clear(innie_id)
            cell = self._cells[innie_id]
            if not cell.is_settled_locked():
                cell._settle_locked(
                    Result(innie_id=innie_id, value=DEADLOCK_VALUE)
                )
        self.cond.notify_all()
```

Note the guard in `_resolve_cycle`: a member may already have been settled by
a concurrent path, so commit only if still pending.

- [ ] **Step 7: Route all waits through `await_innies` in `lumon/resolvers/concurrent.py`**

```python
    def values(self, innie_ids: Sequence[str]) -> list[int]:
        self.registry.await_innies(self.me, set(innie_ids))
        if self.registry.is_cancelled(self.me):
            raise Cancelled(self.me)
        with self.registry.cond:
            results = [self.registry.result_locked(i) for i in innie_ids]
        return [unwrap(r) for r in results]
```

### Cancellation — the assumption this breaks

Everywhere else in the system, a Cell is written only by the thread running that Innie. Cycle resolution violates this: the detecting thread writes `-1` into Cells belonging to Innies that are currently asleep. Two consequences:

**1. A woken Innie must notice it was cancelled.** Implemented as an exception rather than a polling check, so the interpreter stays free of concurrency awareness: `ConcurrentResolver.values` raises `Cancelled` after any blocking call if `me` was cancelled. `run_innie` catches `Cancelled` and returns without settling. Without this, a cancelled Innie resumes, runs to completion, and attempts a second commit.

**2. Normal completion must tolerate a pre-settled Cell.** `run_innie` already returns early on `Cancelled`, which covers the path where cancellation is observed. Add a belt-and-braces guard for the case where an Innie was cancelled while *not* blocked (it cannot happen today, but the guard costs nothing and documents the invariant):

```python
def run_innie(innie: Innie, registry: Registry) -> None:
    cell = registry.cell(innie.id)
    resolver = ConcurrentResolver(registry, innie.id)
    try:
        outcome = execute(innie.program, resolver)
    except Cancelled:
        return
    except BaseException as error:  # noqa: BLE001
        if not registry.is_cancelled(innie.id):
            cell.fault(error)
        return
    if registry.is_cancelled(innie.id):
        return
    if outcome.staged is None:
        cell.commit_void()
    else:
        cell.commit(outcome.staged)
```

**Keep `Cell._settle` raising `DoubleSettle`** — that is a real bug detector. Guard at the call site instead of weakening the Cell.

**Behaviour for non-members.** Innies that merely depend on a cycle member are not cancelled. They block normally, wake when the member's Cell commits `-1`, receive `-1` as an ordinary integer, and continue computing with it. Only Innies inside the SCC are cancelled.

- [ ] **Step 8: Run the deadlock tests and confirm they pass**

Run: `python -m pytest tests/test_deadlock.py -v`
Expected: 11 passed

- [ ] **Step 9: Confirm nothing regressed**

Run: `python -m pytest -v`
Expected: all previous tests still pass, golden values unchanged.

- [ ] **Step 10: Commit**

```bash
git add lumon/waitgraph.py lumon/deadlock/ lumon/cell.py \
        lumon/resolvers/concurrent.py tests/test_waitgraph.py tests/test_deadlock.py
git commit -m "feat: deadlock detection via the runtime wait-for graph"
```

---

# Task 13: OR-waits — real short-circuit, and the deadlock refinement it forces

**Files:**
- Create: `lumon/deadlock/andor.py`
- Modify: `lumon/resolvers/concurrent.py` (`any_of`/`all_of` become genuinely concurrent)
- Modify: `lumon/cell.py` (`await_any`; default detector becomes `AndOrDetector`)
- Modify: `tests/test_waitgraph.py`, `tests/test_deadlock.py`, `tests/test_scheduler.py`

**Interfaces:**
- Produces: `AndOrDetector` (implements `DeadlockDetector`), `Registry.await_any(me, targets) -> str`.

**Why this is an addition, not a rewrite.** Because Task 12 put detection
behind `DeadlockDetector`, this task adds a file and changes one default. The
`WaitGraph` already carries OR-groups (`push_or_group`/`or_groups`), and
`SccDetector` stays exactly as it is — still used by its own unit tests as the
AND-only reference. That is the SRP split paying for itself.

> **The gap this task closes.** The supplied Phase 6 design models every wait as an AND-wait — "task A is blocked, needing B and C". That is correct for `ADD [B, C]`, and it is what Task 12 implements. But `ANY OF` is an **OR**-wait: the Innie proceeds as soon as *one* branch resolves. Routing an OR-wait through `await_innies` has two consequences, and both are wrong:
>
> 1. **Short-circuit is lost** — the Innie blocks on every branch, violating spec Requirement 4. (Task 12's `ConcurrentResolver.any_of` walks the list sequentially for exactly this reason.)
> 2. **False deadlocks.** Given `HELLY: WELLNESS_CHECK X > ANY OF [MARK, DYLAN]` where MARK cycles back to HELLY but DYLAN is independent: registering AND-edges to both makes HELLY look like an SCC member, so HELLY and MARK both get `-1` — when HELLY could have resolved via DYLAN and completed normally. The answer is deterministic but incorrect.
>
> The fix keeps the SCC machinery and adds one layer in front of it.

### The refinement

Track OR-waits separately: `_or_waiting[A] = [ {M, D}, ... ]`, a list of OR-groups. Then:

1. **Compute the stuck set by greatest fixpoint.** Start by assuming every blocked Innie is stuck, then repeatedly release any Innie that still has a way forward. An Innie can still make progress if *all* its AND-dependencies can make progress *and* every OR-group contains at least one member that can. Iterate to a fixpoint. The fixpoint is unique and independent of iteration order, so it is deterministic.
2. **Within the stuck set, find cycles exactly as before.** Restrict the graph to stuck-to-stuck edges (AND-edges, plus OR-group edges only where the entire group is stuck) and take the SCC. Only those Innies get `-1`; the rest of the stuck set is waiting *on* a cycle and is rescued when the cycle publishes.

With no OR-groups present this reduces exactly to Task 12's behaviour, so all eleven Task 12 tests must continue to pass unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_deadlock.py

def test_or_branch_escapes_a_cycle_no_false_deadlock():
    # HELLY's ANY OF has two branches. MARK cycles back to HELLY; DYLAN
    # does not. HELLY must escape via DYLAN and complete normally.
    # Under an AND-only wait graph this wrongly reports HELLY and MARK
    # as a cycle.
    assert values(sched(
        ("DYLAN", "LOAD 1\nWAFFLE"),
        ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]\nWAFFLE"),
        ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
    )) == {"DYLAN": 1, "HELLY": 0, "MARK": 0}


def test_or_branch_with_no_escape_is_still_a_cycle():
    # MARK is HELLY's only branch, and MARK waits on HELLY. Genuine cycle.
    assert values(sched(
        ("HELLY", "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK]\nWAFFLE"),
        ("MARK", "LOAD 0\nADD HELLY\nWAFFLE"),
    )) == {"HELLY": -1, "MARK": -1}


def test_all_of_short_circuits_on_a_false_branch_and_escapes():
    # 5 > FALSIFIER is false, so ALL OF is false without ever needing
    # LATE — which cycles back. HELLY escapes.
    assert values(sched(
        ("FALSIFIER", "LOAD 999\nWAFFLE"),
        ("HELLY", "LOAD 7\nWELLNESS_CHECK 5 > ALL OF [FALSIFIER, LATE]\nWAFFLE"),
        ("LATE", "LOAD 0\nADD HELLY\nWAFFLE"),
    )) == {"FALSIFIER": 999, "HELLY": 7, "LATE": 7}
```

```python
# append to tests/test_scheduler.py

def test_any_of_does_not_serialize_on_a_slow_branch():
    # SLOW depends on a long chain; FAST is immediate. ANY OF must be
    # satisfiable by FAST without waiting for the whole chain. Asserted
    # by result, not by timing: if `any_of` blocked on SLOW first this
    # would still pass, so pair it with the deadlock test above, which
    # can only pass with a genuine OR-wait.
    chain = [{"id": "C0", "schedule": "LOAD 1\nWAFFLE"}]
    chain += [{"id": f"C{i}", "schedule": f"LOAD 0\nADD C{i-1}\nWAFFLE"}
              for i in range(1, 20)]
    out = values({"innies": chain + [
        {"id": "FAST", "schedule": "LOAD 1\nWAFFLE"},
        {"id": "P", "schedule": "LOAD 9\nWELLNESS_CHECK 5 > ANY OF [FAST, C19]\nWAFFLE"},
    ]})
    assert out["P"] == 0
```

- [ ] **Step 2: Run and confirm the first one fails**

Run: `python -m pytest tests/test_deadlock.py::test_or_branch_escapes_a_cycle_no_false_deadlock -v`
Expected: FAIL — asserts `{"HELLY": -1, "MARK": -1, "DYLAN": 1}` instead of the correct result.

- [ ] **Step 3: Write `lumon/deadlock/andor.py`**

```python
"""AND/OR deadlock detection.

An AND-wait (`ADD [B, C]`) is blocked if ANY dependency is stuck.
An OR-wait (`ANY OF [B, C]`) is blocked only if ALL branches are stuck.
Mixing them needs a greatest fixpoint rather than a plain reachability walk.
"""

from __future__ import annotations

from lumon.deadlock.base import DeadlockDetector
from lumon.waitgraph import WaitGraph


class AndOrDetector(DeadlockDetector):
    """Two phases:

    1. **Stuck set, by greatest fixpoint.** Assume every blocked Innie is
       stuck, then repeatedly release any that still has a way forward. The
       fixpoint is unique and independent of iteration order, so it is
       deterministic.
    2. **Cycles within the stuck set.** Restrict the graph to stuck-to-stuck
       edges and take the SCC. Only those get -1; the rest of the stuck set
       is waiting *on* a cycle and is rescued when the cycle publishes.

    With no OR-groups present this reduces exactly to SccDetector.
    """

    def find_cycle(self, graph: WaitGraph) -> list[str] | None:
        stuck = self._stuck_set(graph)
        if not stuck:
            return None

        for node in sorted(stuck):
            forward = self._reachable(graph, stuck, node)
            if node not in forward:
                continue
            scc = {
                n for n in forward
                if node in self._reachable(graph, stuck, n)
            }
            return sorted(scc | {node})
        return None

    # -- phase 1 ----------------------------------------------------------

    def _stuck_set(self, graph: WaitGraph) -> set[str]:
        stuck = graph.blocked()
        changed = True
        while changed:
            changed = False
            for node in sorted(stuck):
                if self._has_way_forward(graph, node, stuck):
                    stuck.discard(node)
                    changed = True
        return stuck

    @staticmethod
    def _has_way_forward(graph: WaitGraph, node: str, stuck: set[str]) -> bool:
        """Progress is possible if every AND-dependency can progress and
        every OR-group has at least one member that can."""
        for dep in graph.and_edges(node):
            if dep in stuck:
                return False
        for group in graph.or_groups(node):
            if group and all(m in stuck for m in group):
                return False
        return True

    # -- phase 2 ----------------------------------------------------------

    @staticmethod
    def _stuck_edges(graph: WaitGraph, stuck: set[str], node: str) -> set[str]:
        edges = {d for d in graph.and_edges(node) if d in stuck}
        for group in graph.or_groups(node):
            if group and all(m in stuck for m in group):
                edges |= group
        return edges

    def _reachable(
        self, graph: WaitGraph, stuck: set[str], start: str
    ) -> set[str]:
        seen: set[str] = set()
        frontier = list(self._stuck_edges(graph, stuck, start))
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(self._stuck_edges(graph, stuck, node))
        return seen
```

Export it and make it the default:

```python
# lumon/deadlock/__init__.py
from lumon.deadlock.andor import AndOrDetector
from lumon.deadlock.base import DeadlockDetector
from lumon.deadlock.scc import SccDetector

__all__ = ["DeadlockDetector", "SccDetector", "AndOrDetector"]
```

```python
# lumon/cell.py — one line changes in Registry.__init__
        self._detector = detector or AndOrDetector()
```

- [ ] **Step 4: Unit-test the new detector with no threads**

```python
# append to tests/test_waitgraph.py
from lumon.deadlock import AndOrDetector


def test_andor_reduces_to_scc_when_there_are_no_or_groups():
    for edges in ({}, {"A": "B"}, {"A": "B", "B": "A"},
                  {"A": "B", "B": "C", "C": "A"}, {"A": "A"}):
        g1, g2 = graph(**edges), graph(**edges)
        assert AndOrDetector().find_cycle(g1) == SccDetector().find_cycle(g2)


def test_an_or_group_with_a_live_branch_is_not_stuck():
    # HELLY waits on ANY OF {MARK, DYLAN}; MARK waits on HELLY; DYLAN is
    # not blocked at all, so HELLY has a way forward and nothing is stuck.
    g = WaitGraph()
    g.push_or_group("HELLY", {"MARK", "DYLAN"})
    g.wait_on("MARK", {"HELLY"})
    assert AndOrDetector().find_cycle(g) is None


def test_an_or_group_whose_only_branch_cycles_is_a_deadlock():
    g = WaitGraph()
    g.push_or_group("HELLY", {"MARK"})
    g.wait_on("MARK", {"HELLY"})
    assert AndOrDetector().find_cycle(g) == ["HELLY", "MARK"]


def test_and_edge_to_a_cycle_does_not_make_the_waiter_a_member():
    g = graph(A="B", B="A")
    g.wait_on("X", {"A"})
    assert AndOrDetector().find_cycle(g) == ["A", "B"]


def test_or_group_pops_back_off():
    g = WaitGraph()
    g.push_or_group("HELLY", {"MARK"})
    g.wait_on("MARK", {"HELLY"})
    g.pop_or_group("HELLY")
    assert AndOrDetector().find_cycle(g) is None
```

Run: `python -m pytest tests/test_waitgraph.py -v`
Expected: 14 passed — still no threads anywhere in this file.

- [ ] **Step 5: Add `await_any` to `Registry` in `lumon/cell.py`**

```python
    def await_any(self, me: str, targets: Sequence[str]) -> str:
        """Block until at least one target settles; return its id.

        Registers an OR-group so detection knows this Innie has several ways
        forward. Scans `targets` in the caller's order, so when more than one
        is already settled the choice is order-independent.
        """
        with self.cond:
            while True:
                for t in targets:
                    if self._cells[t].is_settled_locked():
                        return t
                if me in self._cancelled:
                    raise Cancelled(me)
                self._graph.push_or_group(me, targets)
                try:
                    cycle = self._detector.find_cycle(self._graph)
                    if cycle:
                        self._resolve_cycle(cycle)
                        continue          # a target may now hold -1
                    self.cond.wait()
                finally:
                    self._graph.pop_or_group(me)
```

Add `Sequence` to the `typing` import and `from lumon.errors import Cancelled`
to `cell.py`.

- [ ] **Step 6: Make `any_of`/`all_of` genuinely concurrent in `lumon/resolvers/concurrent.py`**

```python
    def _quantified(self, innie_ids, pred, absorbing: bool) -> bool:
        """S8: wait on all branches at once, return the moment an
        absorbing value lands. `absorbing` is True for ANY OF (true wins)
        and False for ALL OF (false wins)."""
        remaining = list(innie_ids)
        while remaining:
            settled = self.registry.await_any(self.me, remaining)
            if self.registry.is_cancelled(self.me):
                raise Cancelled(self.me)
            remaining.remove(settled)
            with self.registry.cond:
                result = self.registry.result_locked(settled)
            if pred(unwrap(result)) is absorbing:
                return absorbing          # absorbing result wins (S8)
        return not absorbing              # ANY OF [] -> False, ALL OF [] -> True

    def any_of(self, innie_ids, pred):
        return self._quantified(innie_ids, pred, absorbing=True)

    def all_of(self, innie_ids, pred):
        return self._quantified(innie_ids, pred, absorbing=False)
```

Note the fault ordering rule from S8: `unwrap` may raise on a branch that faulted. Because `await_any` returns branches in the caller's list order among those already settled, and an absorbing value short-circuits before later branches are unwrapped, the "absorbing result wins over a pending fault" rule holds — and matches `SerialResolver`'s left-to-right walk exactly.

- [ ] **Step 7: Run the deadlock and scheduler suites**

Run: `python -m pytest tests/test_deadlock.py tests/test_scheduler.py -v`
Expected: 14 + 9 passed. **All eleven Task 12 tests must still pass unchanged** — that is the check that the generalization reduces correctly.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -v`
Expected: all pass, golden values unchanged.

- [ ] **Step 9: Commit**

```bash
git add lumon/deadlock/ lumon/cell.py lumon/resolvers/concurrent.py \
        tests/test_waitgraph.py tests/test_deadlock.py tests/test_scheduler.py
git commit -m "feat: OR-waits with short-circuit and AND/OR deadlock detection"
```

---

# Task 14: The serial oracle

**Files:**
- Create: `lumon/resolvers/serial.py`, `lumon/runners/serial.py`
- Modify: `lumon/runners/__init__.py` (export the serial runner)
- Test: `tests/test_serial.py`

**Interfaces:**
- Consumes: `execute` (Task 7), `Registry` (Task 9), `Runner` (Task 10).
- Produces: `SerialResolver`, `SerialRunner` (`name = "serial"`, auto-registered in `RUNNERS`), convenience `run_serial(innies)`.

A single-threaded, lazy, memoized, recursive evaluator that drives **the same interpreter**. Not a topological sort — dependencies are dynamic (S6), so a static order does not exist. Recursion with an on-stack set gives cycle detection for free.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serial.py
import pathlib
import pytest
from lumon.loader import load, load_path
from lumon.runners.serial import run_serial

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"

EXPECTED = {
    "1.json": {"HELLY": 20, "MARK": 5, "IRVING": 25, "BURT": 0},
    "2.json": {"DYLAN": 100, "BURT": 200},
    "3.json": {"HELLY": 8, "MARK": 16, "IRVING": 124, "BURT": 497, "DYLAN": 0},
}


def values(innies):
    return {r.innie_id: r.value for r in run_serial(innies).snapshot()}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_serial_matches_golden_values(name):
    assert values(load_path(SAMPLES / name)) == EXPECTED[name]


def test_serial_detects_a_two_cycle():
    assert values(load({"innies": [
        {"id": "A", "schedule": "LOAD 0\nADD B\nWAFFLE"},
        {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
    ]})) == {"A": -1, "B": -1}


def test_serial_detects_a_self_reference():
    assert values(load({"innies": [
        {"id": "A", "schedule": "LOAD 5\nADD A\nWAFFLE"},
    ]})) == {"A": -1}


def test_serial_defers_a_cyclic_branch_and_escapes_via_a_healthy_one():
    # S8(b). Without deferral this returns {"HELLY": -1, "MARK": -1} while
    # the concurrent run returns {"HELLY": 0, "MARK": 0} — the exact
    # mismatch Task 16 would report. Mirrors
    # test_or_branch_escapes_a_cycle_no_false_deadlock in test_deadlock.py.
    assert values(load({"innies": [
        {"id": "DYLAN", "schedule": "LOAD 1\nWAFFLE"},
        {"id": "HELLY",
         "schedule": "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK, DYLAN]\nWAFFLE"},
        {"id": "MARK", "schedule": "LOAD 0\nADD HELLY\nWAFFLE"},
    ]})) == {"DYLAN": 1, "HELLY": 0, "MARK": 0}


def test_serial_still_reports_a_cycle_when_no_branch_escapes():
    assert values(load({"innies": [
        {"id": "HELLY",
         "schedule": "LOAD 100\nWELLNESS_CHECK 5 > ANY OF [MARK]\nWAFFLE"},
        {"id": "MARK", "schedule": "LOAD 0\nADD HELLY\nWAFFLE"},
    ]})) == {"HELLY": -1, "MARK": -1}


def test_serial_result_is_independent_of_innie_declaration_order():
    forward = load({"innies": [
        {"id": "A", "schedule": "LOAD 1\nWAFFLE"},
        {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
    ]})
    backward = load({"innies": [
        {"id": "B", "schedule": "LOAD 0\nADD A\nWAFFLE"},
        {"id": "A", "schedule": "LOAD 1\nWAFFLE"},
    ]})
    assert values(forward) == values(backward) == {"A": 1, "B": 1}
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_serial.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.runners.serial'`

- [ ] **Step 3: Implement `lumon/resolvers/serial.py` and `lumon/runners/serial.py`**

```python
"""Single-threaded reference evaluator — the determinism oracle.

Drives the SAME interpreter as the concurrent path, through a Resolver that
recurses on demand instead of blocking. Cycle detection is the classic
on-stack DFS check.
"""

from __future__ import annotations

from typing import Callable, Sequence

from lumon.cell import DEADLOCK_VALUE, Registry, Result, unwrap
from lumon.interp import execute
from lumon.loader import Innie
from lumon.resolvers.base import Resolver
from lumon.runners.base import Runner, register


class SerialResolver(Resolver):
    def __init__(self, engine: "_Engine"):
        self._engine = engine

    def value(self, innie_id: str) -> int:
        return unwrap(self._engine.result_for(innie_id))

    def values(self, innie_ids: Sequence[str]) -> list[int]:
        return [self.value(i) for i in innie_ids]

    def _quantified(self, innie_ids, pred, absorbing: bool) -> bool:
        """S8(b): branches that would re-enter an Innie already under
        evaluation are DEFERRED, not resolved. The concurrent resolver
        gets this ordering for free — a cyclic branch cannot settle until
        the detector fires, so a non-cyclic branch always settles first.
        Without the deferral, this oracle reports -1 where the concurrent
        run completes normally, and Task 16 fails for the wrong reason.
        """
        deferred = []
        for innie_id in innie_ids:
            if self._engine.would_reenter(innie_id):
                deferred.append(innie_id)
                continue
            if pred(self.value(innie_id)) is absorbing:
                return absorbing
        for innie_id in deferred:         # these resolve to -1 (S9)
            if pred(self.value(innie_id)) is absorbing:
                return absorbing
        return not absorbing              # ANY OF [] -> False, ALL OF [] -> True

    def any_of(self, innie_ids, pred: Callable[[int], bool]) -> bool:
        return self._quantified(innie_ids, pred, absorbing=True)

    def all_of(self, innie_ids, pred: Callable[[int], bool]) -> bool:
        return self._quantified(innie_ids, pred, absorbing=False)


class _Engine:
    def __init__(self, innies: list[Innie]):
        self._programs = {i.id: i.program for i in innies}
        self._registry = Registry(i.id for i in innies)
        self._on_stack: list[str] = []

    def would_reenter(self, innie_id: str) -> bool:
        """True if resolving this Innie would recurse into one already
        being evaluated — i.e. this branch is cyclic (S8b)."""
        return (
            innie_id in self._on_stack
            and self._registry.cell(innie_id).peek() is None
        )

    def result_for(self, innie_id: str) -> Result:
        cell = self._registry.cell(innie_id)
        existing = cell.peek()
        if existing is not None:
            return existing               # memoized

        if innie_id in self._on_stack:
            # On-stack means a cycle. Settle every member of the cycle
            # to -1, matching S9 and the concurrent SCC rule.
            start = self._on_stack.index(innie_id)
            for member in self._on_stack[start:]:
                member_cell = self._registry.cell(member)
                if member_cell.peek() is None:
                    member_cell.commit(DEADLOCK_VALUE)
            return self._registry.cell(innie_id).peek()

        self._on_stack.append(innie_id)
        try:
            outcome = execute(self._programs[innie_id], SerialResolver(self))
        except BaseException as error:  # noqa: BLE001
            if cell.peek() is None:
                cell.fault(error)
            return cell.peek()
        finally:
            self._on_stack.pop()

        if cell.peek() is None:           # not settled as a cycle member
            if outcome.staged is None:
                cell.commit_void()        # S2
            else:
                cell.commit(outcome.staged)
        return cell.peek()

    def run(self) -> Registry:
        for innie_id in self._registry.ids():
            self.result_for(innie_id)
        return self._registry


@register
class SerialRunner(Runner):
    name = "serial"

    def run(self, innies: list[Innie]) -> Registry:
        return _Engine(innies).run()


def run_serial(innies: list[Innie]) -> Registry:
    return SerialRunner().run(innies)
```

The `finally: self._on_stack.pop()` runs after the `except` returns, which is what keeps the stack consistent when a fault unwinds.

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_serial.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add lumon/resolvers/serial.py lumon/runners/serial.py \
        lumon/runners/__init__.py tests/test_serial.py
git commit -m "feat: serial lazy-recursive reference evaluator"
```

---

# Task 15: CLI

**Files:**
- Create: `lumon/cli.py`, `lumon/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv) -> int`; `format_results(registry) -> dict`.
- Output shape: `{"innies": [{"id": ..., "result": <int|null>, "error": <str|absent>}]}` in input order (S11).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import json
import pathlib
import pytest
from lumon.cli import main

SAMPLES = pathlib.Path(__file__).parent.parent / "Hometask Backend"


def run(capsys, *args):
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_runs_a_sample_and_prints_json(capsys):
    code, out = run(capsys, str(SAMPLES / "3.json"))
    assert code == 0
    assert out["innies"] == [
        {"id": "HELLY", "result": 8},
        {"id": "MARK", "result": 16},
        {"id": "IRVING", "result": 124},
        {"id": "BURT", "result": 497},
        {"id": "DYLAN", "result": 0},
    ]


def test_serial_mode_agrees_with_concurrent(capsys):
    _, conc = run(capsys, str(SAMPLES / "3.json"))
    _, ser = run(capsys, str(SAMPLES / "3.json"), "--mode", "serial")
    assert conc == ser


def test_void_innie_reports_null(tmp_path, capsys):
    path = tmp_path / "void.json"
    path.write_text(json.dumps({"innies": [{"id": "A", "schedule": "LOAD 5"}]}))
    code, out = run(capsys, str(path))
    assert code == 0
    assert out["innies"] == [{"id": "A", "result": None}]


def test_faulted_innie_reports_error_and_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "fault.json"
    path.write_text(json.dumps(
        {"innies": [{"id": "A", "schedule": "LOAD 5\nMODULO 0\nWAFFLE"}]}))
    code, out = run(capsys, str(path))
    assert code == 1
    assert out["innies"][0]["result"] is None
    assert "MODULO by zero" in out["innies"][0]["error"]


def test_load_error_exits_nonzero_with_a_message(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(
        {"innies": [{"id": "A", "schedule": "LOAD 1\nADD GHOST\nWAFFLE"}]}))
    code = main([str(path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "GHOST" in err
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lumon.cli'`

- [ ] **Step 3: Implement `lumon/cli.py`**

```python
"""Read a work schedule, run it, print the registry as JSON."""

from __future__ import annotations

import argparse
import json
import sys

from lumon.cell import Registry
from lumon.errors import LumonError
from lumon.loader import load_path
from lumon.runners import RUNNERS       # importing this registers both runners


def format_results(registry: Registry) -> dict:
    innies = []
    for result in registry.snapshot():          # input order (S11)
        entry: dict = {"id": result.innie_id, "result": None}
        if result.is_fault:
            entry["error"] = _describe(result.error)
        elif not result.is_void:
            entry["result"] = result.value
        innies.append(entry)
    return {"innies": innies}


def _describe(error: BaseException) -> str:
    """Follow the __cause__ chain so the message reads
    'BURT failed because DYLAN failed because MODULO by zero'."""
    parts = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__
    return " because ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumon")
    parser.add_argument("schedule", help="path to the work schedule JSON")
    parser.add_argument(
        # Choices come from the registry, so a new Runner needs no edit here.
        "--mode", choices=sorted(RUNNERS), default="concurrent",
        help="concurrent (default) or the single-threaded reference evaluator",
    )
    args = parser.parse_args(argv)

    try:
        innies = load_path(args.schedule)
    except (LumonError, OSError, json.JSONDecodeError) as error:
        print(f"lumon: {error}", file=sys.stderr)
        return 2

    registry = RUNNERS[args.mode]().run(innies)
    output = format_results(registry)
    print(json.dumps(output, indent=2))
    return 1 if any("error" in i for i in output["innies"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

```python
# lumon/__main__.py
from lumon.cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Run tests and confirm they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 5 passed

- [ ] **Step 5: Run it by hand on all three samples**

```bash
python -m lumon "Hometask Backend/1.json"
python -m lumon "Hometask Backend/2.json"
python -m lumon "Hometask Backend/3.json"
```
Expected: the golden values, formatted as JSON.

- [ ] **Step 6: Commit**

```bash
git add lumon/cli.py lumon/__main__.py tests/test_cli.py
git commit -m "feat: CLI with concurrent and serial modes"
```

---

# Task 16: The determinism check — fuzz corpus, jitter, and the watchdog

**Files:**
- Create: `tests/corpus.py`, `tests/test_determinism.py`
- Modify: `lumon/runners/concurrent.py` (watchdog on join)

**Interfaces:**
- Produces: `generate(seed: int) -> dict` (a random schedule), and the cross-mode equality assertion that guards the whole design.

This is the payoff. Any mismatch between modes means state is leaking outside the Registry.

- [ ] **Step 1: Add the watchdog to `ConcurrentRunner`**

The watchdog is a **bug detector, not a fallback**. It must never fire in a correct implementation, so it raises rather than papering over the problem by settling cells to -1.

```python
# in lumon/runners/concurrent.py
import time

from lumon.cell import PendingAtSnapshot
from lumon.errors import WatchdogTimeout

WATCHDOG_SECONDS = 30.0


@register
class ConcurrentRunner(Runner):
    name = "concurrent"

    def __init__(self, timeout: float = WATCHDOG_SECONDS):
        self.timeout = timeout

    def run(self, innies: list[Innie]) -> Registry:
        registry = Registry(i.id for i in innies)
        threads = [
            threading.Thread(
                target=run_innie, args=(innie, registry),
                name=f"outie-{innie.id}", daemon=True,
            )
            for innie in innies
        ]
        for t in threads:
            t.start()

        deadline = time.monotonic() + self.timeout
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))

        stragglers = [t.name for t in threads if t.is_alive()]
        if stragglers:
            pending = [
                r.innie_id for r in registry.snapshot()
                if isinstance(r.error, PendingAtSnapshot)
            ]
            raise WatchdogTimeout(
                f"no progress after {self.timeout}s; threads still running: "
                f"{sorted(stragglers)}; cells still pending: {sorted(pending)}"
            )
        return registry
```

`RUNNERS["concurrent"]` still resolves — `@register` keys on `name`, and re-decorating the same class name replaces the entry.

- [ ] **Step 2: Write the corpus generator**

```python
# tests/corpus.py
"""Random schedule generator for the determinism fuzz test.

Generates a random DAG so results are well-defined, then optionally
injects back-edges to create cycles. Both modes must agree either way.
"""

import random

OPS = ["ADD", "MULTIPLY", "MODULO"]


def generate(seed: int, n: int | None = None, allow_cycles: bool = False) -> dict:
    rng = random.Random(seed)
    n = n or rng.randint(50, 200)
    ids = [f"I{i}" for i in range(n)]
    innies = []

    for idx, innie_id in enumerate(ids):
        earlier = ids[:idx]
        lines = [f"LOAD {rng.randint(-50, 50)}"]

        for _ in range(rng.randint(0, 4)):
            choice = rng.random()

            if choice < 0.30 or not earlier:
                op = rng.choice(OPS)
                operand = rng.randint(1, 20)     # never 0: MODULO 0 faults
                lines.append(f"{op} {operand}")

            elif choice < 0.50:
                lines.append(f"ADD {rng.choice(earlier)}")

            elif choice < 0.65:
                picks = rng.sample(earlier, min(len(earlier), rng.randint(1, 3)))
                lines.append(f"ADD [{', '.join(picks)}]")

            elif choice < 0.78:
                lhs = rng.choice(earlier)
                picks = rng.sample(earlier, min(len(earlier), rng.randint(1, 3)))
                quant = rng.choice(["ANY", "ALL"])
                op = rng.choice([">", "<", ">=", "<=", "==", "!="])
                lines.append(
                    f"WELLNESS_CHECK {lhs} {op} {quant} OF [{', '.join(picks)}]"
                )

            elif choice < 0.90:
                picks = rng.sample(earlier, min(len(earlier), rng.randint(1, 3)))
                lines.append(
                    f"CONDITIONAL_ADD [{', '.join(picks)}] "
                    f"IF {rng.choice(earlier)} > {rng.randint(-50, 50)}"
                )

            else:
                body_target = rng.choice(earlier)
                lines.append(f"SHIFT {rng.randint(0, 3)} TIMES")
                lines.append(f"ADD {body_target}")
                lines.append("END_SHIFT")

        if allow_cycles and idx > 0 and rng.random() < 0.15:
            lines.append(f"ADD {rng.choice(ids[idx:])}")   # back-edge

        lines.append("WAFFLE")
        innies.append({"id": innie_id, "schedule": "\n".join(lines)})

    return {"innies": innies}
```

- [ ] **Step 3: Write the determinism test**

```python
# tests/test_determinism.py
import pytest
from lumon.loader import load
from lumon.runners import run_concurrent, run_serial
from tests.corpus import generate


def fingerprint(registry) -> list[tuple]:
    """Comparable, order-preserving view of a whole registry."""
    out = []
    for r in registry.snapshot():
        kind = "fault" if r.is_fault else "void" if r.is_void else "value"
        out.append((r.innie_id, kind, r.value, type(r.error).__name__ if r.error else None))
    return out


@pytest.mark.parametrize("seed", range(200))
def test_concurrent_matches_serial_on_acyclic_corpus(seed):
    innies = load(generate(seed))
    assert fingerprint(run_concurrent(innies)) == fingerprint(run_serial(innies))


@pytest.mark.parametrize("seed", range(200))
def test_concurrent_matches_serial_with_cycles(seed):
    innies = load(generate(seed, n=40, allow_cycles=True))
    assert fingerprint(run_concurrent(innies)) == fingerprint(run_serial(innies))


@pytest.mark.parametrize("seed", [0, 7, 42, 99, 123])
def test_concurrent_is_stable_across_repeated_runs(seed):
    innies = load(generate(seed, n=60, allow_cycles=True))
    first = fingerprint(run_concurrent(innies))
    for _ in range(50):
        assert fingerprint(run_concurrent(innies)) == first
```

- [ ] **Step 4: Run it**

Run: `python -m pytest tests/test_determinism.py -v`
Expected: 405 passed.

**If any seed mismatches, do not weaken the test.** Reproduce it in isolation with `generate(seed)` written to a temp JSON, run both modes through the CLI, and diff. The mismatch is a real bug — usually one of: short-circuit changing a *value* rather than a wait (S8 violated), a cycle set that depends on which thread detected it (S9/Task 12), or state living outside the Registry.

- [ ] **Step 5: Add interleaving jitter to shake out races repetition alone will not find**

Raw repetition explores few interleavings under the GIL. Force more by lowering the switch interval, which makes preemption far more frequent:

```python
# append to tests/test_determinism.py
import sys


@pytest.fixture
def aggressive_switching():
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(original)


@pytest.mark.parametrize("seed", range(50))
def test_determinism_under_aggressive_thread_switching(seed, aggressive_switching):
    innies = load(generate(seed, n=40, allow_cycles=True))
    assert fingerprint(run_concurrent(innies)) == fingerprint(run_serial(innies))
```

Run: `python -m pytest tests/test_determinism.py -v`
Expected: 455 passed.

- [ ] **Step 6: Run the entire suite**

Run: `python -m pytest -v`
Expected: everything passes. Record the count in the commit message.

- [ ] **Step 7: Commit**

```bash
git add tests/corpus.py tests/test_determinism.py lumon/runners/concurrent.py
git commit -m "test: cross-mode determinism over a 200-seed fuzz corpus"
```

---

# Appendix: Things to watch for

Carried forward from the original draft plan, corrected and extended.

- **Do not let an Innie's Cell be written by two paths.** Exactly one of: normal commit, void commit, fault, or deadlock resolution. `DoubleSettle` exists to catch violations — never weaken it, guard at the call site (Task 12).
- **Empty registry entries.** "Zero or more WAFFLE commands" means an Innie can finish having staged nothing. Publish VOID explicitly (S2). A pending Cell is always a bug.
- **Daemon threads.** `daemon=True` so a hung run cannot wedge the test suite. The watchdog (Task 16) converts a hang into a diagnostic failure instead of an infinite wait.
- **`time.sleep` in tests is a smell.** Use `threading.Barrier` and `Event` to force ordering deterministically (Task 9 does). The one legitimate timing knob is `sys.setswitchinterval`, which changes *how often* threads switch without making any test depend on a wall-clock duration.
- **Never assert on timing.** "ANY OF short-circuits" cannot be proven by measuring elapsed time; prove it structurally — `test_or_branch_escapes_a_cycle_no_false_deadlock` (Task 13) can only pass with a genuine OR-wait.
- **`interp.py` must never import `threading`.** Enforced by `test_interp_never_imports_threading` (Task 7). If you find yourself wanting to, the thing you need belongs on the `Resolver`.
- **Do not add a static dependency pre-pass.** It is tempting and it is wrong (Task 12, "Why not static analysis"). If you want one, it may only be a *diagnostic*, never an input to execution.
- **Sort before returning any set that reaches a result.** `_find_cycle` sorts its SCC and `_stuck_set` iterates `sorted(...)` for exactly this reason: iteration order of a Python set is not a stable contract across the values it holds.
- **Never annotate a pydantic field with a base model class.** `tuple[Instruction, ...]` silently rebuilds children as `Instruction` and drops their fields. Use `AnyInstruction`. This is the one pydantic behaviour in the project that fails quietly instead of loudly.
- **Don't put `is_cancelled()` on `Resolver`.** It is the natural-looking place and it is wrong: `SerialResolver` would carry a dead stub and the interpreter would gain a concurrency-shaped hole. Cancellation travels as an exception through the methods that already exist.
- **Don't give instructions an `execute()` method.** The `HANDLERS` table gets the same open/closed benefit without coupling the ISA to the interpreter, which would drag execution into the loader's validation pass.
- **The serial oracle is not "the obvious left-to-right version".** Its one non-obvious behaviour — deferring branches that would re-enter an Innie under evaluation (S8b) — is load-bearing. Delete it and Task 16 fails on schedules where a quantified condition has both a cyclic and a healthy branch. If you find yourself simplifying `SerialResolver._quantified`, re-read S8(b) first.

---

# Self-Review: Spec Coverage

| Spec requirement | Covered by |
|---|---|
| Parse the work schedule JSON | Task 6 |
| `LOAD` / `ADD` / `MULTIPLY` / `MODULO` / `WAFFLE` | Tasks 3, 7 |
| `ADD [list]` / `MULTIPLY [list]` | Tasks 3, 7, 8 (S3) |
| Bare `ADD HELLY` | Tasks 3, 8 (S3) |
| `WELLNESS_CHECK`, both forms | Tasks 4, 8 (S5) |
| `CONDITIONAL_ADD [list] IF cond` | Tasks 4, 8 (S6) |
| Simple comparisons, all six operators | Tasks 4, 8 (S7) |
| `ANY OF` / `ALL OF` | Tasks 4, 8, 13 (S7) |
| Short-circuit evaluation (Req. 4) | Task 13 (S8) |
| `SHIFT n TIMES` / `END_SHIFT`, nested | Tasks 5, 7 (S12) |
| Concurrent execution, multiple workers (Req. 1) | Task 10 |
| Atomic workday, no partial products (Req. 2) | Tasks 7, 9, 10 (S1) |
| Dependency resolution, only when needed (Req. 3) | Tasks 10, 13 (S6) |
| Latest-product semantics (Req. 5) | Tasks 7, 10, 11 (S1, `2.json`) |
| Deterministic results (Req. 6) | Tasks 14, 16 (S8) |
| Circular dependency → WAFFLE -1 | Tasks 12, 13 (S9) |
| Determinism despite deadlock | Task 12 (SCC), Task 16 |
| Invalid references | Task 6 (S11) |
| Thread safety for shared state | Task 9 |
| No deadlocks or starvation; all Innies clock out | Tasks 10, 12, 13, 16 (watchdog) |
| Print the registry | Task 15 |

## Self-Review: Design Coverage

| Concern | Where it is addressed |
|---|---|
| SRP — Registry doing three jobs | Task 9 / 12: `Registry` + `WaitGraph` + `DeadlockDetector` |
| OCP — instruction dispatch | Task 7: `HANDLERS` table |
| OCP — opcode parsing | Task 3/4: `OPCODES` table |
| OCP — execution strategy | Task 10/15: `RUNNERS` table, `--mode` from its keys |
| LSP — pydantic base-class coercion | Task 2: `AnyInstruction` + two pinning tests |
| LSP — resolver substitutability | Task 16: cross-mode determinism over 200 seeds |
| ISP — no cancellation on `Resolver` | Task 7 docstring; Task 12 uses an exception |
| DIP — interp independent of threads | Task 7: import-guard test over `lumon/interp/` |
| DIP — Registry independent of the algorithm | Task 12: injected `DeadlockDetector` |
| pydantic at the trust boundary | Task 6: `InnieSpec` / `WorkSchedule` |
| pydantic kept out of lock-holding types | Task 9: `Cell`/`Registry` are plain classes |
