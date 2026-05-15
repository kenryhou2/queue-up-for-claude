import threading

from queue_worker.config import bootstrap
from queue_worker import executor, runner
from queue_worker.queue_ops import (
    DEFAULT_USAGE_WINDOW_MINUTES,
    NEXT_SESSION_BUFFER_SECONDS,
)

bootstrap()


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(('info', msg))

    def error(self, msg):
        self.lines.append(('error', msg))


def test_reset_ping_spawns_codex_hello(monkeypatch):
    captured = {}
    monkeypatch.setattr(executor, '_codex_bin', lambda: 'codex')
    monkeypatch.setattr(executor, '_codex_exec_policy_args',
                        lambda: ['--full-auto'])

    def fake_run(cmd, cwd, timeout_seconds, log_fn, lock_path=None):
        captured['cmd'] = cmd
        captured['cwd'] = cwd
        captured['timeout_seconds'] = timeout_seconds
        captured['lock_path'] = lock_path
        return 0, False, None, None

    monkeypatch.setattr(executor, '_run_codex', fake_run)

    result = executor.execute_reset_ping(_Log())

    assert result.status == 'done'
    assert captured['cmd'] == [
        'codex', 'exec', '--full-auto', '--skip-git-repo-check',
        '-C', str(executor.PROJECT_ROOT), executor.RESET_PING_PROMPT,
    ]
    assert captured['cwd'] == str(executor.PROJECT_ROOT)
    assert captured['timeout_seconds'] == executor.RESET_PING_TIMEOUT_SECONDS
    assert captured['lock_path'] is None


def test_reset_ping_is_armed_after_predicted_reset(monkeypatch):
    now = 1_000_000.0
    predicted_reset = now + 2 * 60 * 60
    monkeypatch.setattr(runner.time, 'time', lambda: now)

    with runner._state_lock:
        runner._scheduled_checks.clear()
        runner._arm_scheduled_locked(predicted_reset)
        scheduled = list(runner._scheduled_checks)

    assert ('reset_ping' in [kind for _due, kind in scheduled])
    ping_due = [due for due, kind in scheduled if kind == 'reset_ping'][0]
    assert ping_due == predicted_reset + NEXT_SESSION_BUFFER_SECONDS


def test_post_reset_reanchors_next_five_hour_window():
    now = 1_000_000.0
    current_reset = now - 5 * 60
    proposed = current_reset + DEFAULT_USAGE_WINDOW_MINUTES * 60
    snap = runner.RunnerState(
        next_reset_at=current_reset,
        post_reset_done_for_cycle=False,
    )

    action, kind = runner._decide_anchor_action(snap, 'post_reset', proposed, now)

    assert action == 'reanchor'
    assert kind == 'post_reset'


def test_reset_ping_requeues_when_codex_is_busy(monkeypatch):
    called = False

    def fake_execute(_log):
        nonlocal called
        called = True

    monkeypatch.setattr(runner, 'execute_reset_ping', fake_execute)
    execute_lock = threading.Lock()
    execute_lock.acquire()
    try:
        ran = runner._run_reset_ping(_Log(), execute_lock)
    finally:
        execute_lock.release()

    assert ran is False
    assert called is False
