import os
import json
import shlex
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import yaml

from .task import Task
from .profiles import resolve_capabilities, build_caps_section


# Optional global base directory for shared agent identity
BASE_AGENT_DIR = Path.home() / '.agent-base'

# codex-queue project root (derived same way as cli.py)
QW_ROOT = Path(__file__).resolve().parents[2]


# ── Abstract extraction ────────────────────────────────────────────────────────

def extract_abstract(file_path: Path) -> str:
    """
    Extract the abstract for a file. Priority order:
    1. YAML frontmatter `abstract:` field
    2. First non-empty, non-heading paragraph (trimmed to 220 chars)
    3. '(no abstract — add frontmatter to this file)'
    """
    try:
        content = file_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return '(file not found)'
    except OSError:
        return '(could not read file)'

    # 1. YAML frontmatter
    if content.startswith('---'):
        end = content.find('---', 3)
        if end > 0:
            try:
                fm = yaml.safe_load(content[3:end])
                if isinstance(fm, dict) and fm.get('abstract'):
                    return str(fm['abstract']).strip()
            except yaml.YAMLError:
                pass

    # 2. First non-empty, non-heading text line(s)
    lines = content.splitlines()
    text_lines = [l.strip() for l in lines
                  if l.strip() and not l.startswith('#') and l.strip() != '---']
    if text_lines:
        snippet = ' '.join(text_lines[:2])
        return snippet[:220] + ('...' if len(snippet) > 220 else '')

    return '(no abstract — add `abstract:` to frontmatter)'


def extract_episodic_abstract(episodic_path: Path) -> str:
    """
    Read last 5 lines of episodic.jsonl and format a short summary.
    """
    try:
        from collections import deque
        with open(episodic_path, encoding='utf-8') as f:
            recent = deque(f, maxlen=5)
        if not recent:
            return '(no sessions recorded yet)'
        entries = []
        for line in recent:
            try:
                e = json.loads(line)
                ts = e.get('ts', '')[:10]
                tid = e.get('task_id', '?')
                status = e.get('status', '?')
                entries.append(f"{ts}: {tid} → {status}")
            except json.JSONDecodeError:
                pass
        summary = "Recent: " + ' | '.join(reversed(entries))
        return summary[:300]
    except FileNotFoundError:
        return '(no sessions recorded yet)'
    except OSError:
        return '(could not read episodic log)'


# ── Reference file definitions ─────────────────────────────────────────────────

AGENT_REF_FILES = [
    # (relative_path_in_agent_dir, section_label)
    ('AGENT.md',              'Agent identity'),
    ('CONTEXT.md',            'Project context'),
    ('BEHAVIOR.md',           'Behavior rules'),
    ('memory/procedural.md',  'Working procedures'),
    ('memory/semantic.md',    'Learned project facts'),
]


def build_reference_section(agent_dir: Path) -> str:
    """
    Build the "## Context files" block.
    Each entry:
      ### <label>
      `<absolute path>`
      > <abstract>
    """
    lines = ['## Context files', '',
             'Read each file you need. Start with those relevant to your task.',
             'Use your file-reading tools — do not guess at file contents.', '']

    for rel, label in AGENT_REF_FILES:
        file_path = agent_dir / rel

        # Fall back to base agent dir if project file missing
        if not file_path.exists() and BASE_AGENT_DIR.exists():
            base_path = BASE_AGENT_DIR / rel
            if base_path.exists():
                file_path = base_path

        abstract = extract_abstract(file_path)
        lines += [
            f'### {label}',
            f'`{file_path}`',
            f'> {abstract}',
            '',
        ]

    # Episodic log (special handling)
    episodic = agent_dir / 'memory' / 'episodic.jsonl'
    abstract = extract_episodic_abstract(episodic)
    lines += [
        '### Recent session history',
        f'`{episodic}`',
        f'> {abstract}',
        '',
    ]

    return '\n'.join(lines)


