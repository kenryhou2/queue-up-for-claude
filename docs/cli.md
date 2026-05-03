# CLI

Entry point: `codex-queue` after `pip install -e .`. The root wrapper
`./codex-queue` runs the same CLI through the repo virtualenv, so examples can
use either form from the repo root.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
chmod 0600 .env
./codex-queue init ~/projects/my-app
./codex-queue add ~/projects/my-app "Add input validation to /signup"
./codex-queue run
```

Usage-aware burning requires one usage backend. For the built-in ChatGPT Codex
HTTP backend, get the `__Secure-next-auth.session-token` cookie from
`https://chatgpt.com`, store it, and enable the backend:

```bash
./codex-queue set-chatgpt-token
printf '\nCODEX_QUEUE_USAGE_BACKEND=codex_http\n' >> .env
./codex-queue check-usage
```

For a local usage script instead, leave `CODEX_QUEUE_USAGE_BACKEND` unset and
set `CODEX_QUEUE_USAGE_COMMAND` in `.env`. The command must print:

```json
{"used_pct": 71, "reset_minutes": 58}
```

## Commands

| Command | What it does |
|---|---|
| `add` | Creates a pending task YAML file for a project prompt. |
| `begin` | Moves a pending task to `running/`; mainly for manual recovery. |
| `check-usage` | Runs the configured usage backend once and appends `usage_history.csv`. |
| `compile` | Writes a project `CODEX.md` for daytime interactive Codex use. |
| `context` | Prints the full generated task context that will be injected as `CODEX.md`. |
| `discover-usage-url` | Prints likely ChatGPT Codex usage API URLs found from the analytics page. |
| `done` | Moves a running task to `done/`; mainly for manual recovery. |
| `fail` | Moves a running task to `failed/` with an optional detail string. |
| `init` | Scaffolds `.agent/` context files in a project repo. |
| `logs` | Prints queue logs, optionally filtered by task or date. |
| `ls` | Lists tasks across queue folders. |
| `next` | Shows the next runnable task after priority and dependency resolution. |
| `remove` | Deletes a task from any queue folder. |
| `retry` | Moves a failed or unfinished task back to `pending/`. |
| `run` | Starts the usage-aware runner, or drains ready tasks once with `--once`. |
| `set-chatgpt-token` | Stores the ChatGPT session token used by the `codex_http` usage backend. |
| `stall` | Moves a running task to `unfinished/` with a stall reason. |
| `status` | Prints task counts by queue status. |

## Task Creation

```bash
./codex-queue add <project_dir> "<prompt>" [options]
```

Options:

| Option | Meaning |
|---|---|
| `-l, --level observer|craftsman|committer|deployer` | Capability profile. Default: `craftsman`. |
| `-p, --priority 1..5` | Lower number runs first. Default: `3`; `1` is critical and `5` is idle. |
| `--dry-run` | Ask Codex to write proposed changes under `.agent/dry-run/` instead of editing directly. |
| `--depends-on <ids>` | Comma-separated task IDs that must finish first. |
| `--tag <tags>` | Comma-separated labels for filtering/debugging. |
| `--max-minutes <n>` | Per-task timeout in minutes. |
| `--session-id <uuid>` | Resume an existing Codex session UUID when executing the task. |

## Runner

```bash
./codex-queue run
./codex-queue run --once
```

Default mode uses the usage-aware state machine. It checks usage at clock and
reset-anchored times, transitions from chilling to burning when enough budget
is close to reset, then runs one ready task at a time.

`--once` bypasses usage checks and drains ready tasks immediately. Use it for
manual execution and smoke testing.

## Queue Inspection

```bash
./codex-queue ls
./codex-queue ls --status pending
./codex-queue status
./codex-queue next
./codex-queue next --json-out
./codex-queue context <task_id>
./codex-queue logs
./codex-queue logs --task <task_id>
./codex-queue logs --date YYYY-MM-DD
```

`context` prints the full task `CODEX.md`, including agent identity, project
context, capabilities, and the queued task prompt.

## Task Lifecycle

```bash
./codex-queue retry <task_id>
./codex-queue remove <task_id>
./codex-queue remove <task_id> --force
./codex-queue begin <task_id>
./codex-queue done <task_id>
./codex-queue fail <task_id> --detail "reason"
./codex-queue stall <task_id> --reason checkpoint --detail "..."
```

`begin`, `done`, `fail`, and `stall` are low-level recovery/debug commands.
Normal task execution should go through `run` or the web dashboard.

Stall reasons are `timeout`, `checkpoint`, `dry_run_complete`, and `uncertain`.

## Usage Commands

```bash
./codex-queue set-chatgpt-token
printf '%s' "$TOKEN" | ./codex-queue set-chatgpt-token --stdin
./codex-queue check-usage
./codex-queue discover-usage-url
```

`set-chatgpt-token` stores the `__Secure-next-auth.session-token` cookie in a
private `0600` file. `check-usage` runs the active backend once. Use
`discover-usage-url` only when the default ChatGPT Codex usage endpoint changes.

## Project Context

```bash
./codex-queue init <project_dir>
./codex-queue compile <project_dir> --level craftsman
```

`init` scaffolds `.agent/` files that codex-queue injects into each task.
`compile` writes a daytime `CODEX.md` without creating a queued task.

## Codex CLI Path

Queued execution runs the Codex CLI. If the web server or systemd service does
not have `codex` on `PATH`, set an absolute path:

```bash
CODEX_QUEUE_CODEX_BIN="$(command -v codex)"
```

In `.env`, write the resolved path directly:

```bash
CODEX_QUEUE_CODEX_BIN=/home/you/.local/bin/codex
```

If Codex is already running inside a container, VM, or restricted remote
environment, its nested Linux sandbox may fail before shell commands run. The
queue can opt into Codex's no-sandbox mode:

```bash
CODEX_QUEUE_CODEX_BYPASS_SANDBOX=1
```

Only use this with an external isolation boundary such as a dedicated Linux
user, container, or VM.

## Adding New Commands

Commands live in `src/queue_worker/cli.py` as Click handlers registered on the
`main` group.

1. Add a focused `@main.command()` function in the matching section of
   `cli.py`. Use `@main.command('name-with-dashes')` when the public command
   name should differ from the Python function name.
2. Keep command handlers thin. Put reusable queue behavior in `queue_ops.py`,
   context behavior in `injector.py`, logging behavior in `logger.py`, and
   usage behavior in the usage modules.
3. Add Click arguments/options with clear help text and validate user input at
   the CLI boundary.
4. Update this file and any feature doc that owns the behavior.
5. Add or update tests under `tests/`. Prefer testing reusable helpers for
   business behavior and Click invocation only for CLI-specific output/errors.
6. Run:

```bash
python -m pyflakes src/queue_worker/
python -m pytest tests/
./codex-queue --help
./codex-queue <new-command> --help
```
