"""Codex usage checker — local command backend.

Runs the command configured in ``CODEX_QUEUE_USAGE_COMMAND`` and expects JSON
on stdout:

    {"used_pct": 71, "reset_minutes": 58}

The command is local and executed without a shell. This module only fetches and
parses; usage_check.py owns CSV writes and the runner owns burn decisions.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import time
from typing import Any

from .config import get_env, subprocess_env


DEFAULT_TIMEOUT_SECONDS = 30
_KEY_RE = re.compile(r'sk-[A-Za-z0-9._\-]+')
_EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')


class UsageCommandError(Exception):
    """Base for command-backend errors with stable API-facing codes."""

    code = 'usage_command_error'


class UsageCommandMissing(UsageCommandError):
    code = 'usage_command_missing'


class UsageCommandFailed(UsageCommandError):
    code = 'usage_command_failed'


class UsageCommandTimeout(UsageCommandError):
    code = 'usage_command_timeout'


class BadResponse(UsageCommandError):
    code = 'bad_response'


def redact(s: str | None) -> str | None:
    """Strip likely API keys and emails from any string before logging."""
    if not s:
        return s
    s = _KEY_RE.sub('sk-***', s)
    return _EMAIL_RE.sub('***@***', s)


def _timeout_seconds() -> int:
    raw = (get_env('CODEX_QUEUE_USAGE_TIMEOUT_SECONDS') or '').strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise UsageCommandMissing(
            'CODEX_QUEUE_USAGE_TIMEOUT_SECONDS must be an integer'
        ) from None
    if value <= 0:
        raise UsageCommandMissing(
            'CODEX_QUEUE_USAGE_TIMEOUT_SECONDS must be greater than 0'
        )
    return value


def _command_args() -> list[str]:
    raw = (get_env('CODEX_QUEUE_USAGE_COMMAND') or '').strip()
    if not raw:
        raise UsageCommandMissing('CODEX_QUEUE_USAGE_COMMAND is not set')
    try:
        args = shlex.split(raw)
    except ValueError as e:
        raise UsageCommandMissing(f'CODEX_QUEUE_USAGE_COMMAND: {e}') from None
    if not args:
        raise UsageCommandMissing('CODEX_QUEUE_USAGE_COMMAND is empty')
    return args


def _format_reset_str(reset_minutes: int) -> str:
    if reset_minutes <= 0:
        return '0min'
    hrs = reset_minutes // 60
    mins = reset_minutes % 60
    if hrs:
        return f'{hrs}hr {mins}min'
    return f'{mins}min'


def _coerce_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BadResponse(f'{key} not numeric')
    if math.isnan(value) or math.isinf(value):
        raise BadResponse(f'{key} not finite')
    if int(value) != value:
        raise BadResponse(f'{key} must be a whole number')
    return int(value)


def _parse_usage(raw: str) -> tuple[int, int, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise BadResponse('stdout was not valid JSON') from None
    if not isinstance(data, dict):
        raise BadResponse('response not a JSON object')

    pct = _coerce_int(data, 'used_pct')
    reset_minutes = _coerce_int(data, 'reset_minutes')
    if pct < 0 or pct > 100:
        raise BadResponse('used_pct must be between 0 and 100')
    if reset_minutes < 0:
        raise BadResponse('reset_minutes must be greater than or equal to 0')
    return pct, reset_minutes, _format_reset_str(reset_minutes)


def fetch_usage_command(log_fn):
    """Run the configured command and return a UsageCheckResult."""
    from .usage_check import UsageCheckResult, decide

    args = _command_args()
    timeout_s = _timeout_seconds()
    log_fn(f'usage command: running `{args[0]}` (timeout {timeout_s}s)')
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=subprocess_env(),
        )
    except FileNotFoundError:
        raise UsageCommandMissing(f'{args[0]!r} not found on PATH') from None
    except subprocess.TimeoutExpired:
        raise UsageCommandTimeout(f'usage command timed out after {timeout_s}s') from None

    if result.returncode != 0:
        err_lines = (result.stderr or result.stdout or '').strip().splitlines()
        detail = redact(err_lines[0][:200]) if err_lines else '<no output>'
        raise UsageCommandFailed(
            f'usage command exited {result.returncode}: {detail}'
        )

    pct, reset_minutes, reset_str = _parse_usage(result.stdout.strip())
    return UsageCheckResult(
        pct=pct,
        reset_minutes=reset_minutes,
        reset_str=reset_str,
        status=decide(pct, reset_minutes),
        finished_at=time.time(),
        backend='command',
    )