# ── Output conventions section ─────────────────────────────────────────────────

def build_output_conventions(task: Task, agent_dir: Path) -> str:
    today = datetime.now().strftime('%Y%m%d')
    journal_path = agent_dir / 'briefings' / f'{today}-HH-MM-SS.md'
    checkpoint_path = agent_dir / 'checkpoints' / f'{today}-HHMMSS.yaml'
    proposed_path = agent_dir / 'proposed' / f'semantic-{today}-HHMMSS.md'
    dryrun_path = agent_dir / 'dry-run' / today
    queue_add_command = (
        f'cd {shlex.quote(str(QW_ROOT))} && ./codex-queue add '
        f'{shlex.quote(task.resolved_dir)} "<task prompt>" --level {task.level}'
    )

    lines = [
        '## Output conventions — always follow these',
        '',
        '**1. Work journal (mandatory)**:',
        f'   Write exactly one task journal at `{journal_path}` (use actual timestamp and create directories).',
        '   This MUST be your final filesystem write before exiting.',
        '   The filename pattern is `YYYYMMDD-HH-MM-SS.md`.',
        '   Do not write a date-only briefing file such as `YYYYMMDD.md`.',
        '   This does not apply to hello reset tasks.',
        '   Format:',
        '   ```',
        '   # Work Journal — YYYYMMDD-HH-MM-SS',
        '   task: <task_id>',
        '',
        '   ## Summary',
        '   <short summary of tasks performed>',
        '',
        '   ## Major Changes',
        '   <files, commands, or behavior changed in this task>',
        '',
        '   ## Security Risks Or Bugs',
        '   <potential security risks or bugs, or "None observed.">',
        '',
        '   ## Failures',
        '   <commands, tests, or checks that failed, or "None.">',
        '',
        '   ## Proposed Next Tasks',
        '   1. <improvement and why it should happen next>',
        '      ```bash',
        f'      {queue_add_command}',
        '      ```',
        '   2. <improvement and why it should happen next>',
        '      ```bash',
        f'      {queue_add_command}',
        '      ```',
        '   ```',
        '',
        '**2. Checkpoint (when you hit a decision boundary)**:',
        f'   Write `{checkpoint_path}` (use actual timestamp) then halt.',
        '   Format:',
        '   ```yaml',
        '   question: "What decision do you need?"',
        '   options: [option_a, option_b, option_c]',
        '   agent_recommendation: option_a',
        '   context_summary: "What you completed and what is pending."',
        '   ```',
        '',
        '**3. Learning (when you discover a durable fact)**:',
        f'   Write `{proposed_path}` (use actual timestamp).',
        '   The human reviews and merges. Never edit semantic.md directly',
        '   unless `write_agent_direct` is in your allowed capabilities.',
        '',
    ]

    if task.dry_run:
        lines += [
            '**4. DRY RUN MODE**: Do NOT apply any changes.',
            f'   Write all proposed changes as unified diffs to `{dryrun_path}/`.',
            '   Include a summary in the work journal. Then write the journal and halt.',
            '',
        ]

    return '\n'.join(lines)


# ── Main build function ────────────────────────────────────────────────────────

