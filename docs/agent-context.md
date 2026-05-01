# Agent context (`.agent/`)

To make a project compatible with queue-up-for-claude, it needs a `.agent/` directory with files that tell the agent who it is, what the project is, and how to work. The runner injects this content into a generated `CLAUDE.md` before each task.

> **Worked example:** this repo eats its own dog food. The populated [`.agent/`](../.agent/) at the repo root is a real, non-template version of everything described below. Run `queue-worker compile .` from the repo root to see the `CLAUDE.md` it produces — the abstracts surface as one-liners, capabilities resolve into ALLOWED / NOT ALLOWED sections, and recent briefings get pulled in. Read those files alongside this doc.

## Scaffold

```bash
queue-worker init ~/projects/my-app
```

Creates:

```
my-app/
└── .agent/
    ├── AGENT.md              ← who the agent is (identity, role, goals)
    ├── CONTEXT.md            ← what the project is (tech stack, architecture)
    ├── BEHAVIOR.md           ← rules and constraints (always / never / style)
    ├── SETUP_GUIDE.md        ← onboarding doc
    ├── memory/
    │   ├── procedural.md     ← how to test, build, deploy
    │   ├── semantic.md       ← learned facts (grows over time)
    │   └── episodic.jsonl    ← session history (auto-populated by the runner)
    ├── proposed/             ← agent's proposed memory additions (review-and-merge)
    ├── checkpoints/          ← when the agent needs human input mid-task
    ├── dry-run/              ← proposed changes when running with --dry-run
    ├── briefings/            ← post-run summaries the agent writes
    └── .gitignore            ← ignores proposed/, checkpoints/, dry-run/
```

## YAML frontmatter

Every `.agent/*.md` file **must** have YAML frontmatter with an `abstract` field. The abstract is a 1–2 sentence summary that's injected into every generated `CLAUDE.md` so the agent can decide which files to read in full.

```markdown
---
abstract: "Senior backend engineer working on Acme SaaS. Specializes in
           TypeScript/Node, PostgreSQL, and Stripe integrations."
---

# Agent identity
...
```

The injector calls `extract_abstract()` on each file. Missing or malformed frontmatter falls back to the file path being referenced without a summary.

## Three core files

`init` writes templates with inline guidance — fill in the `[bracketed]` placeholders.

### `AGENT.md` — identity

Who the agent is. Role, specialty, goals for this project. Read at the start of every task.

### `CONTEXT.md` — project map

Tech stack, architecture, conventions. Enough that a new engineer could navigate the codebase after reading it.

### `BEHAVIOR.md` — rules

`Always` (e.g., "run tests before committing"), `Never` (e.g., "never modify migrations"), `Code style`. If a rule here conflicts with a task prompt, this file wins.

## Memory tiers

### Procedural (`memory/procedural.md`)

How to do things. Test commands, lint commands, build, deploy, dev server, migrations. Hand-curated.

### Semantic (`memory/semantic.md`)

Learned facts about the project. Hand-curated, but grows over time as the agent writes proposals to `proposed/` and you review-and-merge.

### Episodic (`memory/episodic.jsonl`)

Auto-populated. The executor appends a JSON line per task with id, status, stall reason, duration, tokens used. The injector summarizes recent entries into a "Recent sessions" snippet for the next CLAUDE.md.

## Proposed memory edits

Capability `write_agent_proposed` lets the agent write to `.agent/proposed/`. Files here represent suggested additions to `semantic.md` or `procedural.md` — never directly merged. You review and copy into the canonical files manually.

This keeps the agent's learning loop **human-gated**: it can propose ("I noticed migrations live in `prisma/migrations/`") but can't silently amend its own context.

## Checkpoints

Capability `write_checkpoint` lets the agent write `.agent/checkpoints/<id>.yaml` mid-task to escalate to a human:

```yaml
# .agent/checkpoints/2026-04-30T14-22.yaml
question: "Stripe API key has two candidates in .env. Which should I use?"
options:
  - STRIPE_SECRET_KEY (live)
  - STRIPE_SECRET_KEY_TEST
context: "I need it for the refund flow in src/billing/refund.ts"
```

