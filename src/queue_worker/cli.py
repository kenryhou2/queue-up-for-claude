import sys
import json
from pathlib import Path

import click

from .config import LOG_DIR, bootstrap as _bootstrap, get_logger as _log


@click.group()
def main():
    """codex-queue — autonomous Codex job queue."""
    _bootstrap()


# ── run ───────────────────────────────────────────────────────────────────────

@main.command()
@click.option('--once', is_flag=True, help='Drain the queue once and exit (bypasses usage check).')
def run(once):
    """Start the runner.

    Default mode: chilling with clock-aligned usage checks at every HH:00
    plus reset-anchored T-60 / T-10 / T+5 checks and a hello reset ping.
    When usage shows
    >=30% remaining AND <70 minutes to reset, transitions to burning and
    processes tasks until the reset time. During burning, a usage check
    runs after each task finishes.
    Use --once to drain the queue immediately, bypassing the state machine.
    """
    from .runner import start_runner
    start_runner(_log(), run_once=once)


# ── next ──────────────────────────────────────────────────────────────────────

@main.command('next')
@click.option('--json-out', 'as_json', is_flag=True, help='Output as JSON.')
def next_task(as_json):
    """Print the next task to run (highest priority, dependencies met)."""
    from .queue_ops import get_pending_tasks, resolve_run_order
    tasks = get_pending_tasks(_log())
    ordered = resolve_run_order(tasks, _log())
    if not ordered:
        click.echo('No tasks ready.') if not as_json else click.echo('null')
        sys.exit(1)
    task = ordered[0]
    if as_json:
        click.echo(json.dumps({
            'id': task.id, 'dir': task.dir, 'level': task.level,
            'priority': task.priority, 'prompt': task.prompt,
            'depends_on': task.depends_on, 'dry_run': task.dry_run,
            'tags': task.tags, 'budget': {'max_minutes': task.budget.max_minutes},
        }, indent=2))
    else:
        click.echo(f'ID:       {task.id}\nDir:      {task.dir}\nLevel:    {task.level}')
        click.echo(f'Priority: {task.priority}\nDry-run:  {task.dry_run}')
        click.echo(f'Prompt:   {task.prompt.strip()[:200]}')


# ── add ───────────────────────────────────────────────────────────────────────

@main.command()
@click.argument('project_dir')
@click.argument('prompt')
@click.option('-l', '--level', default='craftsman', show_default=True,
              type=click.Choice(['observer', 'craftsman', 'committer', 'deployer']))
@click.option('-p', '--priority', default=3, show_default=True,
              type=click.IntRange(1, 5), help='1=critical, 2=high, 3=normal, 4=low, 5=idle')
@click.option('--dry-run', is_flag=True)
@click.option('--depends-on', default='', help='Comma-separated task IDs.')
@click.option('--tag', default='', help='Comma-separated tags.')
@click.option('--max-minutes', type=click.IntRange(min=1), default=None, help='Timeout in minutes.')
@click.option('--session-id', default=None, help='Codex session UUID to resume when executing.')
def add(project_dir, prompt, level, priority, dry_run, depends_on, tag, max_minutes, session_id):
    """Add a task to the queue."""
    from .queue_ops import create_task
    deps = [x.strip() for x in depends_on.split(',') if x.strip()]
    tags = [x.strip() for x in tag.split(',') if x.strip()]
    try:
        task_id, out_path = create_task(project_dir, prompt, level, priority,
                                        dry_run, deps, tags, max_minutes,
                                        session_id=session_id)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f'Added task {task_id}')
    click.echo(f'  file:  {out_path}')
    click.echo(f'  level: {level}  |  priority: {priority}  |  dir: {project_dir}')


# ── begin / done / fail / stall ──────────────────────────────────────────────