def build_codex_md(task: Task) -> str:
    """
    Assemble the full agent context.
    Uses reference links + abstracts — does NOT copy file content inline.
    Used by `context` (prints to conversation) and `compile` (writes CODEX.md).
    """
    agent_dir = Path(task.resolved_dir) / '.agent'
    caps = resolve_capabilities(task)

    sections: list[str] = []

    # Header
    sections.append(
        f'<!-- codex-queue context | task: {task.id} | '
        f'level: {task.level} | generated: {datetime.now().isoformat(timespec="seconds")} -->'
    )
    sections.append('')

    # Identity preamble
    sections.append('# Agent session')
    sections.append('')
    sections.append(
        'You are running autonomously via codex-queue. '
        'This generated context is written to CODEX.md. '
        'Your project context is in the files listed below. '
        'Read each file you need using your file-reading tools. '
        'Never guess at file contents — read them.'
    )
    sections.append('')

    # codex-queue CLI instructions
    qw_path = QW_ROOT / 'codex-queue'
    venv_activate = QW_ROOT / '.venv' / 'bin' / 'activate'
    sections.append('## codex-queue CLI')
    sections.append('')
    sections.append(f'The codex-queue CLI is available at `{qw_path}`.')
    sections.append('To use it in shell commands:')
    sections.append('')
    sections.append('```bash')
    sections.append(f'source {venv_activate} && codex-queue <command>')
    sections.append('```')
    sections.append('')
    sections.append('Useful commands: `codex-queue ls`, `codex-queue status`, '
                    '`codex-queue next`, `codex-queue logs`, '
                    '`codex-queue add <dir> "<prompt>"`, '
                    '`codex-queue init <dir>` (scaffold .agent/ in a new project).')
    sections.append(f'Full documentation: `{QW_ROOT / "README.md"}`')
    sections.append('')
    sections.append('---')
    sections.append('')

    # Reference files section
    sections.append(build_reference_section(agent_dir))

    sections.append('---')
    sections.append('')

    # Capabilities
    sections.append(f'## Automation level: {task.level}')
    sections.append('')
    sections.append(build_caps_section(caps))
    sections.append('')
    sections.append('---')
    sections.append('')

    # Output conventions
    sections.append(build_output_conventions(task, agent_dir))

    sections.append('---')
    sections.append('')

    # Task (skip for daytime interactive sessions)
    if not task.id.startswith('[daytime-'):
        sections.append('## Your task')
        sections.append('')
        sections.append(task.prompt.strip())
        sections.append('')

    # Checkpoint resume
    if task.checkpoint_answer:
        sections.append('---')
        sections.append('')
        sections.append('## Resuming from checkpoint')
        sections.append('')
        sections.append(
            'A previous session paused and asked for human input. The answer is:'
        )
        sections.append('')
        sections.append(f'**{task.checkpoint_answer}**')
        if task.resume_context:
            sections.append('')
            sections.append(task.resume_context.strip())
        sections.append('')
        sections.append(
            'Read the most recent entry in `episodic.jsonl` and the checkpoint file '
            'in `.agent/checkpoints/` to understand exactly where the previous session '
            'stopped. Continue from there.'
        )
        sections.append('')

    return '\n'.join(sections)


# ── Inject / cleanup ───────────────────────────────────────────────────────────

@dataclass
class BackupInfo:
    had_original: bool
    backup_path: Optional[Path] = None


def inject_codex_md(project_dir: str, content: str) -> BackupInfo:
    """Write CODEX.md to project_dir, backing up any existing one (PID-stamped)."""
    codex_path = Path(project_dir) / 'CODEX.md'
    backup_path = Path(project_dir) / f'CODEX.md.queue-worker-bak-{os.getpid()}'
    had_original = codex_path.exists()

    if had_original:
        codex_path.rename(backup_path)

    codex_path.write_text(content, encoding='utf-8')
    return BackupInfo(had_original=had_original,
                      backup_path=backup_path if had_original else None)


def cleanup_codex_md(project_dir: str, backup: BackupInfo) -> None:
    """Delete injected CODEX.md and restore backup. Must run in a finally block."""
    codex_path = Path(project_dir) / 'CODEX.md'
    codex_path.unlink(missing_ok=True)
    if backup.had_original and backup.backup_path and backup.backup_path.exists():
        backup.backup_path.rename(codex_path)


def render_task_context(task_id: str) -> Optional[str]:
    """Render the CODEX.md a task would receive at execution time. Returns
    None if the task isn't found in any queue bucket."""
    from .queue_ops import find_task_yaml
    from .task import parse_task
    path = find_task_yaml(task_id)
    if not path:
        return None
    return build_codex_md(parse_task(str(path)))
