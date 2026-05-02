---
abstract: "Non-obvious queue-up-for-codex facts: usage is command-driven,
           CODEX.md is explicitly injected/restored, and Codex session lookup
           reads ~/.codex state without writing to it."
---

# Semantic memory

## Things that look like dead code but are not

- Click commands and FastAPI route handlers are decorator-registered.
- `Task.status` is kept on disk for human grep/debug value.
- `tick_seconds` in `runner.start_runner` is a test seam.

## Usage flow

- `usage_check_command.py` runs `CODEX_QUEUE_USAGE_COMMAND` without a shell.
- The command must print `used_pct` and `reset_minutes`.
- `usage_check.py` appends one CSV row per logical check.
- `runner.py` recomputes the real burn decision from pct/reset/anchor; the
  `UsageCheckResult.status` string is display-only.

## Execution flow

- `injector.py` writes `CODEX.md`, backing up any existing file.
- `executor.py` runs `codex exec --full-auto`.
- Fresh tasks pass `-C <project_dir>`; resumed tasks run from the project cwd.
- Cleanup of injected `CODEX.md` must remain in a `finally` block.

## Session lookup

- `sessions.py` reads `~/.codex/state_5.sqlite` first, then scans
  `~/.codex/sessions/**/*.jsonl`.
- Only UUID session ids are supported for dashboard resume summaries.
- Transcript cwd must match the task's resolved project directory.

## Subprocess env hygiene

- `.env` is loaded into `config._DOTENV`, not `os.environ`.
- `config.subprocess_env()` strips queue-private keys before spawning Codex.
- Always use `get_env()` for queue config.
