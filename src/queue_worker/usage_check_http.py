"""Claude.ai usage checker — HTTP backend.

Calls the same web endpoints as the claude.ai UI:
  GET https://claude.ai/api/organizations
  GET https://claude.ai/api/organizations/{uuid}/usage
authenticated by the user's `sessionKey` cookie + browser-shaped headers.

Returns the same UsageCheckResult shape as the Playwright backend so the
runner is agnostic to which path produced the result. The dispatcher in
usage_check.py owns CSV writes; this module is fetch+parse only.
"""

import gzip
import json
import math
import re
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import PROJECT_ROOT, check_secret_file_perms, get_env


BASE_URL = 'https://claude.ai/api'

# Bounds chosen so a single check cannot hold the runner's _usage_check_lock
# for more than ~30 s. fetch_usage_http runs inside that lock (do_usage_check
# in runner.py acquires it non-blocking), so a long retry storm here silently
# drops manual `Check usage now` clicks and delays the canonical T+5 anchor.
# 2 attempts × 15 s timeout + 1 backoff = ~32 s worst case. The runner re-fires
# hourly anyway, so a deeper budget buys nothing.
REQUEST_TIMEOUT_S = 15
MAX_ATTEMPTS = 2
BACKOFF_BASE_S = 2.0
RATE_LIMIT_BACKOFF_BASE_S = 3.0

ORG_CACHE_PATH = PROJECT_ROOT / 'state' / 'org_cache.json'
SESSION_KEY_FILE = Path.home() / '.config' / 'queue-worker' / 'session_key'

# In-process memo for the resolved org UUID so the retry loop and the
# every-task burn-mode check don't re-stat the cache file each time. Cleared
# on resolve failures so a misconfigured cache can self-heal.
_ORG_UUID_MEMO: Optional[str] = None

# Browser-shaped headers — Cloudflare drops Python's bare `urllib/3.x` UA.
# Accept-Language and Accept-Encoding are added explicitly because URLSession
# adds them implicitly on macOS but stdlib urllib does not.
_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

_KEY_RE   = re.compile(r'sk-ant-[A-Za-z0-9._\-]+')
_EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')


# ── Exceptions (reason codes only — never carry response bodies) ────────────

class UsageHttpError(Exception):
    """Base for HTTP-backend errors. Each subclass carries a stable `code`
    string used by the dispatcher to populate UsageCheckResult.error_code."""

    code: str = 'http_error'


class SessionKeyMissing(UsageHttpError):
    """No session key found in env or fallback file (or the configured value
    is malformed — equivalent to "not configured")."""

    code = 'session_key_missing'


class SessionKeyInvalid(UsageHttpError):
    """Server returned 401 — key is rotated/expired/wrong. Reserved for
    actual upstream rejections; format failures use SessionKeyMissing."""

    code = 'session_key_invalid'


class RateLimited(UsageHttpError):
    """Server returned 429."""

    code = 'rate_limited'


class BetweenSessions(UsageHttpError):
    """API returned five_hour with a null/past resets_at — no active session."""

    code = 'between_sessions'


class HttpUpstreamError(UsageHttpError):
    """Upstream failure with a per-instance code (cloudflare_blocked,
    http_error, network_error, bad_response, org_resolve_failed,
    multi_org_no_pin)."""

    def __init__(self, code: str, detail: str = ''):
        super().__init__(f'{code}{": " + detail if detail else ""}')
        self.code = code
        self.detail = detail


# ── Redaction ────────────────────────────────────────────────────────────────

def redact(s: str) -> str:
    """Strip session keys and emails from any string before logging or persisting."""
    if not s:
        return s
    s = _KEY_RE.sub('sk-ant-***', s)
    s = _EMAIL_RE.sub('***@***', s)
    return s


# ── Session key loading ─────────────────────────────────────────────────────

def _validate_key(key: str) -> str:
    """Format check only — raises SessionKeyMissing for malformed values.

    Format failures are local config issues equivalent to "no key configured":
    the dispatcher's `auto` branch falls back to Playwright on
    SessionKeyMissing, but NOT on SessionKeyInvalid (the latter is reserved
    for actual server 401s — only the server can adjudicate validity). A
    fat-fingered .env entry should not hard-fail; it should fall through.
    """
    key = key.strip()
    if not key.startswith('sk-ant-') or len(key) <= 10:
        raise SessionKeyMissing('configured key does not match sk-ant- format')
    return key


