# Agent Context

Projects use a `.agent/` directory to tell Codex who it is, what the project
is, how to work, and what it remembers. The runner compiles that into a
generated `CODEX.md` before each queued task.

## Scaffold

```bash
codex-queue init ~/projects/my-app
```

Required files:

| File | Purpose |
|---|---|
| `.agent/AGENT.md` | Agent identity and goals |
| `.agent/CONTEXT.md` | Product, stack, architecture, conventions |
| `.agent/BEHAVIOR.md` | Always/never rules |
| `.agent/memory/procedural.md` | Commands and repeatable procedures |
| `.agent/memory/semantic.md` | Durable project facts |
| `.agent/memory/episodic.jsonl` | Auto-appended task history |

Every `.agent/*.md` file should have YAML frontmatter with an `abstract` field.
The abstract is injected into every generated `CODEX.md` so Codex can decide
which context files to read in full.

## Capability Levels

Levels live in `config/profiles.yaml` and resolve to ALLOWED / NOT ALLOWED
sections in `CODEX.md`.

| Level | Intent |
|---|---|
| `observer` | Read-only inspection |
| `craftsman` | Edit files and run local commands |
| `committer` | Craftsman plus stage/commit permission |
| `deployer` | Committer plus push/deploy/network permissions |

Capability boundaries are prompt-level guidance, not OS sandboxing. The
executor runs Codex with `--full-auto`; use levels to communicate intended
behavior and use local OS permissions for hard boundaries.

## Output Conventions

Codex is instructed to write:

| Output | Path |
|---|---|
| Briefing | `.agent/briefings/YYYYMMDD.md` |
| Checkpoint | `.agent/checkpoints/YYYYMMDD-HHMMSS.yaml` |
| Proposed memory | `.agent/proposed/semantic-YYYYMMDD-HHMMSS.md` |
| Dry-run diffs | `.agent/dry-run/YYYYMMDD/` |

If a checkpoint appears during execution, the task moves to `unfinished/`.
Answer by editing `checkpoint_answer` in the task YAML, then run
`codex-queue retry <task_id>`.

## Build And Injection

For each task, `injector.build_codex_md(task)` assembles:

- metadata header
- codex-queue CLI instructions
- context file references and abstracts
- capability boundaries
- output conventions
- queued task prompt
- checkpoint resume answer, when present

Then `inject_codex_md(project_dir, content)` writes `CODEX.md`, backing up any
existing file to `CODEX.md.queue-worker-bak-{pid}`. Cleanup runs in a `finally`
block after the Codex subprocess exits.

For interactive daytime use:

```bash
codex-queue compile ~/projects/my-app --level craftsman
```