@main.command()
@click.argument('task_id')
def begin(task_id):
    """Move a task from pending to running."""
    from .queue_ops import begin_task
    try:
        begin_task(task_id, _log())
        click.echo(f'Started {task_id}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command('done')
@click.argument('task_id')
def done_cmd(task_id):
    """Move a task from running to done."""
    from .queue_ops import complete_task
    try:
        complete_task(task_id, _log())
        click.echo(f'Completed {task_id}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command()
@click.argument('task_id')
@click.option('--detail', default='', help='Failure detail.')
def fail(task_id, detail):
    """Move a task from running to failed."""
    from .queue_ops import fail_task
    try:
        fail_task(task_id, detail, _log())
        click.echo(f'Failed {task_id}: {detail}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command()
@click.argument('task_id')
@click.option('--reason', required=True,
              type=click.Choice(['timeout', 'checkpoint', 'dry_run_complete', 'uncertain']))
@click.option('--detail', default='', help='Stall detail.')
def stall(task_id, reason, detail):
    """Move a task from running to unfinished."""
    from .queue_ops import stall_task
    try:
        stall_task(task_id, reason, detail, _log())
        click.echo(f'Stalled {task_id}: [{reason}] {detail}')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


# ── retry / remove ───────────────────────────────────────────────────────────

@main.command()
@click.argument('task_id')
def retry(task_id):
    """Move a task from unfinished/ or failed/ back to pending/."""
    from .queue_ops import retry_task
    try:
        has_checkpoint = retry_task(task_id, _log())
        click.echo(f'Moved {task_id} -> pending/')
        if has_checkpoint:
            click.echo('  (checkpoint_answer preserved)')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command()
@click.argument('task_id')
@click.option('--force', is_flag=True, help='Skip confirmation prompt.')
def remove(task_id, force):
    """Remove a task from any queue."""
    from .queue_ops import remove_task
    if not force:
        click.confirm(f'Remove task {task_id}?', abort=True)
    try:
        queue_name = remove_task(task_id, _log())
        click.echo(f'Removed {task_id} (was in {queue_name}/)')
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


# ── ls / status / context / logs ─────────────────────────────────────────────

@main.command('ls')
@click.option('--status', default=None, type=click.Choice(
    ['pending', 'running', 'done', 'unfinished', 'failed']))
def ls(status):
    """List tasks across all queues."""
    from .queue_ops import list_tasks
    STATUS_COLOR = {'pending': 'blue', 'running': 'yellow', 'done': 'green',
                    'unfinished': 'magenta', 'failed': 'red'}
    rows = list_tasks(status)
    if not rows:
        click.echo('Queue is empty.')
        return
    click.echo(f"{'ID':<36}  {'STATUS':<12}  {'LEVEL':<12}  {'PRI':<5}  {'DIR':<25}  NOTE")
    click.echo('-' * 100)
    for r in rows:
        s = r.get('_status', '?')
        color = STATUS_COLOR.get(s, 'white')
        d = str(r.get('dir', '?'))
        click.echo(f"{r.get('id','?'):<36}  {click.style(f'{s:<12}', fg=color)}  "
                   f"{r.get('level','?'):<12}  {str(r.get('priority','?')):<5}  "
                   f"{d[-23:] if len(d)>23 else d:<25}  {r.get('stall_reason','')}")


@main.command()
def status():
    """Show queue summary."""
    from .queue_ops import get_queue_counts
    for s, count in get_queue_counts().items():
        click.echo(f'  {s:<12} {count}')


@main.command()
@click.argument('task_id')
def context(task_id):
    """Print the full agent context for a task."""
    from .injector import render_task_context
    rendered = render_task_context(task_id)
    if rendered is None:
        raise click.ClickException(f'Task {task_id} not found')
    click.echo(rendered)


@main.command()
@click.option('--task', 'task_id', default=None, help='Filter by task ID.')
@click.option('--date', default=None, help='YYYY-MM-DD (default: today).')
def logs(task_id, date):
    """Show log output for today (or a specified date)."""
    from .logger import read_log_lines
    date_str, lines = read_log_lines(LOG_DIR, date=date, task_id=task_id)
    if not lines:
        click.echo(f'No log for {date_str}')
        return
    for line in lines:
        click.echo(line)


# ── usage auth / checking ────────────────────────────────────────────────────

@main.command('set-chatgpt-token')
@click.option('--stdin', 'from_stdin', is_flag=True,
              help='Read the token from stdin instead of prompting.')
def set_chatgpt_token(from_stdin):
    """Store the ChatGPT session token for the codex_http usage backend."""
    from .usage_check_codex_http import SESSION_TOKEN_FILE

    if from_stdin:
        token = sys.stdin.read().strip()
    else:
        token = click.prompt(
            'Paste __Secure-next-auth.session-token',
            hide_input=True,
            confirmation_prompt=False,
        ).strip()

    if len(token) < 20:
        raise click.ClickException('token is too short')

    SESSION_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_TOKEN_FILE.parent.chmod(0o700)
    SESSION_TOKEN_FILE.write_text(token, encoding='utf-8')
    SESSION_TOKEN_FILE.chmod(0o600)
    click.echo(f'Stored ChatGPT session token at {SESSION_TOKEN_FILE}')


@main.command('check-usage')
def check_usage():
    """Fetch usage once and append usage_history.csv."""
    from .usage_check import check_usage_once

    result = check_usage_once(lambda msg: click.echo(msg))
    if result.pct is not None:
        click.echo(f'usage: {result.pct}%')
    if result.reset_str:
        click.echo(f'resets in: {result.reset_str}')
    click.echo(f'status: {result.status}')
    click.echo(f'backend: {result.backend}')
    if result.error:
        click.echo(f'error: {result.error}')
    if result.error_code:
        raise click.ClickException(result.error_code)


@main.command('discover-usage-url')
def discover_usage_url():
    """Print likely ChatGPT Codex usage API URLs from the analytics page."""
    from .usage_check_codex_http import discover_usage_urls

    urls = discover_usage_urls(lambda msg: click.echo(msg))
    if not urls:
        raise click.ClickException('no candidate usage URLs found')
    for url in urls:
        click.echo(url)


# ── compile / init ───────────────────────────────────────────────────────────

@main.command()
@click.argument('project_dir')
@click.option('-l', '--level', default='craftsman', show_default=True,
              type=click.Choice(['observer', 'craftsman', 'committer', 'deployer']))
def compile(project_dir, level):
    """Generate CODEX.md for a project for daytime interactive use."""
    from .task import Task, expand_path, now_iso, CapsOverride, TaskBudget
    from .injector import build_codex_md
    abs_dir = expand_path(project_dir)
    dummy = Task(id='[daytime-session]', created=now_iso(), dir=abs_dir,
                 prompt='[Daytime interactive session]', level=level,
                 yaml_path='', resolved_dir=abs_dir,
                 budget=TaskBudget(), caps_override=CapsOverride())
    out = Path(abs_dir) / 'CODEX.md'
    out.write_text(build_codex_md(dummy), encoding='utf-8')
    click.echo(f'CODEX.md written to {out}')


@main.command('init')
@click.argument('project_dir')
def init(project_dir):
    """Scaffold a .agent/ directory in a project repo."""
    from .task import expand_path
    agent_dir = Path(expand_path(project_dir)) / '.agent'
    memory_dir = agent_dir / 'memory'
    for d in [agent_dir, memory_dir, agent_dir/'proposed',
              agent_dir/'checkpoints', agent_dir/'dry-run', agent_dir/'briefings']:
        d.mkdir(parents=True, exist_ok=True)
    for path, tmpl in _TEMPLATES.items():
        full = (agent_dir if '/' not in path else memory_dir) / path.split('/')[-1]
        if full.exists():
            click.echo(f'  exists (skipping): {full.name}')
        else:
            full.write_text(tmpl, encoding='utf-8')
            click.echo(f'  created: {full.name}')
    episodic = memory_dir / 'episodic.jsonl'
    if not episodic.exists():
        episodic.touch()
        click.echo('  created: episodic.jsonl')
    gi = agent_dir / '.gitignore'
    if not gi.exists():
        gi.write_text('proposed/*\ncheckpoints/*\ndry-run/*\n', encoding='utf-8')
        click.echo('  created: .gitignore')
    # Write setup guide
    guide = agent_dir / 'SETUP_GUIDE.md'
    if not guide.exists():
        guide.write_text(_SETUP_GUIDE, encoding='utf-8')
        click.echo('  created: SETUP_GUIDE.md')

    click.echo(f'\n.agent/ scaffolded in {agent_dir}')
    click.echo('\nNext steps:')
    click.echo('  1. Read .agent/SETUP_GUIDE.md for detailed instructions')
    click.echo('  2. Fill in each .md file (start with AGENT.md and CONTEXT.md)')
    click.echo('  3. Replace every "REPLACE THIS" abstract with a real summary')
    click.echo('  4. git add .agent/ && git commit -m "chore: add .agent/ context"')
    click.echo(f'  5. codex-queue add {agent_dir.parent} "your first task" --level craftsman')


_TEMPLATES = {
    'AGENT.md': '''\
---
abstract: "REPLACE THIS: 1-2 sentences describing who the agent is and what
           project it works on. This abstract is shown to the agent so it can
           decide whether to read the full file. Be specific."
---

<!-- HOW TO FILL THIS IN:
     The abstract above is the most important part. It appears in every
     generated CODEX.md. Write it as if briefing a new teammate in 2 sentences.

     Below, describe the agent's role, specialty, and goals for this project.
     Be concrete: "senior backend engineer" is better than "developer".
     The agent reads this at the start of every task to understand its identity.
-->

# Agent identity

You are a [ROLE, e.g. "senior backend engineer", "full-stack developer"]
working on [PROJECT NAME, e.g. "Acme SaaS invoice platform"].

## Specialty
<!-- What does this agent know best about this project? e.g.
     "TypeScript/React frontend, REST API design, PostgreSQL optimization" -->

## Goals
<!-- What should the agent optimize for when working on tasks? -->
- [e.g. "Ship clean, tested code that follows existing patterns"]
- [e.g. "Never break the CI pipeline"]
- [e.g. "Prefer small, focused changes over large refactors"]
''',

    'CONTEXT.md': '''\
---
abstract: "REPLACE THIS: 1-2 sentences describing the project, its tech stack,
           and key architectural choices. This helps the agent understand what
           it's working with before reading the full file."
---

<!-- HOW TO FILL THIS IN:
     This is the agent's map of the codebase. Include enough detail that a
     new engineer could navigate the project after reading this file.

     Key things to cover:
     - What the project IS (product, purpose, scale)
     - Tech stack (language, framework, database, key libraries)
     - Architecture (monorepo? microservices? module boundaries?)
     - Important conventions (file naming, directory structure)
     - External dependencies (APIs, services, infrastructure)
-->

# Project context

## What this is
<!-- e.g. "B2B SaaS for invoice management. ~50k LOC, 3 engineers." -->

## Tech stack
- Language / framework: <!-- e.g. "TypeScript, React 18, Express.js" -->
- Database: <!-- e.g. "PostgreSQL 15 with Prisma ORM" -->
- Key libraries: <!-- e.g. "TailwindCSS, Zod, React Query" -->
- CI/CD: <!-- e.g. "GitHub Actions, deploys to Vercel" -->

## Architecture
<!-- Describe the high-level structure:
     - Directory layout (e.g. "monorepo: packages/web, packages/api, packages/shared")
     - Key module boundaries (e.g. "all API routes in src/routes/, all DB queries in src/db/")
     - Data flow (e.g. "React -> API -> Prisma -> PostgreSQL")
     - Important patterns (e.g. "repository pattern for DB access, middleware for auth")
-->

## Important conventions
<!-- Things every agent MUST know to avoid breaking things:
     - e.g. "All API responses use { data, error } envelope"
     - e.g. "Database migrations are in prisma/migrations/, never edit existing ones"
     - e.g. "Environment variables are in .env.local, never commit .env"
-->
''',

    'BEHAVIOR.md': '''\
---
abstract: "REPLACE THIS: 1-2 sentences summarizing the key rules. e.g.
           'Always run tests before committing. Never modify migrations.
           Use conventional commits.'"
---

<!-- HOW TO FILL THIS IN:
     This file defines hard rules the agent must follow. Think of it as
     "things that would make you reject a PR". Be specific and concrete.

     The agent checks this before every action. If a rule here conflicts
     with a task prompt, this file wins.
-->

# Behavior rules

## Always
<!-- Things the agent must do on every task: -->
- [e.g. "Run the test suite before committing any changes"]
- [e.g. "Use conventional commit messages (feat:, fix:, refactor:)"]
- [e.g. "Add tests for new functions"]
- [e.g. "Keep changes focused -- one concern per commit"]

## Never
<!-- Hard prohibitions: -->
- [e.g. "Never modify existing database migration files"]
- [e.g. "Never push directly to main"]
- [e.g. "Never delete .env.example or .gitignore"]
- [e.g. "Never install new dependencies without noting in the briefing"]

## Code style
<!-- Formatting, naming, patterns the agent should follow: -->
<!-- e.g. "Use single quotes for strings"
     e.g. "Name files in kebab-case"
     e.g. "Prefer async/await over .then() chains"
     e.g. "Use TypeScript strict mode -- no `any` types" -->
''',

    'memory/procedural.md': '''\
---
abstract: "REPLACE THIS: list the key commands. e.g. 'Test: npm test.
           Lint: npm run lint. Build: npm run build. Dev: npm run dev (port 3000).'"
---

<!-- HOW TO FILL THIS IN:
     List every command the agent might need to run. Be exact -- include
     flags, environment variables, and prerequisites.

     The agent reads this to know HOW to do things (test, build, lint, deploy).
     If a command has gotchas (e.g. "must run from repo root"), note them.
-->

# Working procedures

## Run tests
```
[e.g. npm test]
```
<!-- Notes: e.g. "Requires test DB running: docker compose up -d postgres-test" -->

## Lint / format
```
[e.g. npm run lint]
[e.g. npm run lint:fix]
```

## Build
```
[e.g. npm run build]
```

## Start dev server
```
[e.g. npm run dev]
```
<!-- Notes: e.g. "Runs on port 3000, requires .env.local" -->

## Deploy
```
[e.g. npm run deploy:staging]
```
<!-- Notes: e.g. "Requires VPN connection" -->

## Database migrations
```
[e.g. npx prisma migrate dev]
```

## Other useful commands
<!-- Add any project-specific commands:
     e.g. "Generate types: npm run codegen"
     e.g. "Seed database: npm run db:seed"
     e.g. "Run single test: npm test -- --grep 'test name'" -->
''',

    'memory/semantic.md': '''\
---
abstract: "Learned facts about this project. Updated by the agent over time
           as it discovers things. Human reviews and merges proposals."
---

# Semantic memory

<!-- This file is populated over time as the agent works on tasks.
     The agent writes proposed additions to .agent/proposed/ -- you review
     and merge useful facts here.

     Examples of what goes here:
     - "The auth middleware at src/middleware/auth.ts caches JWT verification
        for 5 minutes -- don't add a second cache layer"
     - "Tests in packages/api use a shared test DB that resets between runs"
     - "The deploy script requires AWS_PROFILE=staging to be set"

     You can also add facts manually if you know something the agent should
     learn without having to discover it the hard way. -->
''',
}

_SETUP_GUIDE = '''\
# .agent/ Setup Guide

This directory contains context files that codex-queue uses to brief the
AI agent before each task. Fill in each file to get better results.

**Delete this guide once you're done setting up.**

---

## Quick checklist

- [ ] Fill in `AGENT.md` -- who the agent is for this project
- [ ] Fill in `CONTEXT.md` -- what the project is, tech stack, architecture
- [ ] Fill in `BEHAVIOR.md` -- rules the agent must follow
- [ ] Fill in `memory/procedural.md` -- commands to test, build, deploy
- [ ] Replace every `"REPLACE THIS"` abstract with a real 1-2 sentence summary
- [ ] Commit: `git add .agent/ && git commit -m "chore: add .agent/ context"`

---

## How it works

Each `.md` file has YAML frontmatter with an `abstract:` field:

```markdown
---
abstract: "This 1-2 sentence summary is shown to the agent so it can decide
           whether to read the full file."
---

# Full content below...
```

The abstract is critical -- it appears in every task context. Write it as if
briefing a colleague in a Slack message: short, specific, informative.

**Bad abstract:** "Information about the project"
**Good abstract:** "React 18 + Express monorepo. PostgreSQL with Prisma ORM.
                    All API routes in packages/api/src/routes/."

---

## File-by-file guide

### AGENT.md -- Who is the agent?

Define the agent's role and goals. This shapes how it approaches tasks.

**What to write:**
- Role: "senior backend engineer", "full-stack developer", etc.
- Project name: the specific project this .agent/ directory is in
- Specialty: what the agent knows best about THIS project
- Goals: what to optimize for (test coverage? shipping speed? code quality?)

**Example:**
```markdown
---
abstract: "Senior TypeScript engineer working on Acme invoice platform.
           Specializes in API design and PostgreSQL query optimization."
---

# Agent identity

You are a senior TypeScript engineer working on Acme.

## Specialty
REST API design, Prisma ORM, PostgreSQL performance.

## Goals
- Ship tested, type-safe code
- Keep API response times under 200ms
- Follow existing patterns in the codebase
```

### CONTEXT.md -- What is the project?

This is the agent's map. Include enough detail to navigate without asking.

**What to write:**
- What the product IS (1-2 sentences)
- Full tech stack (language, framework, database, key libraries)
- Architecture (directory layout, module boundaries, data flow)
- Conventions (file naming, patterns, things that would surprise a newcomer)

**Example:**
```markdown
---
abstract: "B2B invoice SaaS. React 18 + Vite frontend, Express API,
           PostgreSQL 15 with Prisma. Monorepo with packages/web and packages/api."
---

# Project context

## What this is
Invoice management platform for small businesses. ~50k LOC, 3 engineers.

## Tech stack
- Frontend: React 18, Vite, TailwindCSS, React Query
- API: Express.js, TypeScript strict mode
- Database: PostgreSQL 15, Prisma ORM
- Auth: JWT with refresh tokens
- CI: GitHub Actions, deploys to Vercel (frontend) and Fly.io (API)

## Architecture
Monorepo: packages/web, packages/api, packages/shared (types).
All API routes: packages/api/src/routes/<resource>.ts
All DB queries: packages/api/src/db/<resource>.ts (repository pattern)
Shared types: packages/shared/src/types/<resource>.ts

## Conventions
- API responses always use { data, error } envelope
- Database migrations in prisma/migrations/ -- never edit existing ones
- Environment: .env.local for dev, .env.production for prod (never committed)
- Feature flags in src/config/features.ts
```

### BEHAVIOR.md -- What are the rules?

Hard rules the agent must follow. Think "things that would make you reject a PR."

**What to write:**
- "Always" rules: things to do on every task
- "Never" rules: hard prohibitions
- Code style: formatting, naming, patterns

**Example:**
```markdown
---
abstract: "Always run tests before committing. Use conventional commits.
           Never modify migrations. No `any` types in TypeScript."
---

# Behavior rules

## Always
- Run `npm test` before committing
- Use conventional commits (feat:, fix:, refactor:, chore:)
- Add tests for new public functions
- Update types in packages/shared when changing API contracts

## Never
- Modify existing migration files (create new ones instead)
- Push directly to main
- Use `any` type in TypeScript
- Delete .env.example or .gitignore
```

### memory/procedural.md -- How to test, build, deploy?

Every command the agent might need. Be exact -- include flags and prerequisites.

**What to write:**
- Test command (and how to run a single test)
- Lint/format command
- Build command
- Dev server command (and what port)
- Deploy command (and any prerequisites)
- Database migration command
- Any project-specific commands

### memory/semantic.md -- What has the agent learned?

Starts empty. Over time, the agent proposes additions to `.agent/proposed/`.
You review and merge useful facts here.

You can also pre-populate it with things you know the agent should learn:
- Tricky bugs and their root causes
- Performance-sensitive code paths
- External API quirks
- "Don't touch this because..." explanations

---

## After filling in the files

1. Commit the .agent/ directory to your repo
2. Add a task: `codex-queue add /path/to/this/project "your task" --level craftsman`
3. The agent will read these files at the start of every task

## Tips

- **Be specific.** "Use TypeScript" is less useful than "TypeScript strict mode,
  no `any`, prefer `unknown` for external data."
- **Update over time.** As the project evolves, update these files.
  The agent proposes updates to semantic.md automatically.
- **Start minimal.** You don't need to fill in everything on day one.
  AGENT.md and CONTEXT.md are the most important. The rest can be added later.
'''


if __name__ == '__main__':
    main()
