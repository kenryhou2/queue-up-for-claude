from pathlib import Path

from queue_worker.config import bootstrap
from queue_worker import executor
from queue_worker.task import CapsOverride, Task, TaskBudget, now_iso

bootstrap()


class _Log:
    def __init__(self):
        self.lines = []

    def task(self, task_id, msg):
        self.lines.append((task_id, msg))


def _task(tmp_path: Path, *, session_id: str | None = None) -> Task:
    project = tmp_path / 'project'
    project.mkdir()
    agent = project / '.agent'
    (agent / 'memory').mkdir(parents=True)
    (agent / 'briefings').mkdir()
    (agent / 'checkpoints').mkdir()
    (agent / 'dry-run').mkdir()
    (agent / 'proposed').mkdir()
    yaml_path = tmp_path / 'task.yaml'
    yaml_path.write_text('id: task-1\n')
    return Task(
        id='task-1',
        created=now_iso(),
        dir=str(project),
        prompt='Do the thing',
        level='craftsman',
        yaml_path=str(yaml_path),
        resolved_dir=str(project),
        budget=TaskBudget(max_minutes=1),
        caps_override=CapsOverride(),
        session_id=session_id,
    )


def _patch_locks(monkeypatch, tmp_path):
    lock_path = tmp_path / 'task.lock'
    monkeypatch.setattr(executor, 'acquire_task_lock',
                        lambda _task_id, _project_dir: lock_path)
    monkeypatch.setattr(executor, 'update_task_lock',
                        lambda _lock_path, _fields: None)
    monkeypatch.setattr(executor, 'release_task_lock',
                        lambda _lock_path: None)
    monkeypatch.setattr(executor, '_codex_bin', lambda: 'codex')
    monkeypatch.setattr(executor, '_codex_exec_policy_args',
                        lambda: ['--full-auto'])


def test_fresh_task_uses_codex_exec_full_auto(monkeypatch, tmp_path):
    _patch_locks(monkeypatch, tmp_path)
    captured = {}

    def fake_run(cmd, cwd, timeout_seconds, log_fn, lock_path=None):
        captured['cmd'] = cmd
        captured['cwd'] = cwd
        return 0, False, 123, None

    monkeypatch.setattr(executor, '_run_codex', fake_run)
    task = _task(tmp_path)
    result = executor.execute_task(task, _Log())

    assert result.status == 'done'
    assert captured['cmd'][:6] == [
        'codex', 'exec', '--full-auto', '--skip-git-repo-check',
        '-C', task.resolved_dir,
    ]
    assert '--- BEGIN CODEX.md ---' in captured['cmd'][-1]
    assert 'Do the thing' in captured['cmd'][-1]
    assert captured['cwd'] == task.resolved_dir
    assert not (Path(task.resolved_dir) / 'CODEX.md').exists()


def test_resume_task_uses_codex_exec_resume(monkeypatch, tmp_path):
    _patch_locks(monkeypatch, tmp_path)
    captured = {}

    def fake_run(cmd, cwd, timeout_seconds, log_fn, lock_path=None):
        captured['cmd'] = cmd
        captured['cwd'] = cwd
        return 0, False, None, None

    sid = '019dea2c-2da1-73b3-bda5-ea155c32d6a4'
    monkeypatch.setattr(executor, '_run_codex', fake_run)
    task = _task(tmp_path, session_id=sid)
    result = executor.execute_task(task, _Log())

    assert result.status == 'done'
    assert captured['cmd'][:5] == [
        'codex', 'exec', 'resume', '--full-auto', '--skip-git-repo-check',
    ]
    assert captured['cmd'][5] == sid
    assert '--- BEGIN CODEX.md ---' in captured['cmd'][6]
    assert 'Do the thing' in captured['cmd'][6]
    assert captured['cwd'] == task.resolved_dir


def test_codex_md_original_is_restored_on_failure(monkeypatch, tmp_path):
    _patch_locks(monkeypatch, tmp_path)

    def fake_run(cmd, cwd, timeout_seconds, log_fn, lock_path=None):
        return 2, False, None, None

    monkeypatch.setattr(executor, '_run_codex', fake_run)
    task = _task(tmp_path)
    codex_md = Path(task.resolved_dir) / 'CODEX.md'
    codex_md.write_text('original', encoding='utf-8')

    result = executor.execute_task(task, _Log())

    assert result.status == 'failed'
    assert codex_md.read_text(encoding='utf-8') == 'original'


def test_codex_md_original_is_restored_on_timeout(monkeypatch, tmp_path):
    _patch_locks(monkeypatch, tmp_path)

    def fake_run(cmd, cwd, timeout_seconds, log_fn, lock_path=None):
        return -15, True, None, None

    monkeypatch.setattr(executor, '_run_codex', fake_run)
    task = _task(tmp_path)
    codex_md = Path(task.resolved_dir) / 'CODEX.md'
    codex_md.write_text('original', encoding='utf-8')

    result = executor.execute_task(task, _Log())

    assert result.status == 'unfinished'
    assert result.stall_reason == 'timeout'
    assert codex_md.read_text(encoding='utf-8') == 'original'


def test_codex_environment_blocker_fails_even_with_zero_exit(monkeypatch, tmp_path):
    _patch_locks(monkeypatch, tmp_path)

    def fake_run(cmd, cwd, timeout_seconds, log_fn, lock_path=None):
        return 0, False, 4105, 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'

    monkeypatch.setattr(executor, '_run_codex', fake_run)
    task = _task(tmp_path)

    result = executor.execute_task(task, _Log())

    assert result.status == 'failed'
    assert 'environment blocker' in result.stall_detail
