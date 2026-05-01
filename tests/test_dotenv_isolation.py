"""Regression tests for the .env loader's subprocess-isolation guarantee.

The runner spawns `claude -p` subprocesses with env=os.environ.copy(), so
anything that lands in os.environ becomes readable by every queued task.
Secrets read from .env (CLAUDE_SESSION_KEY, QUEUE_WORKER_PASSWORD, etc.)
MUST NOT propagate that way. These tests pin the contract.
"""

from __future__ import annotations

import os
from importlib import reload

import pytest

from queue_worker import config


def test_dotenv_does_not_pollute_os_environ(tmp_path, monkeypatch):
    """The whole point of the P1 fix: .env values stay in config._DOTENV
    and never reach os.environ, so claude -p subprocess env can't see them."""
    env_file = tmp_path / '.env'
    env_file.write_text(
        'CLAUDE_SESSION_KEY=sk-ant-test-from-dotenv-FAKE-1234567890\n'
        'CLAUDE_ORG_UUID=dotenv-uuid\n'
        'QUEUE_WORKER_PASSWORD=hunter2\n'
    )
    env_file.chmod(0o600)
    monkeypatch.delenv('CLAUDE_SESSION_KEY', raising=False)
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)
    monkeypatch.delenv('QUEUE_WORKER_PASSWORD', raising=False)

    config._DOTENV.clear()
    config._load_dotenv(env_file)

    # Loaded into the private store
    assert config._DOTENV['CLAUDE_SESSION_KEY'].startswith('sk-ant-')
    assert config._DOTENV['CLAUDE_ORG_UUID'] == 'dotenv-uuid'
    assert config._DOTENV['QUEUE_WORKER_PASSWORD'] == 'hunter2'

    # NOT in os.environ — this is the actual security guarantee
    assert 'CLAUDE_SESSION_KEY' not in os.environ
    assert 'CLAUDE_ORG_UUID' not in os.environ
    assert 'QUEUE_WORKER_PASSWORD' not in os.environ


def test_get_env_prefers_process_env_over_dotenv(monkeypatch):
    """Explicit shell exports / CI / systemd Environment= must override .env."""
    monkeypatch.setenv('CLAUDE_SESSION_KEY', 'from-env')
    config._DOTENV['CLAUDE_SESSION_KEY'] = 'from-dotenv'
    try:
        assert config.get_env('CLAUDE_SESSION_KEY') == 'from-env'
    finally:
        config._DOTENV.pop('CLAUDE_SESSION_KEY', None)


def test_get_env_falls_back_to_dotenv(monkeypatch):
    monkeypatch.delenv('CLAUDE_SESSION_KEY', raising=False)
    config._DOTENV['CLAUDE_SESSION_KEY'] = 'from-dotenv'
    try:
        assert config.get_env('CLAUDE_SESSION_KEY') == 'from-dotenv'
    finally:
        config._DOTENV.pop('CLAUDE_SESSION_KEY', None)


def test_load_dotenv_refuses_loose_perms(tmp_path, monkeypatch, capsys):
    """0644 .env containing secrets must be refused, not warned and loaded."""
    env_file = tmp_path / '.env'
    env_file.write_text('CLAUDE_SESSION_KEY=sk-ant-leak-FAKE-1234567890\n')
    env_file.chmod(0o644)
    config._DOTENV.clear()
    config._load_dotenv(env_file)
    err = capsys.readouterr().err
    assert 'too permissive' in err
    assert 'CLAUDE_SESSION_KEY' not in config._DOTENV


def test_load_dotenv_refuses_symlink(tmp_path, monkeypatch, capsys):
    real = tmp_path / 'real.env'
    real.write_text('CLAUDE_SESSION_KEY=sk-ant-leak-FAKE-1234567890\n')
    real.chmod(0o600)
    link = tmp_path / '.env'
    link.symlink_to(real)
    config._DOTENV.clear()
    config._load_dotenv(link)
    err = capsys.readouterr().err
    assert 'symlink' in err
    assert 'CLAUDE_SESSION_KEY' not in config._DOTENV


def test_load_dotenv_skips_comments_and_quotes(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text(
        '# top-level comment\n'
        '\n'
        'PLAIN=value1\n'
        'DOUBLE_QUOTED="value 2 with spaces"\n'
        "SINGLE_QUOTED='value 3'\n"
        'NO_EQUALS_SIGN_SKIPPED\n'
    )
    env_file.chmod(0o600)
    config._DOTENV.clear()
    config._load_dotenv(env_file)
    assert config._DOTENV['PLAIN'] == 'value1'
    assert config._DOTENV['DOUBLE_QUOTED'] == 'value 2 with spaces'
    assert config._DOTENV['SINGLE_QUOTED'] == 'value 3'
    assert 'NO_EQUALS_SIGN_SKIPPED' not in config._DOTENV
