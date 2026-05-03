"""ChatGPT Codex cloud usage checker — HTTP backend.

Fetches token usage from the ChatGPT Codex cloud analytics API, the same data
visible at https://chatgpt.com/codex/cloud/settings/analytics#usage

Authentication:
  The ChatGPT session token is a NextAuth cookie. To find yours:
  1. Log in at https://chatgpt.com
  2. Open DevTools → Application → Cookies → chatgpt.com
  3. Copy the value of  __Secure-next-auth.session-token

  Then store it in ONE of:
    • CODEX_QUEUE_CHATGPT_SESSION_TOKEN  env var or .env
    • ~/.config/queue-worker/chatgpt_session_token  (chmod 0600)

Configuration:
  CODEX_QUEUE_CHATGPT_USAGE_URL  — override the usage API endpoint
      default: https://chatgpt.com/backend-api/codex/cloud/usage

Returns a UsageCheckResult. The dispatcher in usage_check.py owns CSV writes.
"""

from __future__ import annotations

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
from urllib.parse import urljoin, urlparse

from .config import check_secret_file_perms, get_env


# NextAuth session endpoint — returns {"accessToken": "..."} for API calls.
_NEXTAUTH_SESSION_URL = 'https://chatgpt.com/api/auth/session'

# Default usage endpoint. Override with CODEX_QUEUE_CHATGPT_USAGE_URL if
# OpenAI changes the path. Inspect browser DevTools → Network on the analytics
# page to find the exact call if the default stops working.
_DEFAULT_USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
_ANALYTICS_PAGE_URL = 'https://chatgpt.com/codex/cloud/settings/analytics'

REQUEST_TIMEOUT_S = 15
MAX_ATTEMPTS = 2
BACKOFF_BASE_S = 2.0

SESSION_TOKEN_FILE = Path.home() / '.config' / 'queue-worker' / 'chatgpt_session_token'

_SESSION_TOKEN_RE = re.compile(r'[A-Za-z0-9._\-]{20,}')
_KEY_RE   = re.compile(r'sk-[A-Za-z0-9._\-]+')
_EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')

_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)


# ── Exceptions ────────────────────────────────────────────────────────────────

class CodexHttpError(Exception):
    code: str = 'codex_http_error'


class SessionTokenMissing(CodexHttpError):
    """No session token configured or file absent."""
    code = 'chatgpt_session_token_missing'


class SessionTokenInvalid(CodexHttpError):
    """Server returned 401 — token is rotated/expired/wrong."""
    code = 'chatgpt_session_token_invalid'


class RateLimited(CodexHttpError):
    code = 'rate_limited'


class CodexUpstreamError(CodexHttpError):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(f'{code}{": " + detail if detail else ""}')
        self.code = code
        self.detail = detail


# ── Redaction ─────────────────────────────────────────────────────────────────

def redact(s: str) -> str:
    if not s:
        return s
    s = _KEY_RE.sub('sk-***', s)
    s = _EMAIL_RE.sub('***@***', s)
    return s


# ── Session token loading ─────────────────────────────────────────────────────

def _load_token_from_file(path: Path = SESSION_TOKEN_FILE) -> Optional[str]:
    if not path.exists():
        return None
    err = check_secret_file_perms(path)
    if err:
        raise SessionTokenMissing(err)
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError as e:
        raise SessionTokenMissing(f'{path}: {e}') from None


def _load_session_token() -> str:
    """Resolve ChatGPT session token. Precedence: process env > .env > file."""
    raw = get_env('CODEX_QUEUE_CHATGPT_SESSION_TOKEN')
    if raw and raw.strip():
        tok = raw.strip()
        if len(tok) < 20:
            raise SessionTokenMissing('CODEX_QUEUE_CHATGPT_SESSION_TOKEN is too short')
        return tok
    file_tok = _load_token_from_file()
    if file_tok:
        if len(file_tok) < 20:
            raise SessionTokenMissing(f'{SESSION_TOKEN_FILE}: token is too short')
        return file_tok
    raise SessionTokenMissing(
        'no CODEX_QUEUE_CHATGPT_SESSION_TOKEN in env, .env, or file at '
        f'{SESSION_TOKEN_FILE}. See docs/usage-checking.md for setup instructions.'
    )


# ── HTTP helpers ──────────────────────────────────────────────────────────────

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


