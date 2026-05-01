"""Password-based session auth for queue-worker web.

Config (env vars):
  QUEUE_WORKER_PASSWORD        Shared password. Unset/empty → auth disabled.
  QUEUE_WORKER_COOKIE_SECURE   '1' (default) to require HTTPS for the session
                               cookie. Set '0' for local HTTP dev.

Protects against:
  - Random internet traffic finding the public URL
  - Credential-stuffing / brute force (per-IP lockout)
  - IP rotation attacks (global rate limit)

Does NOT protect against:
  - A compromised client machine
  - An attacker who obtains the password
  - A targeted attacker already past the tunnel
"""

import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import Request

from .config import get_env

# ── Config ────────────────────────────────────────────────────────────────────

# Read through get_env so .env values reach auth.py without being exported
# into os.environ (which would propagate to claude -p subprocesses).
_PASSWORD = get_env('QUEUE_WORKER_PASSWORD') or ''
_PASSWORD_BYTES = _PASSWORD.encode('utf-8')  # compare_digest requires bytes for non-ASCII
COOKIE_SECURE = (get_env('QUEUE_WORKER_COOKIE_SECURE') or '1') == '1'
COOKIE_NAME = 'qw_session'
SESSION_TTL_SEC = 7 * 86400

LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION_SEC = 600

# Global rate limit — defends against rotating-IP attacks. A client moving
# across many IPs still contributes to the shared counter.
GLOBAL_FAILURE_WINDOW_SEC = 60
GLOBAL_FAILURE_THRESHOLD  = 50
GLOBAL_LOCKOUT_DURATION_SEC = 600

_MAX_FAILURE_ENTRIES = 10_000   # hard cap to prevent memory exhaustion


# ── State ─────────────────────────────────────────────────────────────────────

class AuthReason(str, Enum):
    OK = 'ok'
    WRONG = 'wrong'
    IP_LOCKED = 'ip_locked'
    GLOBAL_LOCKED = 'global_locked'


@dataclass
class FailureRecord:
    count: int = 0
    locked_until: float = 0.0
    last_attempt_at: float = 0.0


@dataclass
class AuthResult:
    success: bool
    retry_after: int = 0
    remaining_attempts: Optional[int] = None
    reason: AuthReason = AuthReason.OK


_sessions: dict[str, float] = {}           # token → expiry epoch
_sessions_lock = threading.Lock()

_failures: dict[str, FailureRecord] = {}   # client_ip → record
_failures_lock = threading.Lock()

_global_failures: deque[float] = deque()   # timestamps within rolling window
_global_locked_until: float = 0.0
_global_lock = threading.Lock()


# ── Public helpers ────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Auth is disabled (pass-through) when the env var is unset or empty."""
    return bool(_PASSWORD)


def extract_client_ip(request: Request) -> str:
    """Return the best-guess client IP.

    The web server only ever talks to clients through either (a) the
    cloudflared tunnel bound to localhost, or (b) direct localhost access.
    Trust ``CF-Connecting-IP`` only when the peer is localhost — this
    prevents an attacker with a different network path from spoofing it.
    """
    peer = request.client.host if request.client else '0.0.0.0'
    if peer in ('127.0.0.1', '::1') and 'cf-connecting-ip' in request.headers:
        return request.headers['cf-connecting-ip'].strip() or peer
    return peer


def check_password_attempt(client_ip: str, submitted: str) -> AuthResult:
    """Validate a login attempt and update rate-limit state.

    Order: global-lock check → (per-IP lock check + password compare + mutate)
    atomically under _failures_lock → global counter tally. Keeping the per-IP
    check, compare, and mutation under one lock prevents a concurrent attempt
    from slipping past the lock check and incrementing the counter during an
    active lockout window.
    """
    if not is_enabled():
        return AuthResult(success=True, reason=AuthReason.OK)

    now = time.time()

    with _global_lock:
        if now < _global_locked_until:
            return AuthResult(success=False,
                              retry_after=int(_global_locked_until - now),
                              reason=AuthReason.GLOBAL_LOCKED)

    submitted_bytes = (submitted or '').encode('utf-8')

    triggered_ip_lock = False
    remaining_for_ip = 0
    with _failures_lock:
        rec = _failures.get(client_ip)
        if rec and now < rec.locked_until:
            return AuthResult(success=False,
                              retry_after=int(rec.locked_until - now),
                              reason=AuthReason.IP_LOCKED)

        if hmac.compare_digest(_PASSWORD_BYTES, submitted_bytes):
            _failures.pop(client_ip, None)
            return AuthResult(success=True, reason=AuthReason.OK)

        if rec is None:
            rec = FailureRecord()
            _failures[client_ip] = rec
        rec.count += 1
        rec.last_attempt_at = now
        if rec.count >= LOCKOUT_THRESHOLD:
            rec.locked_until = now + LOCKOUT_DURATION_SEC
            rec.count = 0
            triggered_ip_lock = True
        remaining_for_ip = LOCKOUT_THRESHOLD - rec.count
        _prune_failures_locked()

    triggered_global_lock = _record_global_failure(now)

    if triggered_global_lock:
        return AuthResult(success=False, retry_after=GLOBAL_LOCKOUT_DURATION_SEC,
                          reason=AuthReason.GLOBAL_LOCKED)
    if triggered_ip_lock:
        return AuthResult(success=False, retry_after=LOCKOUT_DURATION_SEC,
                          reason=AuthReason.IP_LOCKED)
    return AuthResult(success=False, remaining_attempts=remaining_for_ip,
                      reason=AuthReason.WRONG)


def create_session() -> tuple[str, int]:
    """Return (opaque_token, max_age_seconds). Prunes expired sessions."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    expiry = now + SESSION_TTL_SEC
    with _sessions_lock:
        _sessions[token] = expiry
        dead = [k for k, exp in _sessions.items() if exp < now]
        for k in dead:
            _sessions.pop(k, None)
    return token, SESSION_TTL_SEC


def is_session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if time.time() >= exp:
            _sessions.pop(token, None)
            return False
        return True


def invalidate_session(token: Optional[str]) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


# ── Internal ──────────────────────────────────────────────────────────────────

def _prune_failures_locked() -> None:
    """Evict oldest failure records when the dict exceeds the hard cap.

    Caller must hold _failures_lock. Removes the oldest quarter so we don't
    run the prune on every failure.
    """
    if len(_failures) <= _MAX_FAILURE_ENTRIES:
        return
    evict_count = _MAX_FAILURE_ENTRIES // 4
    oldest = sorted(_failures.items(), key=lambda kv: kv[1].last_attempt_at)
    for k, _ in oldest[:evict_count]:
        _failures.pop(k, None)


def _record_global_failure(now: float) -> bool:
    """Append to the global failure log. Returns True if this trip engaged
    the global lockout. No-ops (returns False) if the lockout is already
    active — we don't want concurrent attempts to extend the fixed window.
    """
    global _global_locked_until
    with _global_lock:
        if now < _global_locked_until:
            return False
        cutoff = now - GLOBAL_FAILURE_WINDOW_SEC
        while _global_failures and _global_failures[0] < cutoff:
            _global_failures.popleft()
        _global_failures.append(now)
        if len(_global_failures) >= GLOBAL_FAILURE_THRESHOLD:
            _global_locked_until = now + GLOBAL_LOCKOUT_DURATION_SEC
            _global_failures.clear()
            return True
        return False
