"""Regression tests for the .env loader's subprocess-isolation guarantee."""

from __future__ import annotations

import os

from queue_worker import config


def test_dotenv_does_not_pollute_os_environ(tmp_path, monkeypatch):
    """The whole point of the P1 fix: .env values stay in config._DOTENV
    and never reach os.environ, so Codex subprocess env can't see them."""
    env_file = tmp_path / '.env'
    env_file.write_text(
        'CODEX_QUEUE_USAGE_COMMAND=usage-helper --json\n'
        'CODEX_QUEUE_PASSWORD=hunter2\n'
    )
    env_file.chmod(0o600)
    monkeypatch.delenv('CODEX_QUEUE_USAGE_COMMAND', raising=False)
    monkeypatch.delenv('CODEX_QUEUE_PASSWORD', raising=False)

    config._DOTENV.clear()
    config._load_dotenv(env_file)

    # Loaded into the private store
    assert config._DOTENV['CODEX_QUEUE_USAGE_COMMAND'] == 'usage-helper --json'
    assert config._DOTENV['CODEX_QUEUE_PASSWORD'] == 'hunter2'

    # NOT in os.environ — this is the actual security guarantee
    assert 'CODEX_QUEUE_USAGE_COMMAND' not in os.environ
    assert 'CODEX_QUEUE_PASSWORD' not in os.environ


def test_get_env_prefers_process_env_over_dotenv(monkeypatch):
    """Explicit shell exports / CI / systemd Environment= must override .env."""
    monkeypatch.setenv('CODEX_QUEUE_USAGE_COMMAND', 'from-env')
    config._DOTENV['CODEX_QUEUE_USAGE_COMMAND'] = 'from-dotenv'
    try:
        assert config.get_env('CODEX_QUEUE_USAGE_COMMAND') == 'from-env'
    finally:
        config._DOTENV.pop('CODEX_QUEUE_USAGE_COMMAND', None)


def test_get_env_falls_back_to_dotenv(monkeypatch):
    monkeypatch.delenv('CODEX_QUEUE_USAGE_COMMAND', raising=False)
    config._DOTENV['CODEX_QUEUE_USAGE_COMMAND'] = 'from-dotenv'
    try:
        assert config.get_env('CODEX_QUEUE_USAGE_COMMAND') == 'from-dotenv'
    finally:
        config._DOTENV.pop('CODEX_QUEUE_USAGE_COMMAND', None)


def test_subprocess_env_strips_chatgpt_session_token(monkeypatch):
    monkeypatch.setenv('CODEX_QUEUE_CHATGPT_SESSION_TOKEN', 'secret-token')
    monkeypatch.setenv('CODEX_QUEUE_USAGE_COMMAND', 'usage-helper')

    env = config.subprocess_env()

    assert 'CODEX_QUEUE_CHATGPT_SESSION_TOKEN' not in env
    assert 'CODEX_QUEUE_USAGE_COMMAND' not in env


def test_load_dotenv_refuses_loose_perms(tmp_path, monkeypatch, capsys):
    """0644 .env containing secrets must be refused, not warned and loaded."""
    env_file = tmp_path / '.env'
    env_file.write_text('CODEX_QUEUE_USAGE_COMMAND=usage-helper\n')
    env_file.chmod(0o644)
    config._DOTENV.clear()
    config._load_dotenv(env_file)
    err = capsys.readouterr().err
    assert 'too permissive' in err
    assert 'CODEX_QUEUE_USAGE_COMMAND' not in config._DOTENV


def test_load_dotenv_refuses_symlink(tmp_path, monkeypatch, capsys):
    real = tmp_path / 'real.env'
    real.write_text('CODEX_QUEUE_USAGE_COMMAND=usage-helper\n')
    real.chmod(0o600)
    link = tmp_path / '.env'
    link.symlink_to(real)
    config._DOTENV.clear()
    config._load_dotenv(link)
    err = capsys.readouterr().err
    assert 'symlink' in err
    assert 'CODEX_QUEUE_USAGE_COMMAND' not in config._DOTENV


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
