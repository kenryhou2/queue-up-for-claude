# CLI reference

Entry point: `queue-worker` (installed by `pip install -e .`). The bash wrapper at the repo root (`./queue-worker`) executes the same thing through `.venv/bin/python` so you don't need to activate the venv.

## Runner

### `queue-worker run`

Start the runner. Default: chilling mode with clock-aligned hourly checks plus reset-anchored T-60 / T-10 / T+5 checks. Transitions to burning when usage ≥ 30% remaining AND reset < 70 min.

```bash
queue-worker run                       # state machine (default)
queue-worker run --once                # drain queue immediately, bypass state machine
```

See [runner-state-machine.md](runner-state-machine.md) for full behavior.

> Don't run this alongside `queue-worker-web` against the same queue.

## Queue management

### `queue-worker add`

Queue a task.

```bash
queue-worker add <project_dir> "<prompt>" [options]

# Examples
queue-worker add ~/projects/my-app "Add unit tests for the auth module" --level craftsman
queue-worker add ~/projects/api "Deploy to staging" --level deployer -p 1
queue-worker add ~/projects/app "Refactor utils" --level committer --dry-run
queue-worker add ~/projects/app "Run after migration" --depends-on "app-20260409-a3f1"
queue-worker add ~/projects/app "Quick fix" --max-minutes 30 -p 2
```

| Option | Default | Description |
|--------|---------|-------------|
| `-l, --level` | `craftsman` | `observer`, `craftsman`, `committer`, or `deployer` |
| `-p, --priority` | `3` | 1=critical, 2=high, 3=normal, 4=low, 5=idle |
| `--dry-run` | off | Propose changes without applying them |
| `--depends-on` | none | Comma-separated task IDs that must complete first |
| `--tag` | none | Comma-separated tags |
| `--max-minutes` | 120 | Timeout in minutes (minimum 1) |

### `queue-worker ls`

```bash
queue-worker ls                     # all tasks
queue-worker ls --status pending    # filter by status
```

### `queue-worker status`

Show queue counts.

```
  pending      3
  running      0
  done         12
  unfinished   1
  failed       0
```

(Live runner state — chilling/burning, last check, burn deadline — is exposed by `queue-worker-web` at `/api/status`.)

### `queue-worker retry`

Move a failed or unfinished task back to pending.

```bash
queue-worker retry <task_id>
```

### `queue-worker remove`

```bash
queue-worker remove <task_id>           # prompts for confirmation
queue-worker remove <task_id> --force   # no confirmation
```

## Inspection

### `queue-worker next`

Print the next task to run (highest priority, dependencies met).

```bash
queue-worker next              # human-readable
queue-worker next --json-out   # JSON output
```

### `queue-worker context <task_id>`

Print the full agent context for a task — capability boundaries, `.agent/` file references, output conventions, and the task prompt. Same content the runner injects as `CLAUDE.md`.

### `queue-worker logs`

```bash
queue-worker logs                          # today's log
queue-worker logs --date 2026-04-08        # specific date
queue-worker logs --task my-app-20260408   # filter by task ID
```

## Manual lifecycle

Move tasks through the lifecycle without the runner.

```bash
queue-worker begin <task_id>                                     # pending → running
queue-worker done <task_id>                                      # running → done
queue-worker fail <task_id> --detail "reason"                    # running → failed
queue-worker stall <task_id> --reason checkpoint --detail "..."  # running → unfinished
```

## Project setup

### `queue-worker init <project_dir>`

Scaffold a `.agent/` directory in a project repo. See [agent-context.md](agent-context.md).

### `queue-worker compile <project_dir>`

Generate a `CLAUDE.md` for daytime interactive use (no queued task).

```bash
queue-worker compile ~/projects/my-app --level craftsman
```

## Task YAML reference

```yaml
id: my-app-20260409-a3f1
created: "2026-04-09T14:22:00+00:00"
dir: /Users/me/projects/my-app
prompt: |
  Refactor the auth module. Extract JWT decode/verify into a dedicated
  AuthService class. Update all call sites. Add unit tests.
level: committer
priority: 2
depends_on: []
dry_run: false
tags: [refactor, auth]
budget:
  max_minutes: 90
caps_override:
  add: []
  remove: []
```

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique task ID (auto-generated) |
| `dir` | yes | Project directory (stored as absolute path) |
| `prompt` | yes | Full task description for the agent |
| `level` | yes | Automation level |
| `created` | auto | ISO 8601 timestamp |
| `priority` | no | 1=critical, 2=high, 3=normal (default), 4=low, 5=idle |
| `depends_on` | no | List of task IDs that must be in `done/` first |
| `dry_run` | no | If true, no changes applied |
| `tags` | no | Organizational tags |
| `budget.max_minutes` | no | Timeout (default: 120) |
| `caps_override.add` | no | Extra capabilities |
| `caps_override.remove` | no | Capabilities to revoke |
| `run_policy` | no | `this_session` / `next_session` / `tonight` (controls `eligible_at`) |
| `session_id` | no | Resume an existing Claude Code session by id |

## Priority and dependency resolution

Tasks are ordered by:

1. **Dependency graph first** — a task won't run until all its `depends_on` IDs are in `done/`.
2. **Priority second** — lower number runs first (1 before 5).
3. **Creation time third** — ties broken by oldest first.

## Task lifecycle

```
pending/ ──→ running/ ──→ done/         (success)
                      ──→ unfinished/   (checkpoint, dry-run, timeout)
                      ──→ failed/       (non-zero exit, crash)

unfinished/ or failed/ ──→ pending/     (via `queue-worker retry`)
```