def _get(url: str, session_token: str,
         bearer_token: Optional[str] = None):
    """Single GET with ChatGPT auth headers. Raises typed exceptions."""
    headers = {
        'Cookie':          f'__Secure-next-auth.session-token={session_token}',
        'Accept':          'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'User-Agent':      _USER_AGENT,
        'Referer':         'https://chatgpt.com/codex/cloud/settings/analytics',
        'Origin':          'https://chatgpt.com',
        'Sec-Fetch-Site':  'same-origin',
        'Sec-Fetch-Mode':  'cors',
        'Sec-Fetch-Dest':  'empty',
    }
    if bearer_token:
        headers['Authorization'] = f'Bearer {bearer_token}'

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
        raise CodexUpstreamError('network_error', e.__class__.__name__) from None

    body = _decode_body(raw, rheaders)
    if _detect_cloudflare_block(status, rheaders, body):
        raise CodexUpstreamError('cloudflare_blocked', f'http {status}')
    if status == 401:
        raise SessionTokenInvalid('http 401 — session token may be expired')
    if status == 429:
        raise RateLimited('http 429')
    if status >= 500:
        raise CodexUpstreamError('http_error', f'http {status}')
    if not (200 <= status < 300):
        raise CodexUpstreamError('http_error', f'http {status}')
    try:
        return json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CodexUpstreamError('bad_response', 'invalid json') from None


def _get_raw(url: str, session_token: str,
             bearer_token: Optional[str] = None) -> tuple[int, bytes, object]:
    """Single authenticated GET returning status/body for diagnostics.

    Caller is responsible for avoiding secret/body logging.
    """
    headers = {
        'Cookie':          f'__Secure-next-auth.session-token={session_token}',
        'Accept':          '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'User-Agent':      _USER_AGENT,
        'Referer':         _ANALYTICS_PAGE_URL,
        'Origin':          'https://chatgpt.com',
        'Sec-Fetch-Site':  'same-origin',
        'Sec-Fetch-Mode':  'cors',
        'Sec-Fetch-Dest':  'empty',
    }
    if bearer_token:
        headers['Authorization'] = f'Bearer {bearer_token}'
    req = Request(url, headers=headers, method='GET')
    try:
        resp = urlopen(req, timeout=REQUEST_TIMEOUT_S)
        return resp.status, _decode_body(resp.read(), resp.headers), resp.headers
    except HTTPError as e:
        return e.code, _decode_body(e.read() or b'', e.headers), e.headers


def discover_usage_urls(log_fn=print) -> list[str]:
    """Discover likely ChatGPT Codex usage/analytics endpoints.

    Fetches the authenticated analytics page plus same-origin JS chunks and
    returns same-origin URLs whose text mentions Codex usage/analytics paths.
    Response bodies and cookies are never logged.
    """
    session_token = _load_session_token()
    bearer_token = _get_bearer_token(session_token)
    seen_assets: set[str] = set()
    candidates: set[str] = set()

    def add_candidate(raw: str) -> None:
        if not raw:
            return
        url = urljoin('https://chatgpt.com', raw)
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != 'chatgpt.com':
            return
        if any(part in parsed.path.lower() for part in ('codex', 'usage', 'analytic')):
            candidates.add(url)

    page_status, page_body, _ = _get_raw(_ANALYTICS_PAGE_URL, session_token, bearer_token)
    log_fn(f'discover: analytics page status {page_status}')
    page_text = page_body.decode('utf-8', errors='ignore')

    for m in re.finditer(r'''(?:src|href)=["']([^"']+)["']''', page_text):
        url = urljoin(_ANALYTICS_PAGE_URL, m.group(1))
        parsed = urlparse(url)
        if parsed.scheme == 'https' and parsed.netloc == 'chatgpt.com':
            if parsed.path.endswith('.js') or '/_next/static/' in parsed.path:
                seen_assets.add(url)
        add_candidate(m.group(1))

    string_pat = re.compile(r'''["']((?:https://chatgpt\.com)?/[^"']*(?:codex|usage|analytic)[^"']*)["']''', re.I)
    for m in string_pat.finditer(page_text):
        add_candidate(m.group(1))

    for asset in sorted(seen_assets):
        try:
            status, body, _ = _get_raw(asset, session_token, bearer_token)
        except Exception:
            continue
        if status != 200:
            continue
        text = body.decode('utf-8', errors='ignore')
        if not re.search(r'codex|usage|analytic', text, re.I):
            continue
        for m in string_pat.finditer(text):
            add_candidate(m.group(1))

    return sorted(candidates)


def _get_bearer_token(session_token: str) -> Optional[str]:
    """Exchange the NextAuth session cookie for a short-lived bearer token.

    Returns None if the session endpoint does not include an accessToken — some
    plan types return user info only. In that case the caller falls back to
    cookie-only auth for the usage endpoint.
    """
    try:
        data = _get(_NEXTAUTH_SESSION_URL, session_token)
    except (CodexHttpError, Exception):
        return None
    if isinstance(data, dict):
        tok = data.get('accessToken')
        if isinstance(tok, str) and tok:
            return tok
    return None