def _load_key_from_file(path: Path = SESSION_KEY_FILE) -> Optional[str]:
    """Read session key from a file. Returns None if absent; raises
    SessionKeyMissing with remediation if present-but-unsafe."""
    if not path.exists():
        return None
    err = check_secret_file_perms(path)
    if err:
        raise SessionKeyMissing(err)
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError as e:
        raise SessionKeyMissing(f'{path}: {e}') from None


def _load_session_key() -> str:
    """Resolve the session key. Precedence: process env > .env > 0600 file.

    Reads through ``config.get_env`` so .env values stay in a private store,
    never in os.environ — child processes (``claude -p``) MUST NOT inherit
    the session key.
    """
    raw = get_env('CLAUDE_SESSION_KEY')
    if raw and raw.strip():
        return _validate_key(raw)
    file_key = _load_key_from_file()
    if file_key:
        return _validate_key(file_key)
    raise SessionKeyMissing(
        'no CLAUDE_SESSION_KEY in env, .env, or file at '
        f'{SESSION_KEY_FILE}. See README "Setup: Claude session key".'
    )


# ── Org UUID resolution + cache ─────────────────────────────────────────────

def _read_org_cache() -> Optional[str]:
    try:
        data = json.loads(ORG_CACHE_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    uuid = data.get('org_uuid') if isinstance(data, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def _write_org_cache(uuid: str, source: str) -> None:
    """Atomic write of state/org_cache.json. Single writer (this module)."""
    try:
        ORG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            'org_uuid':  uuid,
            'cached_at': datetime.now(timezone.utc).isoformat(),
            'source':    source,
        }, indent=2)
        tmp = ORG_CACHE_PATH.with_suffix('.json.tmp')
        tmp.write_text(payload, encoding='utf-8')
        tmp.replace(ORG_CACHE_PATH)
    except OSError:
        pass  # best-effort cache; never crash the check


