# CLAUDE.md

## Project

An interpreter for the Lumon Innie Task Scheduler — a concurrent execution system where each
"Innie" runs an assembly-like schedule, publishes results with `WAFFLE`, and may block on other
Innies' published values. Results must be deterministic despite thread-per-Innie execution, and
circular dependencies must resolve to `-1` rather than hang.

- **Spec (authoritative):** `Hometask Backend/excersice.txt`. Sample inputs: `Hometask Backend/1.json`, `2.json`, `3.json`.
- **Implementation plan (authoritative):** `PLAN.md` (mirrored at `docs/superpowers/plans/2026-08-20-lumon-innie-scheduler.md`). Do not rewrite it without being asked; follow it task by task.
- **Layout:** the `lumon/` package at the repo root, tests in `tests/`.
- **Hard constraint:** stdlib only at runtime — `lumon/` must have zero third-party imports, and `[project].dependencies` stays empty. `lumon/interp.py` must never import `threading`.

## Rules

- **Ask before assuming**: When a task is ambiguous, requirements are unclear, or you're unsure about the intended approach, ask clarifying questions before proceeding. Don't guess — it's better to ask than to build the wrong thing.
- **Don't write code unless asked**: Default to investigation and explanation only. Do not write, edit, or commit code unless the user explicitly asks you to. Answer questions, diagnose issues, and propose solutions verbally first.
- **Write tests for code changes**: When you change or add code, add or update tests that cover the new behavior. Before opening a `dev` PR, run only the specific tests your change may have affected (`uv run pytest tests/test_parser.py`) — don't run the full suite, ruff, or mypy for every change. The `dev` → `main` release PR is this project's quality gate; see **Releasing to `main`** below for what must pass there.
- **Avoid default values**: Try to avoid giving variables and parameters default values unless a default is specifically needed. Prefer making callers pass values explicitly so intent is clear and missing values surface as errors instead of being silently filled in.
- **Environment variables**: Never read environment variables (`os.environ`, `os.getenv`) outside of a single central config module. Define and validate all configuration in one place and import it everywhere else. This project should need approximately none — inputs arrive as a schedule file argument, not as configuration.
- **Never assert on timing**: Don't prove concurrency behaviour with `time.sleep` or elapsed-time assertions. Force ordering deterministically with `threading.Barrier` / `Event`, and prove short-circuiting structurally. See the "Traps" section at the end of `PLAN.md`.
- **Workflow**: At the start of a task, pull the latest `dev`. There is no CI on PRs into `dev` — open the PR and **merge it yourself** (`gh pr merge <num> --merge`) once it's mergeable. Resolve any merge conflicts first. After merging, sync your local `dev` with the remote in the **main project directory** (where `dev` is checked out), not in a worktree — git won't update a branch checked out elsewhere.
- **No CI/CD is configured**: This repo has no GitHub Actions workflows, by choice. Nothing runs automatically on push or on a PR, so no check will ever catch a mistake for you — the quality gate below is entirely manual and you are the one who has to run it. Don't tell the user a pipeline will verify something.
- **No database**: There is no database, no migrations, and no MCP servers wired up. If a task seems to need persistence, stop and ask.
- **Releasing to `main`**: Merging `dev` → `main` is a release. Always ask the user for explicit confirmation before opening a `dev` → `main` PR. Because there is no CI, run the full gate locally first and paste the output into the PR:
  ```
  uv run ruff check .
  uv run mypy lumon tests
  uv run pytest
  ```
  All three must be clean. Then merge the PR yourself once the user has given the go-ahead; never release to `main` without it.

## Tools

- **uv** — the project and dependency manager. Run everything through it: `uv run pytest`, `uv run ruff check .`, `uv run mypy lumon tests`, `uv add --dev <pkg>`. Never `pip install` into the environment by hand, and never add a runtime dependency (see the stdlib-only constraint above).
- **ruff** — linter and import sorter, configured in `pyproject.toml` (line length 100; `E`, `F`, `I`, `B`, `UP`, `SIM`). Autofix with `uv run ruff check --fix .`.
- **mypy** — type checker in `strict` mode, configured in `pyproject.toml`. New code is expected to be fully annotated.
- **pytest** — the test suite, in `tests/`.
- **`gh` CLI** — authenticated as `barakolshe`. Use it for PRs and repo operations against `barakolshe/frame-assignment` (public).
- No MCP servers, dashboards, or external services are configured for this project.

## Issues (this project's task board)

This project's issues live in a JSON file at `.forq/issues.json` — a JSON
array of issue objects. The Forq VS Code extension renders them as a
board and launches a Claude session for each issue moved to `todo`. You manage
issues by editing this file directly.

Each issue object has this shape:

```json
{
  "id": "<uuid>",
  "title": "Short, specific, self-explanatory title",
  "description": "A few sentences on the desired behavior or the problem.",
  "type": "feature" | "bug",
  "mode": "auto" | "plan",
  "assigned_to": "agent" | "human",
  "status": "backlog" | "todo" | "in_progress" | "in_review" | "done" | "canceled",
  "order": 1,
  "created_at": "<ISO-8601 timestamp>",
  "updated_at": "<ISO-8601 timestamp>"
}
```

Rules when editing the file:
- Generate a fresh UUID for `id`.
- New issues start in `backlog` with `mode` set to `"auto"` so they run
  autonomously. `order` is the position within a status column
  (use max existing order in that column + 1).
- Always update `updated_at` when you change an issue.
- When you finish work on an issue assigned to you, set its `status` to
  `in_review` — a human reviews before it becomes `done`. Do NOT set it to
  `done` yourself.
