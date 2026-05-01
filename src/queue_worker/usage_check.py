"""Claude.ai usage checker — HTTP-only.

Single backend: `usage_check_http.fetch_usage_http` GETs the same endpoints
the claude.ai UI uses, authenticated with a sessionKey cookie. This module
wraps it with three things the backend itself doesn't do:

  * Map the typed `UsageHttpError` subclasses to a `UsageCheckResult` with a
    stable `error_code` field.
  * Recover from `between_sessions` (no active 5-hour window) by running
    `claude -p "hi"` to kick a fresh session, then re-running the fetch.
  * Append exactly one row to `usage_history.csv` per logical check, even
    when a kick recovery happened in the middle.
"""

import csv
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import PROJECT_ROOT


USAGE_CSV = PROJECT_ROOT / 'usage_history.csv'

# Burn tokens when the session has at least BURN_USAGE_THRESHOLD_PCT remaining
# AND less than BURN_RESET_WINDOW_MIN until reset — unused budget about to be
# wiped. The 70-minute window pairs with the HH:00 hourly + T-60 anchored
# check schedule: at ~60 min remaining the check falls inside the window with
# slack for session-start jitter, so the burn fires ~60 min before reset
# instead of bleeding into the next session. Any other outcome (including
# parse / network errors) fails closed: stay chilling.
BURN_USAGE_THRESHOLD_PCT = 30
BURN_RESET_WINDOW_MIN    = 70

# Sent via `claude -p` when the API reports the account is between 5-hour
# sessions. The window starts on first message — one short message flips
# state, and the next fetch returns real numbers.
SESSION_KICK_MESSAGE = 'hi'
KICK_CLI_TIMEOUT_S   = 60
KICK_CLI_BIN         = 'claude'  # found via PATH; same binary used by executor.py

# Settle for Anthropic's backend to register the freshly-kicked session
# before the re-fetch.
POST_KICK_SETTLE_S = 3.0


@dataclass
class UsageCheckResult:
    pct: Optional[int] = None
    reset_minutes: Optional[int] = None
    reset_str: Optional[str] = None
    status: str = 'ERROR'
    error: Optional[str] = None
    finished_at: Optional[float] = None  # epoch seconds when fetch completed
    error_code: Optional[str] = None     # stable code: session_key_invalid,
                                         # between_sessions, cloudflare_blocked,
                                         # rate_limited, network_error,
                                         # http_error, bad_response,
                                         # org_resolve_failed, parse_failed
    backend: Optional[str] = 'http'      # always 'http' (kept for JSON shape compat)


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


def _kick(log_fn) -> None:
    """Send `SESSION_KICK_MESSAGE` via `claude -p` to start a 5-hour session.

    Failure modes:
      - `claude` not on PATH → RuntimeError with install pointer.
      - Auth / rate-limit / network → non-zero exit, first stderr line
        surfaced (after redaction).
      - Anthropic slow → bounded by KICK_CLI_TIMEOUT_S.
    """
    from .config import subprocess_env
    from .usage_check_http import redact

    log_fn(
        f'kick: running `{KICK_CLI_BIN} -p {SESSION_KICK_MESSAGE!r}` '
        f'(timeout {KICK_CLI_TIMEOUT_S}s)'
    )
    try:
        result = subprocess.run(
            [KICK_CLI_BIN, '-p', SESSION_KICK_MESSAGE],
            capture_output=True, text=True,
            timeout=KICK_CLI_TIMEOUT_S,
            env=subprocess_env(),
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"kick: '{KICK_CLI_BIN}' not on PATH — install Claude Code "
            f"(https://docs.anthropic.com/en/docs/claude-code) and ensure "
            f"the runner's environment can find it"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f'kick: {KICK_CLI_BIN} -p timed out after {KICK_CLI_TIMEOUT_S}s'
        ) from None

    if result.returncode != 0:
        err_lines = (result.stderr or result.stdout or '').strip().splitlines()
        first = redact(err_lines[0][:200]) if err_lines else '<no output>'
        raise RuntimeError(
            f'kick: {KICK_CLI_BIN} -p exited {result.returncode}: {first}'
        )

    log_fn(f'kick: {KICK_CLI_BIN} -p exited 0, session should be active')


def _fetch(log_fn) -> UsageCheckResult:
    """Run the HTTP backend, mapping typed exceptions to a UsageCheckResult."""
    from .usage_check_http import fetch_usage_http, redact, UsageHttpError
    try:
        return fetch_usage_http(log_fn)
    except UsageHttpError as e:
        return UsageCheckResult(
            status=f'ERROR:{e.code}',
            error=redact(str(e)),
            finished_at=time.time(),
            error_code=e.code,
        )
    except Exception as e:
        msg = redact(str(e) or type(e).__name__)[:200]
        return UsageCheckResult(
            status='ERROR:http_unexpected',
            error=msg,
            finished_at=time.time(),
            error_code='http_error',
        )


def check_usage_once(log_fn=lambda _msg: None) -> UsageCheckResult:
    """Fetch usage; on `between_sessions`, kick once and re-fetch. Writes
    exactly one CSV row, then returns the (possibly recovered) result."""
    result = _fetch(log_fn)
    if result.error_code == 'between_sessions':
        log_fn(f'between_sessions — kicking via {KICK_CLI_BIN} -p')
        try:
            _kick(log_fn)
        except Exception as e:
            from .usage_check_http import redact
            log_fn(f'kick failed: {redact(str(e))}')
        else:
            log_fn(f'kick done, settling {POST_KICK_SETTLE_S}s before re-check')
            time.sleep(POST_KICK_SETTLE_S)
            result = _fetch(log_fn)

    pct_str, reset_str, status = _csv_row(result)
    _append_csv(pct_str, reset_str, status)
    return result