If a new checkpoint file appears during task execution, the executor stalls the task with `stall_reason: checkpoint`, records the checkpoint path in the task YAML, and moves it to `unfinished/`. You answer by editing the task YAML's `checkpoint_answer` field and `queue-worker retry`. The injector adds the answer to the next CLAUDE.md.

## Briefings

Capability `write_briefing` lets the agent write `.agent/briefings/YYYYMMDD.md` — a short summary of decisions and changes from a session. The injector pulls recent briefings into the next CLAUDE.md as "Recent context" so the agent doesn't redo discovery work it already did.

This is the most useful piece for cross-session continuity. A 3-line briefing ("refactored auth into AuthService; changed JWT lib from jsonwebtoken to jose; added integration tests in tests/auth/") saves the next session minutes of grep.

## Dry-run output

Capability `write_dryrun` lets the agent write `.agent/dry-run/YYYYMMDD/` when a task has `dry_run: true`. The agent describes what it *would* do without applying changes. Useful for proposing a refactor for review before letting it run for real.

## Capability levels

Levels live in `config/profiles.yaml` and are resolved at task spawn time by `profiles.resolve_capabilities()`. The result is compiled into `ALLOWED` / `NOT ALLOWED` markdown sections of the injected CLAUDE.md.

| Level | Read | Write | Shell | Git stage/commit | Git push | Deploy |
|-------|------|-------|-------|------------------|----------|--------|
| `observer` | ✓ | — | read-only | — | — | — |
| `craftsman` | ✓ | ✓ | ✓ | — | — | — |
| `committer` | ✓ | ✓ | ✓ | ✓ | — | — |
| `deployer` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

All levels can write to `memory/`, `proposed/`, `checkpoints/`, and `briefings/`. `craftsman` and above can write to `dry-run/`.

### Per-task overrides

```yaml
# in a task YAML
caps_override:
  add: [git_push]        # grant git push to a craftsman for this one task
  remove: [delete_files] # revoke file deletion from a committer
```

The override is applied after the base level's caps are resolved.

### Capability flags

Defined in `config/profiles.yaml`. The full set:

`read_files`, `write_files`, `delete_files`, `run_shell`, `run_shell_readonly`, `run_deploy_scripts`, `git_read`, `git_stage_commit`, `git_push`, `net_packages`, `net_full`, `write_agent_memory`, `write_agent_proposed`, `write_briefing`, `write_checkpoint`, `write_dryrun`.

## How CLAUDE.md is built and injected

For each task, `injector.build_claude_md(task)`:

1. Loads `AGENT.md`, `CONTEXT.md`, `BEHAVIOR.md` and extracts each `abstract`.
2. Resolves capabilities (`profiles.resolve_capabilities`) and builds the `ALLOWED` / `NOT ALLOWED` section.
3. Pulls `extract_episodic_abstract()` — a digest of recent episodic entries.
4. Pulls `build_reference_section()` — recent briefings.
5. Renders the task prompt and any pending checkpoint answer.

Then `inject_claude_md(project_dir, content)`:

1. If the project already has a `CLAUDE.md`, copies it to a backup path.
2. Writes the generated content to `CLAUDE.md`.
3. Spawns `claude -p` against the project dir.
4. After the subprocess exits (or on crash recovery), `cleanup_claude_md` restores the backup so your interactive `CLAUDE.md` is never lost.

## Daytime use (no queued task)

```bash
queue-worker compile ~/projects/my-app --level craftsman
```

Generates a `CLAUDE.md` with the same identity / context / behavior / capabilities, minus a task prompt — useful for interactive Claude Code sessions where you want the same project conventions applied.

## Git hygiene

The scaffolded `.agent/.gitignore` excludes `proposed/`, `checkpoints/`, and `dry-run/` since those are session-specific scratch space. **Commit** `AGENT.md`, `CONTEXT.md`, `BEHAVIOR.md`, `memory/procedural.md`, `memory/semantic.md`. `episodic.jsonl` and `briefings/` are your call — committing them gives history a paper trail; ignoring them keeps the diff cleaner.