# ── Parse usage response ──────────────────────────────────────────────────────

def _format_reset_str(reset_minutes: int) -> str:
    if reset_minutes <= 0:
        return '0min'
    hrs = reset_minutes // 60
    mins = reset_minutes % 60
    if hrs:
        return f'{hrs}hr {mins}min'
    return f'{mins}min'


def _parse_resets_at(value: str) -> datetime:
    if not isinstance(value, str):
        raise CodexUpstreamError('bad_response', 'resets_at not a string')
    s = value.replace('Z', '+00:00') if value.endswith('Z') else value
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise CodexUpstreamError('bad_response', 'resets_at unparseable') from None
    if dt.tzinfo is None:
        raise CodexUpstreamError('bad_response', 'resets_at lacks timezone')
    return dt


def _parse_usage(data, now_utc: datetime) -> tuple[int, int, str]:
    """Extract (pct, reset_minutes, reset_str) from the Codex usage API response.

    Handles the most likely response shapes:

      Shape A — utilization + resets_at (same schema as claude.ai):
        {"utilization": 0.71, "resets_at": "2025-01-01T12:00:00Z"}

      Shape B — used/total token counts:
        {"used_tokens": 71000, "total_tokens": 100000, "resets_at": "..."}

      Shape C — wrapper with a "codex" or "usage" sub-key:
        {"codex": {"utilization": 0.71, "resets_at": "..."}}
        {"usage": {"used_tokens": 71000, ...}}

      Shape D — current ChatGPT Codex WHAM rate-limit endpoint:
        {"rate_limit": {"primary_window": {
           "used_percent": 20,
           "reset_after_seconds": 15514,
           "reset_at": 1777786404
        }}}

    If the response shape doesn't match any known pattern, raises
    CodexUpstreamError('bad_response'). Check the Codex analytics page in
    browser DevTools → Network and set CODEX_QUEUE_CHATGPT_USAGE_URL if the
    endpoint has changed.
    """
    if not isinstance(data, dict):
        raise CodexUpstreamError('bad_response', 'response not a dict')

    # Unwrap common envelope keys
    for key in ('codex', 'usage', 'data'):
        if key in data and isinstance(data[key], dict):
            data = data[key]
            break

    # Shape D: WHAM rate-limit status
    rate_limit = data.get('rate_limit')
    if isinstance(rate_limit, dict):
        primary = rate_limit.get('primary_window')
        if isinstance(primary, dict):
            used_percent = primary.get('used_percent')
            if not isinstance(used_percent, (int, float)) or isinstance(used_percent, bool):
                raise CodexUpstreamError('bad_response', 'rate_limit used_percent not numeric')
            if math.isnan(used_percent) or math.isinf(used_percent):
                raise CodexUpstreamError('bad_response', 'rate_limit used_percent NaN/Inf')
            if used_percent < 0 or used_percent > 100:
                raise CodexUpstreamError(
                    'bad_response', f'rate_limit used_percent {used_percent} out of range')
            pct = int(math.ceil(used_percent))

            reset_after_seconds = primary.get('reset_after_seconds')
            if isinstance(reset_after_seconds, (int, float)) and not isinstance(reset_after_seconds, bool):
                reset_minutes = max(0, math.ceil(reset_after_seconds / 60))
                return pct, reset_minutes, _format_reset_str(reset_minutes)

            reset_at = primary.get('reset_at')
            if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
                delta_s = reset_at - now_utc.timestamp()
                if delta_s <= 0:
                    raise CodexUpstreamError('bad_response', 'rate_limit reset_at already in the past')
                reset_minutes = max(0, math.ceil(delta_s / 60))
                return pct, reset_minutes, _format_reset_str(reset_minutes)

            raise CodexUpstreamError(
                'bad_response',
                'rate_limit primary_window missing reset_after_seconds/reset_at',
            )

    # Shape A: utilization (0–100 float or 0–1 fraction) + resets_at
    util = data.get('utilization')
    if util is not None:
        if not isinstance(util, (int, float)) or isinstance(util, bool):
            raise CodexUpstreamError('bad_response', 'utilization not numeric')
        if math.isnan(util) or math.isinf(util):
            raise CodexUpstreamError('bad_response', 'utilization NaN/Inf')
        # Accept either 0–1 fraction or 0–100 percentage
        if 0 <= util <= 1:
            pct = int(math.ceil(util * 100))
        elif 1 < util <= 100:
            pct = int(math.ceil(util))
        else:
            raise CodexUpstreamError('bad_response', f'utilization {util} out of range')
        resets_at_raw = data.get('resets_at') or data.get('reset_at')
        if resets_at_raw is None:
            raise CodexUpstreamError('bad_response', 'resets_at missing')
        resets_at = _parse_resets_at(resets_at_raw)
        delta_s = (resets_at - now_utc).total_seconds()
        if delta_s <= 0:
            raise CodexUpstreamError('bad_response', 'resets_at already in the past')
        reset_minutes = max(0, math.ceil(delta_s / 60))
        return pct, reset_minutes, _format_reset_str(reset_minutes)

    # Shape B: used_tokens + total_tokens + resets_at
    used = data.get('used_tokens') or data.get('tokens_used')
    total = data.get('total_tokens') or data.get('token_limit') or data.get('limit_tokens')
    if used is not None and total is not None:
        if not isinstance(used, (int, float)) or not isinstance(total, (int, float)):
            raise CodexUpstreamError('bad_response', 'token counts not numeric')
        if total <= 0:
            raise CodexUpstreamError('bad_response', 'total_tokens is zero or negative')
        pct = int(math.ceil(used / total * 100))
        pct = max(0, min(100, pct))
        resets_at_raw = data.get('resets_at') or data.get('reset_at') or data.get('refills_at')
        if resets_at_raw is None:
            raise CodexUpstreamError('bad_response', 'resets_at missing')
        resets_at = _parse_resets_at(resets_at_raw)
        delta_s = (resets_at - now_utc).total_seconds()
        if delta_s <= 0:
            raise CodexUpstreamError('bad_response', 'resets_at already in the past')
        reset_minutes = max(0, math.ceil(delta_s / 60))
        return pct, reset_minutes, _format_reset_str(reset_minutes)

    raise CodexUpstreamError(
        'bad_response',
        'could not find usage fields (utilization/used_tokens/total_tokens). '
        'Check CODEX_QUEUE_CHATGPT_USAGE_URL — the API endpoint may have changed.'
    )


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_usage_codex_http(log_fn=lambda _msg: None):
    """Fetch Codex cloud usage via HTTP. Returns a UsageCheckResult.

    Auth flow:
      1. Load session token from env/file.
      2. Exchange for a bearer token via the NextAuth session endpoint
         (optional — falls back to cookie-only if unavailable).
      3. GET the Codex usage endpoint with up to MAX_ATTEMPTS retries.
    """
    from .usage_check import decide, UsageCheckResult

    session_token = _load_session_token()

    usage_url = (get_env('CODEX_QUEUE_CHATGPT_USAGE_URL') or '').strip() or _DEFAULT_USAGE_URL
    log_fn(f'codex-http: fetching usage from {usage_url}')

    # Exchange session cookie for a bearer token (best-effort).
    bearer_token = _get_bearer_token(session_token)
    if bearer_token:
        log_fn('codex-http: bearer token obtained from NextAuth session')
    else:
        log_fn('codex-http: using cookie-only auth (no bearer token)')

    last_exc: Optional[BaseException] = None
    attempt = 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            data = _get(usage_url, session_token, bearer_token)
            now_utc = datetime.now(timezone.utc)
            pct, reset_minutes, reset_str = _parse_usage(data, now_utc)
            status = decide(pct, reset_minutes)
            return UsageCheckResult(
                pct=pct,
                reset_minutes=reset_minutes,
                reset_str=reset_str,
                status=status,
                finished_at=time.time(),
                backend='codex_http',
            )
        except SessionTokenInvalid:
            raise
        except RateLimited as e:
            last_exc = e
            sleep_s = BACKOFF_BASE_S ** attempt
            log_fn(f'codex-http: rate-limited (attempt {attempt}/{MAX_ATTEMPTS}), '
                   f'sleeping {sleep_s:.1f}s')
            if attempt < MAX_ATTEMPTS:
                time.sleep(sleep_s)
        except CodexUpstreamError as e:
            last_exc = e
            if e.code in ('cloudflare_blocked', 'bad_response'):
                raise
            sleep_s = BACKOFF_BASE_S ** attempt
            log_fn(f'codex-http: {e.code} (attempt {attempt}/{MAX_ATTEMPTS}), '
                   f'sleeping {sleep_s:.1f}s')
            if attempt < MAX_ATTEMPTS:
                time.sleep(sleep_s)

    if last_exc:
        raise last_exc
    raise CodexUpstreamError('http_error', 'retries exhausted')
