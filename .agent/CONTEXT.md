---
abstract: "Python 3.11+ tool: queues claude -p subprocesses, runs them when
           Claude.ai usage is about to reset. ~13 modules in src/queue_worker/,
           single-file Alpine.js dashboard, HTTP-only usage check via cookie API."
---

# Project context

## What this is

`queue-up-for-claude` (PyPI name: `queue-worker`) is a usage-aware Claude
Code job queue. The full pitch is in `README.md`. Architecture overview is
in `docs/runner-state-machine.md`.

## Tech stack

- **Language**: Python 3.11+ (uses `tuple[str, list[str]]`, `dict[str, int]`
  built-in generic syntax — don't `from typing import Tuple`)
- **CLI**: Click 8.1+
- **Web**: FastAPI 0.115+ with lifespan async context manager. Pydantic v2 BaseModel for request bodies.
- **Frontend**: Alpine.js single-file SPA at `src/queue_worker/static/index.html`
  (~62 KB). No build step, no bundler, no React.
- **HTTP client**: stdlib `urllib`. Do NOT add `requests` or `httpx`.
- **YAML**: PyYAML — `safe_load` everywhere, never `load`.
- **Tests**: pytest + pytest-mock. 132 tests, all should pass on every commit.

**No browser dependency.** Earlier versions had a Playwright/CDP fallback
that drove a real Chrome instance to scrape the rendered usage page; it was
removed in commit 8b935ea. Don't reintroduce it without strong justification.

## Module map

```
src/queue_worker/
├── cli.py             ← Click commands; embeds `.agent/` template strings (~250 LOC of templates)
├── web.py             ← FastAPI app, lifespan, background runner thread, REST endpoints
├── runner.py          ← state machine (chilling/burning), reset anchor, scheduling
├── usage_check.py     ← thin dispatcher: kick recovery + CSV append + status decide()
├── usage_check_http.py← HTTP backend: org resolve, /usage GET, error code mapping
├── executor.py        ← claude -p subprocess: spawn, monitor, kill, checkpoint detection
├── injector.py        ← CLAUDE.md builder + inject/cleanup with backup
├── queue_ops.py       ← task lifecycle (create/begin/done/fail/stall/retry/remove)
├── profiles.py        ← capability resolution from config/profiles.yaml
├── task.py            ← Task / TaskBudget / CapsOverride dataclasses + YAML I/O
├── auth.py            ← password gate + brute-force lockout for the dashboard
├── file_browser.py    ← list/read/raw helpers (NOT sandboxed — see security.md)
├── sessions.py        ← locate Claude transcripts under ~/.claude/projects/<slug>/
├── lock.py            ← per-task lock files in queue/running/
├── logger.py          ← daily rolling logger + `read_log_lines` shared helper
├── config.py          ← paths, private .env loader, subprocess_env() filter
└── static/            ← index.html (Alpine SPA) + login.html
```

## Key architectural choices

1. **`config.bootstrap()`** is called by both CLI and web entry points. It
   sets module globals: `lock.RUNNING_DIR`, `queue_ops.QUEUE_DIR`,
   `profiles.PROFILES_PATH`. Don't refer to these constants before `bootstrap()`
   has run.

2. **Single source of truth for burn thresholds**:
   `BURN_USAGE_THRESHOLD_PCT = 30` and `BURN_RESET_WINDOW_MIN = 70` live in
   `usage_check.py` and are imported by `runner.py`. Don't re-hardcode 30/70.

3. **The runner re-decides burn eligibility** with anchor-aware logic in
   `runner.py:_effective_burn_minutes` + the burn check around line 590. The
   `UsageCheckResult.status` string ("NEED TO BURN TOKEN !" vs "Chilling") is
   only a label — never base actual decisions on it.

4. **One usage check at a time**: `_usage_check_lock` prevents concurrent
   `claude -p "hi"` kicks (would burn the kick on an already-active session)
   AND CSV row interleave. Not for thread-safety of urllib.

5. **One task at a time**: `_execute_lock` serializes `claude -p` invocations
   across the background runner, "Run All (once)", and "Run This Task".

6. **CLAUDE.md injection** is backup/restore: original (if any) gets renamed
   to a PID-stamped path before injection; restored after. `cleanup_claude_md`
   MUST run in a `finally` block.

7. **YAML forward-compat**: `parse_task` reads via `raw.get(...)` so adding
   or removing optional Task fields doesn't break old YAMLs. Preserve this.

## REST API surface

Full reference: `docs/web-dashboard.md`. Key endpoints:

- `GET  /api/status` — runner state + queue counts (consumed by dashboard polling)
- `POST /api/check-usage` — manual usage check
- `GET  /api/tasks?status=...` — list (uses `queue_ops.list_tasks` shared helper)
- `GET  /api/context/{id}` — render the CLAUDE.md a task would receive (uses `injector.render_task_context`)
- `GET  /api/logs?date=&task_id=` — daily logs (uses `logger.read_log_lines`)

Those three shared helpers were extracted to dedupe with the CLI. Don't
inline them again.

## Security boundaries

- `/api/files/*` is NOT sandboxed. `..` works. Fine on loopback / Tailscale,
  unsafe to expose publicly. Documented in `docs/security.md`.
- Dashboard auth (optional) is in `auth.py` — per-IP + global brute-force
  lockouts. `CF-Connecting-IP` only honored when the request peer is loopback.
- The redactor in `usage_check_http.py:redact()` strips `sk-ant-` keys and
  emails from any string sent to the dashboard. Defense in depth — the source
  is supposed to never leak in the first place.

## What runs where

- **`queue-worker` (Click)** — CLI entry point, all commands.
- **`queue-worker-web` (FastAPI)** — web dashboard + embedded background runner thread.
- **DON'T run both at the same time** against the same queue dir; both would
  try to process tasks. Pick one.
