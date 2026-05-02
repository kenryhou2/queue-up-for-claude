"""Codex usage checker dispatcher.

The single backend is a local command configured by
``CODEX_QUEUE_USAGE_COMMAND``. It prints JSON with ``used_pct`` and
``reset_minutes``. This module maps backend errors to UsageCheckResult and
appends exactly one row to usage_history.csv per logical check.
"""

import csv
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import PROJECT_ROOT


USAGE_CSV = PROJECT_ROOT / 'usage_history.csv'

# Burn when the usage window has at least BURN_USAGE_THRESHOLD_PCT remaining
# AND less than BURN_RESET_WINDOW_MIN until reset — unused budget about to be
# wiped. Any command/provider error fails closed: stay chilling.
BURN_USAGE_THRESHOLD_PCT = 30
BURN_RESET_WINDOW_MIN    = 70


@dataclass
class UsageCheckResult:
    pct: Optional[int] = None
    reset_minutes: Optional[int] = None
    reset_str: Optional[str] = None
    status: str = 'ERROR'
    error: Optional[str] = None
    finished_at: Optional[float] = None  # epoch seconds when fetch completed
    error_code: Optional[str] = None     # stable code: usage_command_missing,
                                         # usage_command_failed,
                                         # usage_command_timeout,
                                         # bad_response
    backend: Optional[str] = 'command'


def decide(pct: Optional[int], reset_minutes: Optional[int]) -> str:
    """Return a status label. The actual burn decision is anchor-aware and
    lives in the runner — this function just describes the latest fetch."""
    if pct is None or reset_minutes is None:
        return 'ERROR:Parse_failed'
    usage_left = 100 - pct
    if usage_left >= BURN_USAGE_THRESHOLD_PCT and reset_minutes < BURN_RESET_WINDOW_MIN:
        return 'NEED TO BURN TOKEN !'
    return 'Chilling'


def _timestamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _append_csv(pct_str: str, reset_str: str, status: str) -> None:
    USAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_CSV.open('a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([_timestamp(), pct_str, reset_str, status])


def _csv_row(result: UsageCheckResult) -> tuple[str, str, str]:
    pct_str   = f'{result.pct}%' if result.pct is not None else 'N/A'
    reset_str = result.reset_str or 'N/A'
    status    = result.status[:120]  # cap unbounded error strings
    return pct_str, reset_str, status


def _fetch(log_fn) -> UsageCheckResult:
    """Run the command backend, mapping typed exceptions to a UsageCheckResult."""
    from .usage_check_command import (
        fetch_usage_command, redact, UsageCommandError,
    )
    try:
        return fetch_usage_command(log_fn)
    except UsageCommandError as e:
        return UsageCheckResult(
            status=f'ERROR:{e.code}',
            error=redact(str(e)),
            finished_at=time.time(),
            error_code=e.code,
        )
    except Exception as e:
        msg = redact(str(e) or type(e).__name__)[:200]
        return UsageCheckResult(
            status='ERROR:command_unexpected',
            error=msg,
            finished_at=time.time(),
            error_code='usage_command_failed',
        )


def check_usage_once(log_fn=lambda _msg: None) -> UsageCheckResult:
    """Fetch usage, append one CSV row, and return the result."""
    result = _fetch(log_fn)
    pct_str, reset_str, status = _csv_row(result)
    _append_csv(pct_str, reset_str, status)
    return result
