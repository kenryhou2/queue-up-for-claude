# CLI

Entry point: `codex-queue` after `pip install -e .`. The root wrapper
`./codex-queue` runs the same CLI through the repo virtualenv.

## `codex-queue run`

```bash
codex-queue run
codex-queue run --once
```

Default mode uses the usage-aware state machine. `--once` drains ready tasks
immediately and bypasses usage checks.

## `codex-queue add`

```bash
codex-queue add <project_dir> "<prompt>" [options]
```

Common options:

| Option | Meaning |
|---|---|
| `--level observer|craftsman|committer|deployer` | Capability profile |
| `--priority 1..5` | Lower number runs first |
| `--dry-run` | Ask Codex to write proposed diffs under `.agent/dry-run/` |
| `--depends-on <ids>` | Comma-separated dependencies |
| `--max-minutes <n>` | Task timeout |
| `--session-id <uuid>` | Resume an existing Codex session UUID |

## Queue inspection

```bash
codex-queue ls
codex-queue status
codex-queue next
codex-queue context <task_id>
codex-queue logs --task <task_id>
```

`context` prints the `CODEX.md` that will be injected for the task.

## Task lifecycle

```bash
codex-queue retry <task_id>
codex-queue remove <task_id>
codex-queue begin <task_id>
codex-queue done <task_id>
codex-queue fail <task_id> --detail "reason"
codex-queue stall <task_id> --reason checkpoint --detail "..."
```

`begin`, `done`, `fail`, and `stall` are low-level recovery/debug commands.

## Project context

```bash
codex-queue init <project_dir>
codex-queue compile <project_dir> --level craftsman
```

`init` scaffolds `.agent/`. `compile` writes a daytime `CODEX.md` without a
queued task prompt.
