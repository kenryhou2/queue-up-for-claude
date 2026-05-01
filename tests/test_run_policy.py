"""Tests for the run_policy / eligible_at scheduling feature."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from queue_worker import queue_ops
from queue_worker.queue_ops import (
    RUN_POLICY_NEXT_SESSION,
    RUN_POLICY_THIS_SESSION,
    RUN_POLICY_TONIGHT,
    SESSION_LENGTH_MINUTES,
    TONIGHT_HOUR_LOCAL,
    compute_eligible_at,
    create_task,
    resolve_run_order,
)
from queue_worker.task import Task, parse_task


class _StubLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg): self.lines.append(('info', msg))
    def warn(self, msg): self.lines.append(('warn', msg))
    def error(self, msg): self.lines.append(('error', msg))
    def task(self, *a, **k): self.lines.append(('task', a, k))


@pytest.fixture
def queue_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_ops, 'QUEUE_DIR', tmp_path)
    for s in queue_ops.QUEUE_STATUSES:
        (tmp_path / s).mkdir()
    return tmp_path


# ── compute_eligible_at ─────────────────────────────────────────────────────

class TestComputeEligibleAt:
    def test_none_policy_returns_none(self):
        assert compute_eligible_at(None, {}) is None

    def test_this_session_returns_none(self):
        assert compute_eligible_at(RUN_POLICY_THIS_SESSION, {}) is None

    def test_next_session_uses_runner_state(self):
        now = datetime(2026, 4, 19, 14, 0, 0, tzinfo=timezone.utc)
        last_check_at = now.timestamp() - 60  # checked one minute ago
        state = {'last_check_at': last_check_at, 'last_check_reset_minutes': 90}
        out = compute_eligible_at(RUN_POLICY_NEXT_SESSION, state, now=now)
        # session reset = check_time + 90 min; eligible_at adds the 60s buffer.
        expected = (datetime.fromtimestamp(last_check_at, tz=timezone.utc)
                    + timedelta(minutes=90, seconds=60))
        assert out == expected.isoformat(timespec='seconds')

    def test_next_session_falls_back_when_no_state(self):
        now = datetime(2026, 4, 19, 14, 0, 0, tzinfo=timezone.utc)
        out = compute_eligible_at(RUN_POLICY_NEXT_SESSION, {}, now=now)
        expected = now + timedelta(minutes=SESSION_LENGTH_MINUTES)
        assert out == expected.isoformat(timespec='seconds')

    def test_next_session_pushes_when_cached_state_already_expired(self):
        # Last check claimed reset 60 min away, but it was 6 hours ago — the
        # session has long since rolled over. We should NOT return a past time.
        now = datetime(2026, 4, 19, 14, 0, 0, tzinfo=timezone.utc)
        stale_check = now.timestamp() - 6 * 3600
        state = {'last_check_at': stale_check, 'last_check_reset_minutes': 60}
        out = compute_eligible_at(RUN_POLICY_NEXT_SESSION, state, now=now)
        expected = now + timedelta(minutes=SESSION_LENGTH_MINUTES)
        assert out == expected.isoformat(timespec='seconds')

    def test_next_session_ignores_stale_reset_after_errored_check(self):
        # runner.do_usage_check() updates last_check_at on error but leaves
        # the pct/reset fields at their previous successful values. Don't
        # trust them — fall back to now+5h.
        now = datetime(2026, 4, 19, 14, 0, 0, tzinfo=timezone.utc)
        state = {
            'last_check_at': now.timestamp() - 5,  # fresh timestamp
            'last_check_reset_minutes': 90,        # stale successful value
            'last_check_error': 'cloudflare blocked the API endpoint',
            'last_check_status': 'ERROR:cloudflare_blocked',
        }
        out = compute_eligible_at(RUN_POLICY_NEXT_SESSION, state, now=now)
        expected = now + timedelta(minutes=SESSION_LENGTH_MINUTES)
        assert out == expected.isoformat(timespec='seconds')

    def test_tonight_after_2am_picks_tomorrow(self):
        # Local 03:00 → eligible_at should be tomorrow 02:00 local.
        local_tz = datetime.now().astimezone().tzinfo
        now_local = datetime(2026, 4, 19, 3, 0, 0, tzinfo=local_tz)
        now_utc = now_local.astimezone(timezone.utc)
        out = compute_eligible_at(RUN_POLICY_TONIGHT, {}, now=now_utc)
        target_local = (now_local + timedelta(days=1)).replace(
            hour=TONIGHT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
        assert out == target_local.astimezone(timezone.utc).isoformat(timespec='seconds')

    def test_tonight_before_2am_picks_today(self):
        # Local 01:00 → eligible_at should be today 02:00 local.
        local_tz = datetime.now().astimezone().tzinfo
        now_local = datetime(2026, 4, 19, 1, 0, 0, tzinfo=local_tz)
        now_utc = now_local.astimezone(timezone.utc)
        out = compute_eligible_at(RUN_POLICY_TONIGHT, {}, now=now_utc)
        target_local = now_local.replace(
            hour=TONIGHT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
        assert out == target_local.astimezone(timezone.utc).isoformat(timespec='seconds')


# ── create_task persistence ──────────────────────────────────────────────────

class TestCreateTaskPersistsPolicy:
    def test_no_policy_yaml_omits_fields(self, queue_dir):
        tid, path = create_task(str(queue_dir), 'do a thing')
        data = yaml.safe_load(path.read_text())
        assert 'run_policy' not in data
        assert 'eligible_at' not in data

    def test_this_session_persists_policy_only(self, queue_dir):
        tid, path = create_task(str(queue_dir), 'do a thing',
                                run_policy=RUN_POLICY_THIS_SESSION)
        data = yaml.safe_load(path.read_text())
        assert data['run_policy'] == RUN_POLICY_THIS_SESSION
        assert 'eligible_at' not in data

    def test_tonight_persists_eligible_at(self, queue_dir):
        with patch('queue_worker.runner.get_runner_state', return_value={}):
            tid, path = create_task(str(queue_dir), 'do a thing',
                                    run_policy=RUN_POLICY_TONIGHT)
        data = yaml.safe_load(path.read_text())
        assert data['run_policy'] == RUN_POLICY_TONIGHT
        assert 'eligible_at' in data
        # Round-trips through parse_task.
        task = parse_task(str(path))
        assert task.run_policy == RUN_POLICY_TONIGHT
        assert task.eligible_at == data['eligible_at']

    def test_next_session_pulls_runner_state(self, queue_dir):
        last_check_at = time.time() - 30
        fake_state = {'last_check_at': last_check_at,
                      'last_check_reset_minutes': 45}
        with patch('queue_worker.runner.get_runner_state', return_value=fake_state):
            tid, path = create_task(str(queue_dir), 'do a thing',
                                    run_policy=RUN_POLICY_NEXT_SESSION)
        data = yaml.safe_load(path.read_text())
        assert data['run_policy'] == RUN_POLICY_NEXT_SESSION
        # Should be ~45 min in the future.
        eligible = datetime.fromisoformat(data['eligible_at'])
        delta = eligible - datetime.now(timezone.utc)
        assert timedelta(minutes=44) < delta < timedelta(minutes=47)

    def test_invalid_policy_rejected(self, queue_dir):
        with pytest.raises(ValueError, match='invalid run_policy'):
            create_task(str(queue_dir), 'do a thing', run_policy='whenever')

    def test_parse_normalizes_hand_edited_datetime(self, queue_dir):
        # Simulate a user hand-editing eligible_at unquoted: PyYAML loads it
        # as a Python datetime, which would crash lexicographic compare in
        # resolve_run_order. parse_task should coerce back to an ISO string.
        yaml_path = queue_dir / 'pending' / 'hand-edited.yaml'
        yaml_path.write_text(
            "id: hand-edited\n"
            "created: '2026-04-19T00:00:00+00:00'\n"
            "dir: /tmp\n"
            "prompt: x\n"
            "level: craftsman\n"
            "eligible_at: 2026-04-20T10:00:00+00:00\n"  # unquoted
        )
        task = parse_task(str(yaml_path))
        assert isinstance(task.eligible_at, str)
        assert task.eligible_at == '2026-04-20T10:00:00+00:00'


# ── resolve_run_order filtering ──────────────────────────────────────────────

def _mk_task(tid, *, eligible_at=None, run_policy=None,
             priority=3, depends_on=None):
    return Task(
        id=tid,
        created='2026-04-19T00:00:00+00:00',
        dir='/tmp',
        prompt='x',
        level='craftsman',
        yaml_path=f'/tmp/{tid}.yaml',
        resolved_dir='/tmp',
        priority=priority,
        depends_on=depends_on or [],
        run_policy=run_policy,
        eligible_at=eligible_at,
    )


class TestResolveRunOrderEligibility:
    def test_future_eligible_at_is_filtered(self, queue_dir):
        log = _StubLogger()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)
                  ).isoformat(timespec='seconds')
        gated = _mk_task('gated', eligible_at=future, run_policy='tonight')
        ready = _mk_task('ready')
        out = resolve_run_order([gated, ready], log)
        assert [t.id for t in out] == ['ready']
        assert any('gated until' in m for _, m in log.lines)

    def test_past_eligible_at_passes_through(self, queue_dir):
        log = _StubLogger()
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(timespec='seconds')
        t = _mk_task('past', eligible_at=past, run_policy='tonight')
        out = resolve_run_order([t], log)
        assert [x.id for x in out] == ['past']

    def test_legacy_task_with_no_eligible_at_runs(self, queue_dir):
        log = _StubLogger()
        t = _mk_task('legacy')
        out = resolve_run_order([t], log)
        assert [x.id for x in out] == ['legacy']
