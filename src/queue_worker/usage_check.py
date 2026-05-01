"""Claude.ai usage checker — dispatcher + Playwright backend.

Two backends:
  * `_check_via_playwright`: scrapes claude.ai/settings/usage via long-running
    CDP Chrome (the historical path; can kick a `hi` message to wake the
    "between sessions" page).
  * `usage_check_http.fetch_usage_http`: GETs the same endpoints the claude.ai
    UI uses, with a sessionKey cookie + browser-spoof headers (Phase 1+).

`check_usage_once` is the dispatcher. It selects a backend based on
QUEUE_WORKER_USAGE_BACKEND (auto|http|playwright), owns the single CSV write
(so a fallback never produces two rows for one logical check), and returns a
UsageCheckResult to the runner.
"""

import csv
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import PROJECT_ROOT, get_env

USAGE_CSV   = PROJECT_ROOT / 'usage_history.csv'
PROFILE_DIR = PROJECT_ROOT / '.chrome-profile'
SCREENSHOT_DIR = PROJECT_ROOT / 'logs' / 'usage_check_img'

# Keep usage-check screenshots for a week so post-hoc debugging works for any
# anomaly the user noticed in the last few days. Hard cap as a safety net
# against runaway disk use during a check storm — burn mode does one check
# per task, easily 10+/hour, so a count-only cap was misleading.
SCREENSHOT_RETENTION_DAYS = 7
SCREENSHOT_HARD_CAP = 5000

CDP_PORT    = 9223
CDP_URL     = f'http://localhost:{CDP_PORT}'
USAGE_URL   = 'https://claude.ai/settings/usage'

# Sent via `claude -p` when the usage page (or HTTP API) is in its
# "between sessions" state. The 5-hour rolling window starts on first
# message, so one short message flips state and the next scrape/fetch
# returns real data.
SESSION_KICK_MESSAGE = 'hi'

# `claude -p "hi"` should respond in seconds; cap generously to handle
# unusually slow responses without hanging the runner indefinitely.
KICK_CLI_TIMEOUT_S = 60
KICK_CLI_BIN       = 'claude'  # same binary used by executor.py — found via PATH

_RENDERED_PREDICATE = (
    "() => { const m = document.querySelector('main');"
    " if (!m) return false;"
    " const t = m.innerText;"
    " return /Current session/.test(t)"
    " && /\\d+\\s*%\\s*used/.test(t)"
    " && /Resets?\\s+in/i.test(t); }"
)

# Relaxed predicate for the post-kick retry: drops the 'Current session' header
# requirement and accepts any page that has both the percentage block and the
# reset countdown. Only applied as a fallback after the strict predicate fails.
_RENDERED_PREDICATE_RELAXED = (
    "() => { const m = document.querySelector('main');"
    " if (!m) return false;"
    " const t = m.innerText;"
    " return /\\d+\\s*%\\s*used/.test(t)"
    " && /Resets?\\s+in/i.test(t); }"
)

# Burn tokens when the session has at least BURN_USAGE_THRESHOLD_PCT remaining
# AND less than BURN_RESET_WINDOW_MIN until reset — unused budget about to be
# wiped. The 70-minute window pairs with the HH:00 hourly + T-60 anchored
# check schedule: at ~60 min remaining the check falls inside the window with
# slack for session-start jitter, so the burn fires ~60 min before reset
# instead of bleeding into the next session. Any other outcome (including
# parse / scrape errors) fails closed: stay chilling.
BURN_USAGE_THRESHOLD_PCT = 30
BURN_RESET_WINDOW_MIN    = 70

PAGE_TIMEOUT_MS   = 30_000
RENDER_TIMEOUT_MS = 10_000

# Settle for Anthropic's backend to register a freshly-started 5-hour
# session against the usage API/page after a `claude -p "hi"` kick. Used
# by the dispatcher's single recovery path; both HTTP and Playwright
# backends pause this long between kick and re-check.
POST_KICK_SETTLE_S = 3.0


# Internal sentinel raised by scrape_usage_text when the usage page rendered
# but the session block is in its placeholder state ("Starts when a message
# is sent"). The dispatcher catches this and runs the shared kick recovery.
class _BetweenSessionsRender(Exception):
    pass

CHROME_PATHS = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
]


