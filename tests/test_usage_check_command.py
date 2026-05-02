"""Tests for the command usage backend."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from queue_worker import usage_check_command as ucc
from queue_worker.usage_check import UsageCheckResult, check_usage_once, decide


def _completed(stdout: str, returncode: int = 0, stderr: str = ''):
    return subprocess.CompletedProcess(
        args=['usage-helper'],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture(autouse=True)
def clear_env(monkeypatch, tmp_path):
    from queue_worker import usage_check
    monkeypatch.delenv('CODEX_QUEUE_USAGE_COMMAND', raising=False)
    monkeypatch.delenv('CODEX_QUEUE_USAGE_TIMEOUT_SECONDS', raising=False)
    monkeypatch.setattr(usage_check, 'USAGE_CSV', tmp_path / 'usage_history.csv')


def test_redact_strips_api_key_and_email():
    out = ucc.redact('failed for sk-proj-AbCdEf123 and foo@example.com')
    assert out == 'failed for sk-*** and ***@***'


def test_parse_usage_happy_path():
    pct, mins, reset = ucc._parse_usage('{"used_pct": 71, "reset_minutes": 58}')
    assert pct == 71
    assert mins == 58
    assert reset == '58min'


@pytest.mark.parametrize('raw,match', [
    ('not-json', 'valid JSON'),
    ('[]', 'JSON object'),
    ('{"used_pct": true, "reset_minutes": 5}', 'used_pct not numeric'),
    ('{"used_pct": 1.5, "reset_minutes": 5}', 'whole number'),
    ('{"used_pct": -1, "reset_minutes": 5}', 'between 0 and 100'),
    ('{"used_pct": 101, "reset_minutes": 5}', 'between 0 and 100'),
    ('{"used_pct": 50, "reset_minutes": -1}', 'greater than or equal to 0'),
])
def test_parse_usage_rejects_bad_output(raw, match):
    with pytest.raises(ucc.BadResponse, match=match):
        ucc._parse_usage(raw)


def test_missing_command_env_maps_to_error():
    result = check_usage_once()
    assert result.error_code == 'usage_command_missing'
    assert result.status == 'ERROR:usage_command_missing'


def test_command_success(monkeypatch):
    monkeypatch.setenv('CODEX_QUEUE_USAGE_COMMAND', 'usage-helper --json')
    run = MagicMock(return_value=_completed('{"used_pct": 71, "reset_minutes": 58}'))
    monkeypatch.setattr(ucc.subprocess, 'run', run)
    result = ucc.fetch_usage_command(lambda _m: None)
    assert isinstance(result, UsageCheckResult)
    assert result.pct == 71
    assert result.reset_minutes == 58
    assert result.reset_str == '58min'
    assert result.status == 'Chilling'
    assert result.backend == 'command'
    assert run.call_args.args[0] == ['usage-helper', '--json']


def test_command_nonzero_maps_to_error(monkeypatch):
    monkeypatch.setenv('CODEX_QUEUE_USAGE_COMMAND', 'usage-helper')
    monkeypatch.setattr(
        ucc.subprocess, 'run',
        MagicMock(return_value=_completed('', returncode=2, stderr='bad sk-proj-123')),
    )
    result = check_usage_once()
    assert result.error_code == 'usage_command_failed'
    assert 'sk-proj-123' not in result.error


def test_command_timeout_maps_to_error(monkeypatch):
    monkeypatch.setenv('CODEX_QUEUE_USAGE_COMMAND', 'usage-helper')
    monkeypatch.setattr(
        ucc.subprocess, 'run',
        MagicMock(side_effect=subprocess.TimeoutExpired(['usage-helper'], 30)),
    )
    result = check_usage_once()
    assert result.error_code == 'usage_command_timeout'


def test_command_bad_json_maps_to_error(monkeypatch):
    monkeypatch.setenv('CODEX_QUEUE_USAGE_COMMAND', 'usage-helper')
    monkeypatch.setattr(
        ucc.subprocess, 'run',
        MagicMock(return_value=_completed('not-json')),
    )
    result = check_usage_once()
    assert result.error_code == 'bad_response'


def test_decide_burn_threshold():
    assert decide(69, 60) == 'NEED TO BURN TOKEN !'
    assert decide(71, 60) == 'Chilling'
    assert decide(69, 70) == 'Chilling'
