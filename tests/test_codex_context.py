from pathlib import Path

from queue_worker.config import bootstrap
from queue_worker.injector import build_codex_md, cleanup_codex_md, inject_codex_md
from queue_worker.task import CapsOverride, Task, TaskBudget, now_iso

bootstrap()


def _task(tmp_path: Path) -> Task:
    project = tmp_path / 'project'
    agent = project / '.agent'
    (agent / 'memory').mkdir(parents=True)
    project.mkdir(exist_ok=True)
    return Task(
        id='task-1',
        created=now_iso(),
        dir=str(project),
        prompt='Ship it',
        level='craftsman',
        yaml_path='',
        resolved_dir=str(project),
        budget=TaskBudget(),
        caps_override=CapsOverride(),
    )


def test_build_codex_md_mentions_codex_context_file_and_cli(tmp_path):
    rendered = build_codex_md(_task(tmp_path))
    assert 'codex-queue context' in rendered
    assert 'CODEX.md' in rendered
    assert 'codex-queue <command>' in rendered
    assert '## Your task' in rendered
    assert 'Ship it' in rendered


def test_inject_codex_md_restores_original(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    codex_md = project / 'CODEX.md'
    codex_md.write_text('original', encoding='utf-8')

    backup = inject_codex_md(str(project), 'generated')
    assert codex_md.read_text(encoding='utf-8') == 'generated'

    cleanup_codex_md(str(project), backup)
    assert codex_md.read_text(encoding='utf-8') == 'original'