@dataclass
class UsageCheckResult:
    pct: Optional[int] = None
    reset_minutes: Optional[int] = None
    reset_str: Optional[str] = None
    status: str = 'ERROR'
    error: Optional[str] = None
    finished_at: Optional[float] = None  # epoch seconds when scrape completed
    error_code: Optional[str] = None     # stable code: session_key_invalid,
                                         # between_sessions, cloudflare_blocked,
                                         # rate_limited, network_error,
                                         # http_error, bad_response,
                                         # org_resolve_failed, parse_failed,
                                         # chrome_unavailable, scrape_failed
    backend: Optional[str] = None        # 'http' | 'playwright' (which backend
                                         # produced this result)


def find_chrome() -> Optional[str]:
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def timestamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect(('127.0.0.1', port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


# Headers that mark the end of the Current session block on the usage page.
# Anything matched after these belongs to weekly limits or other sections and
# must not contaminate session-level pct/reset readings.
_NEXT_SECTION_HEADERS = (
    'Weekly limits',
    'Additional features',
    'Last updated:',
)

# Sanity ceiling for current-session reset. The 5-hour window means any
# reading above this is definitely from another section that leaked through
# the bounded match — drop it. 10 min margin covers clock skew + display lag.
_MAX_CURRENT_SESSION_RESET_MIN = 5 * 60 + 10


def parse_usage_text(text: str) -> tuple[Optional[int], Optional[str], Optional[int]]:
    """Extract (pct, reset_str, reset_minutes) from the CURRENT SESSION block only.

    The usage page also renders Weekly limits with its own pct/reset, but
    those have a different semantic (7-day rolling vs the 5-hour rolling
    session) and the burn decision only cares about the 5-hour window.
    Bounding the search explicitly between "Current session" and the next
    section header prevents weekly text leaking into session readings if
    Anthropic ever changes weekly's format from "Resets Wed 8:00 PM" to
    "Resets in N days".
    """
    idx = text.find('Current session')
    if idx < 0:
        return None, None, None
    end = len(text)
    for header in _NEXT_SECTION_HEADERS:
        h = text.find(header, idx + len('Current session'))
        if h > 0 and h < end:
            end = h
    section = text[idx:end]

    pct_match = re.search(r'(\d+)\s*%\s*used', section, re.IGNORECASE)
    raw_pct = int(pct_match.group(1)) if pct_match else None
    if raw_pct is not None and not (0 <= raw_pct <= 100):
        print(f'WARNING: impossible pct={raw_pct} parsed from page', file=sys.stderr)
    pct = raw_pct if raw_pct is not None and 0 <= raw_pct <= 100 else None

    reset_minutes: Optional[int] = None
    reset_str: Optional[str] = None
    reset_block = re.search(
        r'Resets?\s+in[\s\n]+([^\n]+)', section, re.IGNORECASE
    )
    if reset_block:
        time_text = reset_block.group(1)
        hr = re.search(r'(\d+)\s*hr', time_text, re.IGNORECASE)
        mn = re.search(r'(\d+)\s*min', time_text, re.IGNORECASE)
        sc = re.search(r'(\d+)\s*sec', time_text, re.IGNORECASE)
        if hr or mn or sc:
            hrs = int(hr.group(1)) if hr else 0
            mins = int(mn.group(1)) if mn else 0
            secs = int(sc.group(1)) if sc else 0
            # Round any sub-minute residue up so the "imminent reset" signal
            # stays non-zero — runner compares reset_minutes < BURN_RESET_WINDOW_MIN.
            total = hrs * 60 + mins + (1 if secs > 0 else 0)
            if total > _MAX_CURRENT_SESSION_RESET_MIN:
                # Bounded section still let weekly leak through (or session
                # length changed upstream). Treat as parse failure rather
                # than report a wrong value.
                print(
                    f'WARNING: implausible session reset={total}min parsed from page',
                    file=sys.stderr,
                )
            else:
                if hrs:
                    reset_str = f'{hrs}hr {mins}min'
                elif mins:
                    reset_str = f'{mins}min'
                else:
                    reset_str = f'{secs}sec'
                reset_minutes = total

    return pct, reset_str, reset_minutes


def decide(pct: Optional[int], reset_minutes: Optional[int]) -> str:
    """Return a status label. The actual burn decision is anchor-aware and
    lives in the runner — this function just describes the latest scrape."""
    if pct is None or reset_minutes is None:
        return 'ERROR:Parse_failed'
    usage_left = 100 - pct
    if usage_left >= BURN_USAGE_THRESHOLD_PCT and reset_minutes < BURN_RESET_WINDOW_MIN:
        return 'NEED TO BURN TOKEN !'
    return 'Chilling'


def _append_csv(pct_str: str, reset_str: str, status: str) -> None:
    ts = timestamp()
    USAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_CSV.open('a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([ts, pct_str, reset_str, status])


def _safe_main_text(page) -> str:
    try:
        return page.inner_text('main')
    except Exception:
        return ''


def _diag_snapshot(page) -> dict:
    """Structured per-check diagnostic — logged on every check.

    Tells us *which* clause of the strict predicate is missing when the
    page times out, so the next round of marker tightening is informed
    rather than guessed.
    """
    text = _safe_main_text(page)
    head = ' '.join(text[:300].split())
    return {
        'url': getattr(page, 'url', '?'),
        'inner_len': len(text),
        'inner_head': head,
        'has_current_session': 'Current session' in text,
        'has_pct_used': bool(re.search(r'\d+\s*%\s*used', text)),
        'has_resets_in': bool(re.search(r'Resets?\s+in', text, re.IGNORECASE)),
    }


def _save_screenshot(page, label: str) -> Optional[str]:
    """Save a small screenshot to logs/usage_check_img/. Never raises.

    Microsecond filename suffix prevents collisions on rapid manual retries.
    Prunes oldest files past SCREENSHOT_RETENTION before writing.
    """
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _prune_screenshots()
        ts = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        out = SCREENSHOT_DIR / f'{ts}__{label}.png'
        page.screenshot(path=str(out), full_page=False, timeout=3_000)
        return str(out)
    except Exception:
        return None


def _prune_screenshots() -> None:
    """Delete screenshots older than SCREENSHOT_RETENTION_DAYS, plus a hard
    cap as a safety net against runaway disk use. Best-effort, never raises.
    """
    try:
        cutoff = time.time() - (SCREENSHOT_RETENTION_DAYS * 86400)
        for f in SCREENSHOT_DIR.glob('*.png'):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        files = sorted(SCREENSHOT_DIR.glob('*.png'),
                       key=lambda p: p.stat().st_mtime)
        excess = len(files) - SCREENSHOT_HARD_CAP
        if excess > 0:
            for f in files[:excess]:
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _kick_via_cli(log_fn) -> None:
    """Send SESSION_KICK_MESSAGE via `claude -p` to start a 5-hour session.

    Vastly simpler than driving the browser to do the same thing:
      - No editor selector cascade to chase when Anthropic tweaks the UI.
      - No URL waits or bubble verification: `claude -p` exits 0 only
        when the response stream completes, which is itself proof the
        session is now live.
      - `claude` and `claude.ai` share the same plan / 5-hour rolling
        window, so usage registered via the CLI shows up on the usage
        page (which is what the post-kick reload then verifies).

    Failure modes:
      - `claude` not on PATH → clear RuntimeError with install pointer.
      - Auth / rate-limit / network → non-zero exit, first stderr line
        surfaced (after redaction).
      - Anthropic slow → bounded by KICK_CLI_TIMEOUT_S.
    """
    import subprocess
    from .config import subprocess_env
    from .usage_check_http import redact

    log_fn(
        f'kick: running `{KICK_CLI_BIN} -p {SESSION_KICK_MESSAGE!r}` '
        f'(timeout {KICK_CLI_TIMEOUT_S}s)'
    )
    try:
        result = subprocess.run(
            [KICK_CLI_BIN, '-p', SESSION_KICK_MESSAGE],
            capture_output=True,
            text=True,
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


def _wait_for_usage_render(page, log_fn) -> None:
    """Wait for either the strict or the relaxed render predicate.

    Strict requires `Current session` + `% used` + `Resets in`. Relaxed
    drops the header and accepts any page where `% used` and `Resets in`
    are both present. Two attempts each with 1.5s backoff covers slow
    renders.
    """
    from playwright.sync_api import TimeoutError as PWTimeoutError

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            page.wait_for_function(_RENDERED_PREDICATE, timeout=RENDER_TIMEOUT_MS)
            return
        except PWTimeoutError as e:
            last_exc = e
        time.sleep(1.5)
    # Strict failed twice — try the relaxed predicate before giving up.
    try:
        page.wait_for_function(_RENDERED_PREDICATE_RELAXED, timeout=RENDER_TIMEOUT_MS)
        log_fn('usage render: relaxed predicate accepted (header missing)')
        return
    except PWTimeoutError as e:
        last_exc = e
    raise last_exc  # type: ignore[misc]


def scrape_usage_text(log_fn=lambda _msg: None) -> str:
    """Connect to Chrome, reload usage tab, return main-panel text.

    Raises:
      _BetweenSessionsRender — render predicate timed out while on the
        settings/usage page. The dispatcher catches this, runs the shared
        kick recovery, and re-calls the backend.
      RuntimeError / playwright errors — anything else (no browser context,
        navigation off-page, etc). Treated as scrape_failed by the caller.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError('No browser context found (run: check_usage.py start)')
        context = browser.contexts[0]

        page = next((p for p in context.pages if 'settings/usage' in p.url), None)
        if page:
            page.reload(wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)
        else:
            page = context.new_page()
            page.goto(USAGE_URL, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)

        try:
            _wait_for_usage_render(page, log_fn)
        except PWTimeoutError:
            log_fn(f'usage diag: {_diag_snapshot(page)}')
            if 'settings/usage' in (getattr(page, 'url', '') or ''):
                _save_screenshot(page, 'between-sessions')
                raise _BetweenSessionsRender(
                    'render timeout on usage page (likely between-sessions placeholder)'
                ) from None
            _save_screenshot(page, 'error')
            raise

        log_fn(f'usage diag: {_diag_snapshot(page)}')
        _save_screenshot(page, 'ok')
        return page.inner_text('main')


def _check_via_playwright(log_fn) -> UsageCheckResult:
    """Playwright/CDP backend. Auto-launches Chrome if needed.

    Fails closed: on any error, returns a result with the error set. Does NOT
    write the CSV — the dispatcher (`check_usage_once`) owns that so a backend
    fallback never produces two rows for one logical check.
    """
    if not ensure_chrome_running():
        msg = 'Chrome unavailable (not found or failed to start within 10s)'
        return UsageCheckResult(
            status=f'ERROR:{msg[:80]}', error=msg,
            finished_at=time.time(), error_code='chrome_unavailable',
            backend='playwright',
        )

    try:
        text = scrape_usage_text(log_fn)
    except _BetweenSessionsRender as e:
        return UsageCheckResult(
            status='ERROR:between_sessions',
            error=str(e), finished_at=time.time(),
            error_code='between_sessions', backend='playwright',
        )
    except Exception as e:
        err = str(e) or type(e).__name__
        clean = re.sub(r'\s+', ' ', err).strip()[:80]
        return UsageCheckResult(
            status=f'ERROR:{clean}', error=err,
            finished_at=time.time(), error_code='scrape_failed',
            backend='playwright',
        )

    pct, reset_str, reset_minutes = parse_usage_text(text)
    status = decide(pct, reset_minutes)
    error_code = 'parse_failed' if pct is None or reset_minutes is None else None

    return UsageCheckResult(
        pct=pct, reset_minutes=reset_minutes, reset_str=reset_str,
        status=status, finished_at=time.time(), error_code=error_code,
        backend='playwright',
    )


# Codes where 'auto' falls all the way back from HTTP to Playwright. Both
# backends can independently report `between_sessions` and the dispatcher
# kicks once across both before deciding to escalate.
_HTTP_FALLBACK_CODES = frozenset({
    'session_key_missing',
    'cloudflare_blocked',
})

# The single error class where the shared CLI kick can fix things. Other
# errors (rate limit, network, malformed response, parse failure) are
# unrelated to session presence and re-running with a kick wouldn't help.
_KICK_RECOVERABLE_CODES = frozenset({
    'between_sessions',
})


def _check_via_http(log_fn) -> UsageCheckResult:
    """HTTP backend wrapper: catches typed exceptions and maps them to a
    UsageCheckResult with a stable error_code. Does NOT write the CSV.

    Each UsageHttpError subclass carries a `code` class attribute, so one
    handler covers all of them. Bare Exception is the fallback for unexpected
    bugs — the HTTP module only puts reason codes in exception messages, but
    we redact defensively in case a future bug introduces a leak.
    """
    from .usage_check_http import fetch_usage_http, redact, UsageHttpError

    try:
        return fetch_usage_http(log_fn)
    except UsageHttpError as e:
        return UsageCheckResult(
            status=f'ERROR:{e.code}',
            error=redact(str(e)), finished_at=time.time(),
            error_code=e.code, backend='http',
        )
    except Exception as e:
        msg = redact(str(e) or type(e).__name__)[:200]
        return UsageCheckResult(
            status='ERROR:http_unexpected',
            error=msg, finished_at=time.time(),
            error_code='http_error', backend='http',
        )


def _has_session_key(log_fn) -> bool:
    """Cheap pre-check used by `auto` to decide whether HTTP is even possible.

    Surfaces *non-absent* errors (like a 0644 session_key file or owner
    mismatch) via log_fn so a misconfigured user can see the problem instead
    of silently falling through to Playwright forever.
    """
    raw = get_env('CLAUDE_SESSION_KEY')
    if raw and raw.strip():
        return True
    from .usage_check_http import _load_key_from_file, redact
    try:
        return bool(_load_key_from_file())
    except Exception as e:
        log_fn(f'http: session-key file unusable: {redact(str(e))}')
        return False


def _csv_status_for(result: UsageCheckResult) -> tuple[str, str, str]:
    """Format (pct_str, reset_str, status) for the CSV row."""
    pct_str = f'{result.pct}%' if result.pct is not None else 'N/A'
    reset_str = result.reset_str or 'N/A'
    status = result.status[:120]  # cap unbounded error strings
    return pct_str, reset_str, status


def _run_backend(backend: str, log_fn) -> UsageCheckResult:
    """One pass through the selected backend, no kick recovery. The dispatcher
    wraps this with a shared kick-and-retry."""
    if backend == 'http':
        return _check_via_http(log_fn)
    if backend == 'playwright':
        return _check_via_playwright(log_fn)
    # auto: HTTP first if a key is configured, fall back to Playwright on
    # codes the CLI kick can't fix.
    if not _has_session_key(log_fn):
        log_fn('http: no session key configured → using playwright')
        return _check_via_playwright(log_fn)
    result = _check_via_http(log_fn)
    if result.error_code in _HTTP_FALLBACK_CODES:
        log_fn(f'http: {result.error_code} → falling back to playwright')
        return _check_via_playwright(log_fn)
    return result


def check_usage_once(log_fn=lambda _msg: None) -> UsageCheckResult:
    """Top-level dispatcher. Runs the selected backend; if either backend
    reports `between_sessions`, kicks once via `claude -p "hi"` and re-runs.
    Writes exactly one CSV row regardless of recovery — the usage chart
    never sees phantom rows for transient errors that recovered.

    Backends (QUEUE_WORKER_USAGE_BACKEND):
      * http       — HTTP only.
      * playwright — Browser scrape only.
      * auto       — HTTP if a key is configured; falls back to Playwright
                     on session_key_missing / cloudflare_blocked. Does NOT
                     fall back on session_key_invalid (config issue must
                     surface, not be silently bypassed).
    """
    backend = (get_env('QUEUE_WORKER_USAGE_BACKEND') or 'auto').strip().lower()
    if backend not in ('auto', 'http', 'playwright'):
        log_fn(f'unknown QUEUE_WORKER_USAGE_BACKEND={backend!r}, using auto')
        backend = 'auto'

    result = _run_backend(backend, log_fn)
    if result.error_code in _KICK_RECOVERABLE_CODES:
        log_fn(f'{result.backend}: {result.error_code} — kicking via claude -p')
        try:
            _kick_via_cli(log_fn)
        except Exception as e:
            from .usage_check_http import redact
            log_fn(f'kick failed: {redact(str(e))}')
        else:
            log_fn(f'kick done, settling {POST_KICK_SETTLE_S}s before re-check')
            time.sleep(POST_KICK_SETTLE_S)
            result = _run_backend(backend, log_fn)

    pct_str, reset_str, status = _csv_status_for(result)
    _append_csv(pct_str, reset_str, status)
    return result


def start_chrome() -> int:
    """Launch Chrome with CDP. Returns PID, or 0 if Chrome is already on CDP_PORT."""
    if _port_in_use(CDP_PORT):
        return 0
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError('Chrome not found. Checked:\n' + '\n'.join(CHROME_PATHS))
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [chrome, f'--remote-debugging-port={CDP_PORT}',
         f'--user-data-dir={PROFILE_DIR}', USAGE_URL],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def ensure_chrome_running(wait_seconds: float = 10.0) -> bool:
    """Idempotently ensure CDP Chrome is up. Launches it if missing.

    Returns True if Chrome is reachable on CDP_PORT (already running, or
    launched and now ready). Returns False if Chrome could not be started or
    didn't bind the port within wait_seconds.

    Safe to call from multiple threads / processes — port check makes the
    launch a no-op when Chrome is already running.
    """
    if _port_in_use(CDP_PORT):
        return True
    try:
        start_chrome()
    except (RuntimeError, OSError):
        # RuntimeError: Chrome binary not found.
        # OSError: Popen can raise PermissionError (OSError subclass) or
        # other launch failures. Fail closed so check_usage_once records an
        # error instead of tracebacking out to direct CLI callers.
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _port_in_use(CDP_PORT):
            return True
        time.sleep(0.25)
    return False