def _resolve_org_uuid(key: str, log_fn) -> str:
    """Resolve which org UUID to query. Order:
      1. In-process memo (populated by a previous resolve in this process)
      2. CLAUDE_ORG_UUID env (explicit override; never re-validates against API)
      3. state/org_cache.json (set after a previous successful /usage roundtrip)
      4. GET /organizations — taken only when len(orgs)==1; multi-org accounts
         must set CLAUDE_ORG_UUID to disambiguate.

    The memo lets the burn-mode loop check usage many times per cycle without
    re-stat-ing the cache file or hitting /organizations. Process-lifetime —
    tests reset it directly via `uch._ORG_UUID_MEMO = None`.
    """
    global _ORG_UUID_MEMO
    if _ORG_UUID_MEMO:
        return _ORG_UUID_MEMO
    env_uuid = (get_env('CLAUDE_ORG_UUID') or '').strip()
    if env_uuid:
        _ORG_UUID_MEMO = env_uuid
        return env_uuid
    cached = _read_org_cache()
    if cached:
        _ORG_UUID_MEMO = cached
        return cached
    log_fn('http: resolving org via /organizations (no cache)')
    orgs = _request(f'{BASE_URL}/organizations', key)
    if not isinstance(orgs, list) or not orgs:
        raise HttpUpstreamError('org_resolve_failed', 'empty list')
    # Multi-org accounts: a successful /usage call against orgs[0] only proves
    # that org exists, not that it's the user's intended Claude plan. Refuse
    # to auto-pick — make them set CLAUDE_ORG_UUID explicitly. (Single-org
    # accounts go through unchanged: there is exactly one valid choice.)
    if len(orgs) > 1:
        raise HttpUpstreamError(
            'multi_org_no_pin',
            f'{len(orgs)} orgs returned; set CLAUDE_ORG_UUID to disambiguate',
        )
    uuid = orgs[0].get('uuid') if isinstance(orgs[0], dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise HttpUpstreamError('org_resolve_failed', 'no uuid on first org')
    _ORG_UUID_MEMO = uuid
    return uuid


# ── HTTP request ─────────────────────────────────────────────────────────────

def _decode_body(raw: bytes, headers) -> bytes:
    enc = (headers.get('Content-Encoding') or '').lower() if headers else ''
    if 'gzip' in enc:
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if 'deflate' in enc:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return raw
    return raw


def _detect_cloudflare_block(status: int, headers, body: bytes) -> bool:
    if status in (403, 503):
        if headers and headers.get('cf-mitigated'):
            return True
        head = body[:512].decode('utf-8', errors='ignore').lower()
        if 'just a moment' in head or 'cf-chl-' in head:
            return True
    return False


def _request(url: str, key: str):
    """Single GET. Raises typed exceptions; never returns response bodies in
    error messages. Decodes JSON response on 2xx and returns the parsed value.
    """
    headers = {
        'Cookie':           f'sessionKey={key}',
        'Accept':           'application/json',
        'Accept-Language':  'en-US,en;q=0.9',
        'Accept-Encoding':  'gzip, deflate',
        'User-Agent':       _USER_AGENT,
        'Referer':          'https://claude.ai/',
        'Origin':           'https://claude.ai',
        'Sec-Fetch-Site':   'same-origin',
        'Sec-Fetch-Mode':   'cors',
        'Sec-Fetch-Dest':   'empty',
    }
    req = Request(url, headers=headers, method='GET')
    try:
        resp = urlopen(req, timeout=REQUEST_TIMEOUT_S)
        status = resp.status
        rheaders = resp.headers
        raw = resp.read()
    except HTTPError as e:
        status = e.code
        rheaders = e.headers
        raw = e.read() or b''
    except URLError as e:
        # str(e) can echo the URL (org UUID is mildly identifying); stick to
        # the bare class name.
        raise HttpUpstreamError('network_error', e.__class__.__name__) from None

    body = _decode_body(raw, rheaders)
    if _detect_cloudflare_block(status, rheaders, body):
        raise HttpUpstreamError('cloudflare_blocked', f'http {status}')
    if status == 401:
        raise SessionKeyInvalid('http 401')
    if status == 429:
        raise RateLimited('http 429')
    if status >= 500:
        raise HttpUpstreamError('http_error', f'http {status}')
    if not (200 <= status < 300):
        raise HttpUpstreamError('http_error', f'http {status}')
    try:
        return json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HttpUpstreamError('bad_response', 'invalid json') from None


# ── Parse /usage response ───────────────────────────────────────────────────

def _format_reset_str(reset_minutes: int) -> str:
    """Match the existing scraper's format: 'Xhr Ymin' / 'Xmin' / 'Xsec'."""
    if reset_minutes <= 0:
        return '0min'
    hrs = reset_minutes // 60
    mins = reset_minutes % 60
    if hrs:
        return f'{hrs}hr {mins}min'
    return f'{mins}min'


def _parse_resets_at(value: str) -> datetime:
    """Parse ISO8601 with mandatory tzinfo. Accepts both `+00:00` offset
    (the format claude.ai actually returns) and `Z` suffix; fromisoformat on
    Python 3.11+ handles both. Naive datetimes are rejected.
    """
    if not isinstance(value, str):
        raise HttpUpstreamError('bad_response', 'resets_at not a string')
    s = value.replace('Z', '+00:00') if value.endswith('Z') else value
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HttpUpstreamError('bad_response', 'resets_at unparseable') from None
    if dt.tzinfo is None:
        raise HttpUpstreamError('bad_response', 'resets_at lacks timezone')
    return dt


def _parse_usage(data, now_utc: datetime, log_fn) -> tuple[int, int, str]:
    """Extract (pct, reset_minutes, reset_str) from a /usage response.

    Validates only the fields we read. Unknown top-level keys are ignored
    (forward-compat for fields like `iguana_necktie`, `extra_usage`, ...).
    Raises BetweenSessions when the API has no resets_at — only Playwright's
    session-kick can recover from that.
    """
    if not isinstance(data, dict):
        raise HttpUpstreamError('bad_response', 'response not a dict')
    five_hour = data.get('five_hour')
    if not isinstance(five_hour, dict):
        raise HttpUpstreamError('bad_response', 'missing five_hour')

    util = five_hour.get('utilization')
    if not isinstance(util, (int, float)) or isinstance(util, bool):
        raise HttpUpstreamError('bad_response', 'utilization not numeric')
    if math.isnan(util) or math.isinf(util):
        raise HttpUpstreamError('bad_response', 'utilization NaN/Inf')
    if util < 0 or util > 100:
        raise HttpUpstreamError('bad_response', f'utilization {util} out of range')
    # ceil, not round: Python's round() is banker's, so round(70.5) == 70.
    # That would report 30% remaining at 29.5% actual usage_left and trigger
    # a spurious burn. ceil never under-reports usage, which is the safe
    # direction for the burn boundary in decide().
    pct = int(math.ceil(util))

    resets_at_raw = five_hour.get('resets_at')
    if resets_at_raw is None:
        raise BetweenSessions('five_hour.resets_at is null')
    resets_at = _parse_resets_at(resets_at_raw)
    delta_s = (resets_at - now_utc).total_seconds()
    if delta_s <= 0:
        # Reset already in the past per server clock — same semantics as null.
        raise BetweenSessions('resets_at in the past')

    reset_minutes = max(0, math.ceil(delta_s / 60))
    return pct, reset_minutes, _format_reset_str(reset_minutes)


# ── Public entry point ──────────────────────────────────────────────────────

def fetch_usage_http(log_fn=lambda _msg: None):
    """Fetch usage via HTTP. Returns a UsageCheckResult.

    Imports UsageCheckResult lazily to avoid a circular import with
    usage_check.py (which dispatches into this module).

    Up to MAX_ATTEMPTS retries with exponential backoff for transient errors
    (429, 5xx, network). The runner's _usage_check_lock IS held across the
    whole call (do_usage_check is the lock owner, this function runs inside
    it), so MAX_ATTEMPTS and REQUEST_TIMEOUT_S are tuned to keep total
    wall-clock under ~30 s — see the constant comments. SessionKeyInvalid and
    BetweenSessions never retry; they bubble up immediately for the dispatcher
    to handle.
    """
    from .usage_check import decide, UsageCheckResult

    key = _load_session_key()
    # Resolve once, outside the retry loop — a transient /usage failure must
    # not re-stat the cache file or re-call /organizations on every retry.
    # _resolve_org_uuid raises (multi_org_no_pin / org_resolve_failed) which
    # are non-transient and bubble up to the dispatcher.
    uuid = _resolve_org_uuid(key, log_fn)
    last_exc: Optional[BaseException] = None

    attempt = 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            data = _request(f'{BASE_URL}/organizations/{uuid}/usage', key)
            now_utc = datetime.now(timezone.utc)
            pct, reset_minutes, reset_str = _parse_usage(data, now_utc, log_fn)

            # Cache org uuid only after /usage succeeds — pinning before
            # would risk a wrong-org choice on multi-org accounts.
            # _resolve_org_uuid already rejects auto-pick when len(orgs) > 1
            # without an env override, so reaching here is safe to cache.
            if not (get_env('CLAUDE_ORG_UUID') or '').strip() and not _read_org_cache():
                _write_org_cache(uuid, source='auto')

            status = decide(pct, reset_minutes)
            return UsageCheckResult(
                pct=pct,
                reset_minutes=reset_minutes,
                reset_str=reset_str,
                status=status,
                finished_at=time.time(),
                backend='http',
            )

        except SessionKeyInvalid:
            raise  # do not retry — config issue
        except BetweenSessions:
            raise  # do not retry — fall back to Playwright kick
        except RateLimited as e:
            last_exc = e
            sleep_s = RATE_LIMIT_BACKOFF_BASE_S ** attempt
            log_fn(f'http: rate-limited (attempt {attempt}/{MAX_ATTEMPTS}), '
                   f'sleeping {sleep_s:.1f}s')
            if attempt < MAX_ATTEMPTS:
                time.sleep(sleep_s)
        except HttpUpstreamError as e:
            last_exc = e
            if e.code in ('cloudflare_blocked', 'bad_response',
                          'org_resolve_failed', 'multi_org_no_pin'):
                # Non-transient — surface immediately.
                raise
            sleep_s = BACKOFF_BASE_S ** attempt
            log_fn(f'http: {e.code} (attempt {attempt}/{MAX_ATTEMPTS}), '
                   f'sleeping {sleep_s:.1f}s')
            if attempt < MAX_ATTEMPTS:
                time.sleep(sleep_s)

    if last_exc:
        raise last_exc
    raise HttpUpstreamError('http_error', 'retries exhausted')
