---
abstract: "Strict no-over-engineering rules. Pyflakes clean before commit;
           no defensive try/except for impossible errors; single source of
           truth for constants; comments must reflect reality."
---

# Behavior rules

## Always

- Run `python -m pytest tests/` before considering work done. All 132 tests must pass.
- Run `python -m pyflakes src/queue_worker/` before committing. Fix every warning
  (unused imports, pointless f-strings without placeholders, undefined names).
- When you change a function signature used by the runner, the web routes,
  AND the CLI: search all three for the old signature. Use `grep`, not memory.
  (A previous session shipped a `TypeError` to `web.py` by missing this.)
- When changing comments / docstrings: also fix neighboring stale wording.
  Stale "Playwright"/"scrape"/"HH:55" mentions have shipped to main multiple times.
- Use the existing shared helpers: `queue_ops.list_tasks`, `injector.render_task_context`,
  `logger.read_log_lines`. Don't re-inline what's already extracted.
- Match the existing voice: terse, no headers in short docstrings, no emojis,
  one short comment for *why* not *what*.

## Never

- **Never add `requests`, `httpx`, `playwright`, or any browser automation.**
  The HTTP backend uses stdlib `urllib`. The Playwright fallback was deliberately
  removed (commit 8b935ea); reintroducing it would undo a major win.
- **Never add defensive `try/except` for errors that can't actually be raised**
  in the protected block. Past examples codex caught and removed: `KeyError`
  around code that uses `.get()` only; `IndexError` around code already
  length-guarded; `Exception` around `bytes.decode(errors='ignore')`.
- **Never use `getattr(obj, 'field', default)` on a dataclass field that's
  always present.** Just `obj.field`. Codex flagged two of these in `runner.py`.
- **Never add a function parameter with a default that no production caller
  overrides.** Hidden test seams are OK if they're actually used by tests;
  delete the parameter otherwise.
- **Never `from typing import Tuple, List, Dict`.** Use the built-in generic
  syntax `tuple[str, ...]`, `list[Task]`, `dict[str, int]`. Python 3.11+.
- **Never commit without the user explicitly asking.** "Make a commit" / "commit and push"
  / similar. Otherwise just leave the changes staged or unstaged.
- **Never push to main without confirmation** — even if asked to push, confirm the
  target branch unless the user explicitly said "push to main". The harness will
  block direct-to-main pushes anyway.
- **Never write `f"..."` strings without placeholders.** Plain `"..."`. Pyflakes catches it.

## Code-review checklist (apply on every diff you write, before showing it)

The patterns below have been caught and removed multiple times by codex. Pre-empt them:

1. **Dead arguments / functions** — every function arg should have at least one
   production caller passing a non-default. Every defined function should be
   called somewhere besides tests.
2. **Write-only fields** — every dataclass field should be read by *some*
   consumer, not just set by the producer. Exception: `Task.status` (kept
   intentionally for `grep` value on disk; documented in `semantic.md`).
3. **Single-value config knobs** — if a "knob" only ever takes one value,
   inline it. Tunable constants like `BURN_USAGE_THRESHOLD_PCT` are different —
   they live in one place and get imported.
4. **Single-implementation protocols / abstractions** — don't introduce a base
   class or Protocol if there's one concrete impl. Refactor when there are two.
5. **Dead branches** — a state-machine branch that the producer can never
   trigger is dead. Delete it; don't comment "shouldn't happen".
6. **Stale comments** — every doc / comment / log message that names a function,
   flag, or behavior must match the current code. Stale comments are bugs.
7. **Defensive validation at trust boundaries you control** — don't validate
   data you produced and parsed yourself. Validate at OS / network / user-input
   boundaries only.
8. **CLI / web duplication** — if the CLI command and the REST route do the
   same logical operation, extract to `queue_ops.py` / `injector.py` / `logger.py`.

## Tooling

- **Codex review**: invoke directly via `codex exec "<prompt>" -C $(pwd) -s read-only -c 'model_reasoning_effort="medium"'`.
  Skip the heavy `/codex` skill ceremony when you just want a focused review —
  the skill's preamble fires telemetry / writing-style / routing prompts that
  derail auto-mode work.
- **Pyflakes**: `pip install pyflakes && python -m pyflakes src/queue_worker/`.
  Catches unused imports + pointless f-strings instantly. Add to your pre-commit ritual.
- **Gitleaks**: `gitleaks detect --source . --no-git --verbose`. Run before any
  push if you've touched config / docs / templates. Test fixtures use clearly-fake
  `sk-ant-test-fake-...` strings that gitleaks correctly skips.

## Refactor discipline

- **Refactors do not change behavior.** If you find behavior worth changing
  during a refactor, split it into a separate commit with a clear "behavior:"
  prefix in the message. (One previous refactor accidentally improved CLI
  `logs` to print "No log for X" on empty filter results — would have been
  cleaner as a follow-up commit.)
- **Codex-review every non-trivial diff** before committing. Two passes have
  caught: a real `TypeError` bug introduced in a "cleanup" commit, multiple
  stale comments, defensive `getattr` usage, and ~7 simplifications.
