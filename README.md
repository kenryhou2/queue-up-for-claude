# queue-up-for-codex

A usage-aware job queue for Codex CLI.

You queue tasks against your projects. The runner checks a local usage command,
or the built-in ChatGPT Codex usage backend, waits until unused budget is near
reset, then runs one `codex exec --full-auto` subprocess per task. Each task
gets a fresh Codex session with project-specific identity, capability
boundaries, and rolling memory injected via `CODEX.md`.

> **Unofficial.** Not affiliated with, endorsed by, or supported by OpenAI. If
> you use the `codex_http` usage backend, codex-queue reads Codex cloud usage
> from ChatGPT with your `__Secure-next-auth.session-token` cookie. If that
> endpoint or cookie flow changes, the built-in backend may stop working; the
> local command backend remains available.

> ⚠️ **Full-disk file access.** This tool can read any file on your computer that your user account can read. Two reasons:
> 1. The dashboard's file-browser API (`/api/files/list|read|raw`) is **not sandboxed** — it accepts any absolute path and resolves it via the OS. Anyone who can reach the dashboard can browse your whole home directory. Keep it on `127.0.0.1` / Tailscale and put a password on it before exposing it.
> 2. Each task runs `codex exec --full-auto`, so the Codex subprocess can
>    read/write anywhere your user can unless your environment adds its own
>    isolation.
>
> **Nothing is uploaded to the author and there is no telemetry.** The session
> token, usage history, task prompts, and logs stay on your machine. Outbound
> traffic goes to ChatGPT only for the built-in usage backend and to OpenAI
> through the Codex CLI while running queued tasks. See [Data storage](#data-storage).

---

## Why

Token windows refill whether or not you used the previous bucket. Pure
on-demand usage leaves capacity idle. queue-up-for-codex lets you stage work
asynchronously and burn it when unused budget is close to reset.

The runner is also a structured way to give a Codex subprocess a stable identity
per project: who it is, what the project is, what it is allowed to do, and what
it remembers from prior sessions.

---

## How it works

Two states: **chilling** (default) and **burning** (active).

```
              +-------------+  hourly + reset-anchored checks
              |  CHILLING   |  (HH:00 + T-60 / T-10 / T+5)
              +------+------+ 
                     |  remaining >= 30% AND reset < 70min
              +------v------+
              |   BURNING   |  one task at a time until reset
              +------+------+
                     |
                     v
       codex exec --full-auto per task,
       fresh CODEX.md injected per task,
       outcome recorded, queue advances.
```

For each task the runner:

1. Resolves dependencies + priority to pick the next ready task.
2. Builds a `CODEX.md` from the project's `.agent/` files and injects it into the project dir.
3. Spawns `codex exec --full-auto -C <project> <prompt>`.
4. Watches for checkpoint files, dry-run output, timeouts, and exit codes.
5. Restores the original `CODEX.md`, moves the task YAML, and updates episodic memory.

---

## Quick start

Requires **Python 3.11+** and **Codex CLI** installed and authenticated.

```bash
git clone <your-fork-url> queue-up-for-codex
cd queue-up-for-codex
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env && chmod 0600 .env

# Option A: built-in ChatGPT Codex usage backend
./codex-queue set-chatgpt-token
printf '\nCODEX_QUEUE_USAGE_BACKEND=codex_http\n' >> .env

# Option B: local command usage backend
# set CODEX_QUEUE_USAGE_COMMAND=...; see docs/usage-checking.md

./codex-queue check-usage

./codex-queue init ~/projects/my-app
./codex-queue add ~/projects/my-app "Add input validation to /signup" --level committer

codex-queue-web
# open http://localhost:51002
```

For the built-in backend, paste the `__Secure-next-auth.session-token` cookie
from `https://chatgpt.com` when `set-chatgpt-token` prompts. For the local
command backend, your command must print:

```json
{"used_pct": 71, "reset_minutes": 58}
```

---

## Features

| Area | What | Read more |
|---|---|---|
| Usage-aware runner | Chilling/burning state machine, reset-anchored scheduling, command or ChatGPT Codex HTTP usage provider | [runner-state-machine.md](docs/runner-state-machine.md), [usage-checking.md](docs/usage-checking.md) |
| Per-project agent identity | `.agent/` directory with AGENT/CONTEXT/BEHAVIOR + rolling memory, checkpoints, briefings, proposed memory edits | [agent-context.md](docs/agent-context.md) |
| Capability boundaries | Four levels compiled into ALLOWED / NOT ALLOWED sections of injected `CODEX.md`; per-task overrides | [agent-context.md](docs/agent-context.md#capability-levels) |
| Web dashboard | FastAPI + Alpine.js SPA at `localhost:51002` | [web-dashboard.md](docs/web-dashboard.md) |
| Auth + remote access | Optional password gate with brute-force protection | [auth-and-remote-access.md](docs/auth-and-remote-access.md) |
| CLI | Full guide for `codex-queue` commands and adding new commands | [cli.md](docs/cli.md) |
| Crash recovery | Per-task lock files, dead-PID detection, `CODEX.md` backup restoration, durable reset anchor | [runner-state-machine.md](docs/runner-state-machine.md#crash-recovery) |

---

## Command guide

Use `./codex-queue <command>` from the repo root, or `codex-queue <command>`
after installing the package.
Note: `./codex-queue` automatically runs the commands from the venv directory.

| Command | Usage | What it does |
|---|---|---|
| `add` | `./codex-queue add <project_dir> "<prompt>" [options]` | Creates a pending task for a project prompt. |
| `begin` | `./codex-queue begin <task_id>` | Moves a pending task to `running/` for manual recovery. |
| `check-usage` | `./codex-queue check-usage` | Runs the configured usage backend once. |
| `compile` | `./codex-queue compile <project_dir> --level craftsman` | Writes a project `CODEX.md` for interactive Codex use. |
| `context` | `./codex-queue context <task_id>` | Prints the full generated task context. |
| `discover-usage-url` | `./codex-queue discover-usage-url` | Finds likely ChatGPT Codex usage API URLs. |
| `done` | `./codex-queue done <task_id>` | Moves a running task to `done/`. |
| `fail` | `./codex-queue fail <task_id> --detail "reason"` | Moves a running task to `failed/`. |
| `init` | `./codex-queue init <project_dir>` | Scaffolds `.agent/` context files in a project. |
| `logs` | `./codex-queue logs [--task <id>] [--date YYYY-MM-DD]` | Prints queue logs. |
| `ls` | `./codex-queue ls [--status pending]` | Lists tasks across queue folders. |
| `next` | `./codex-queue next [--json-out]` | Shows the next runnable task. |
| `remove` | `./codex-queue remove <task_id> [--force]` | Deletes a task from the queue. |
| `retry` | `./codex-queue retry <task_id>` | Moves a failed or unfinished task back to `pending/`. |
| `run` | `./codex-queue run [--once]` | Starts the runner or drains ready tasks once. |
| `set-chatgpt-token` | `./codex-queue set-chatgpt-token [--stdin]` | Stores the ChatGPT session token for usage checks. |
| `stall` | `./codex-queue stall <task_id> --reason checkpoint --detail "..."` | Moves a running task to `unfinished/`. |
| `status` | `./codex-queue status` | Prints task counts by status. |

See [docs/cli.md](docs/cli.md) for command options and the contributor guide
for adding new commands.

---

## Repo layout

```
queue-up-for-codex/
├── .agent/                 ← this repo's own codex-queue context
├── codex-queue             ← bash wrapper for the CLI
├── pyproject.toml          ← package metadata + entry points
├── config/profiles.yaml    ← capability level definitions
├── docs/                   ← per-feature documentation
├── src/queue_worker/       ← Python code
│   ├── cli.py              ← Click commands + .agent/ templates
│   ├── web.py              ← FastAPI dashboard + background runner thread
│   ├── runner.py           ← state machine + scheduling
│   ├── usage_check.py      ← dispatcher + CSV write
│   ├── usage_check_command.py ← local JSON command backend
│   ├── executor.py         ← Codex subprocess lifecycle
│   ├── injector.py         ← CODEX.md builder + inject/cleanup
│   ├── sessions.py         ← Codex transcript locator
│   └── static/             ← dashboard SPA
└── tests/                  ← pytest unit tests
```

---

## Data storage

- Usage history is appended to `usage_history.csv` in the project directory.
- Task prompts and per-task Codex output live in `queue/` and `logs/`.
- `.env` is loaded into a private in-process store and is not exported into Codex subprocesses.
- The ChatGPT session token for the `codex_http` backend lives in `.env` or
  `~/.config/queue-worker/chatgpt_session_token` with mode `0600`.
- Strings sent to the dashboard pass through a redactor for likely API keys and email addresses.

See [docs/security.md](docs/security.md) for the full security model and known
limitations.

---

## License

MIT — see [LICENSE](LICENSE).
