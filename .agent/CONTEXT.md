---
abstract: "Python 3.11+ tool: queues codex exec --full-auto subprocesses,
           injects CODEX.md per task, and uses a local JSON command to decide
           when to burn queued work."
---

# Project context

`queue-up-for-codex` is a usage-aware Codex CLI job queue. The public CLI is
`codex-queue`; the web dashboard is `codex-queue-web`.

## Tech stack

- Python 3.11+
- Click CLI
- FastAPI dashboard with a single Alpine.js HTML file
- PyYAML task/context files
- pytest tests
- stdlib subprocess, sqlite3, urllib-free command provider

## Module map

```
src/queue_worker/
├── cli.py                  ← Click commands and .agent templates
├── web.py                  ← FastAPI dashboard and background runner
├── runner.py               ← chilling/burning state machine
├── usage_check.py          ← dispatcher and CSV writer
├── usage_check_command.py  ← CODEX_QUEUE_USAGE_COMMAND backend
├── executor.py             ← Codex subprocess lifecycle
├── injector.py             ← CODEX.md builder + backup/cleanup
├── sessions.py             ← Codex transcript lookup under ~/.codex
├── queue_ops.py            ← task lifecycle and scheduling policy
└── config.py               ← paths, private .env loader, subprocess env filter
```

## Important contracts

- `CODEX_QUEUE_USAGE_COMMAND` prints `{"used_pct": int, "reset_minutes": int}`.
- `executor.py` runs `codex exec --full-auto`; fresh tasks include `-C <dir>`.
- `CODEX.md` is generated and explicitly referenced in the prompt because it is
  not assumed to be auto-loaded.
- Task YAML stays forward-compatible through optional `raw.get(...)` parsing.
- `session_id` means a Codex UUID; thread-name resume is out of scope.
