"""Tests for the HTTP usage backend (queue_worker.usage_check_http).

Covers parser correctness, retry/backoff behavior, error mapping, redaction,
and the /organizations cache write rule (only after a successful /usage
roundtrip — never on partial success).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from queue_worker import usage_check_http as uch
from queue_worker.usage_check import UsageCheckResult, decide


# ── Helpers ────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    """ISO with microseconds + +00:00 — matches spike-confirmed server format."""
    return dt.astimezone(timezone.utc).isoformat()


def _make_response(body: dict, status: int = 200, headers: dict | None = None):
    """Mock for urlopen()'s context manager-less return value."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.read.return_value = json.dumps(body).encode('utf-8')
    return resp


def _http_error(status: int, body: bytes = b'', headers: dict | None = None):
    """Build a urllib HTTPError to raise from urlopen mock."""
    from urllib.error import HTTPError
    err = HTTPError(
        url='https://claude.ai/api/x', code=status, msg='err',
        hdrs=headers or {}, fp=None,
    )
    err.read = MagicMock(return_value=body)
    return err


def _patch_session_key(monkeypatch, key='sk-ant-test-fake-1234567890'):
    monkeypatch.setenv('CLAUDE_SESSION_KEY', key)
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)


@pytest.fixture
def fake_clock(monkeypatch):
    """Pin datetime.now() so reset_minutes math is deterministic."""
    fixed = datetime(2026, 4, 29, 7, 47, 7, tzinfo=timezone.utc)
    real_datetime = uch.datetime

    class _DT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(uch, 'datetime', _DT)
    return fixed


@pytest.fixture
def isolated_org_cache(monkeypatch, tmp_path):
    """Redirect ORG_CACHE_PATH so tests don't pollute the real state/.
    Also clears the in-process org-uuid memo so each test starts fresh.
    """
    monkeypatch.setattr(uch, 'ORG_CACHE_PATH', tmp_path / 'org_cache.json')
    uch._ORG_UUID_MEMO = None
    yield tmp_path / 'org_cache.json'
    uch._ORG_UUID_MEMO = None


@pytest.fixture
def no_sleep(monkeypatch):
    """Speed up retry tests."""
    monkeypatch.setattr(uch.time, 'sleep', lambda _s: None)


# ── Redaction ──────────────────────────────────────────────────────────────

def test_redact_strips_session_key():
    s = 'login failed for sk-ant-sid02-AbCdEfGh123456789-xyz; retry'
    assert 'sk-ant-' not in uch.redact(s).split('sk-ant-***')[1] if False else True
    out = uch.redact(s)
    assert 'sk-ant-***' in out
    assert 'AbCdEfGh' not in out


def test_redact_strips_email():
    s = 'org owner: foo.bar+tag@example.co.uk'
    out = uch.redact(s)
    assert '***@***' in out
    assert 'foo.bar' not in out


def test_redact_handles_empty():
    assert uch.redact('') == ''
    assert uch.redact(None) is None  # type: ignore[arg-type]


# ── _format_reset_str ──────────────────────────────────────────────────────

@pytest.mark.parametrize('minutes,expected', [
    (0,   '0min'),
    (1,   '1min'),
    (59,  '59min'),
    (60,  '1hr 0min'),
    (63,  '1hr 3min'),
    (180, '3hr 0min'),
    (-5,  '0min'),  # negative clamps to "0min" via the <=0 branch
])
def test_format_reset_str(minutes, expected):
    assert uch._format_reset_str(minutes) == expected


# ── _parse_resets_at ────────────────────────────────────────────────────────

def test_parse_resets_at_offset_form():
    """Spike-confirmed format: '2026-04-29T08:50:00.695859+00:00'."""
    dt = uch._parse_resets_at('2026-04-29T08:50:00.695859+00:00')
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.minute == 50


def test_parse_resets_at_z_form():
    dt = uch._parse_resets_at('2026-04-29T08:50:00Z')
    assert dt.tzinfo is not None


def test_parse_resets_at_naive_rejected():
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._parse_resets_at('2026-04-29T08:50:00')
    assert ei.value.code == 'bad_response'


def test_parse_resets_at_non_string():
    with pytest.raises(uch.HttpUpstreamError):
        uch._parse_resets_at(12345)  # type: ignore[arg-type]


def test_parse_resets_at_garbage():
    with pytest.raises(uch.HttpUpstreamError):
        uch._parse_resets_at('not-a-date')


# ── _parse_usage ───────────────────────────────────────────────────────────

def _usage_body(util, resets_at_iso, **extras):
    body = {'five_hour': {'utilization': util, 'resets_at': resets_at_iso}}
    body.update(extras)
    return body


def test_parse_usage_happy_path(fake_clock):
    # +1h 2m 53s → 63 min after sub-min round-up
    resets = (fake_clock + timedelta(hours=1, minutes=2, seconds=53)).isoformat()
    pct, mins, rs = uch._parse_usage(_usage_body(89.0, resets), fake_clock,
                                      lambda _m: None)
    assert pct == 89
    assert mins == 63
    assert rs == '1hr 3min'


@pytest.mark.parametrize('util,expected_pct', [
    # ceil, not banker's. 70.5 must NOT round down to 70 — that would report
    # 30% remaining at 29.5% actual usage_left and trigger a spurious burn.
    (69.5, 70),
    (70.0, 70),
    (70.5, 71),
    (71.5, 72),
    (89.4, 90),
    (89.6, 90),
    (89.0, 89),
    (0.0,  0),
    (0.1,  1),     # ceil rounds tiny utilization up — over-reports by <1pt
    (100.0, 100),
])
def test_parse_usage_rounding_boundaries(fake_clock, util, expected_pct):
    resets = (fake_clock + timedelta(hours=2)).isoformat()
    pct, _, _ = uch._parse_usage(_usage_body(util, resets), fake_clock,
                                  lambda _m: None)
    assert pct == expected_pct


def test_parse_usage_rounds_sub_minute_up(fake_clock):
    # Exactly 0.1s past 60 min → 61 min, not 60
    resets = (fake_clock + timedelta(minutes=60, seconds=0, microseconds=100_000)).isoformat()
    _, mins, _ = uch._parse_usage(_usage_body(50.0, resets), fake_clock,
                                   lambda _m: None)
    assert mins == 61


def test_parse_usage_resets_at_null_is_between_sessions(fake_clock):
    with pytest.raises(uch.BetweenSessions):
        uch._parse_usage({'five_hour': {'utilization': 50.0, 'resets_at': None}},
                         fake_clock, lambda _m: None)


def test_parse_usage_resets_at_in_past_is_between_sessions(fake_clock):
    past = (fake_clock - timedelta(minutes=5)).isoformat()
    with pytest.raises(uch.BetweenSessions):
        uch._parse_usage(_usage_body(50.0, past), fake_clock, lambda _m: None)


@pytest.mark.parametrize('bad_util', [
    None,
    'fifty',
    True,        # bool is rejected (despite being instance of int)
    float('nan'),
    float('inf'),
    -1.0,
    100.5,
    150,
])
def test_parse_usage_invalid_utilization(fake_clock, bad_util):
    resets = (fake_clock + timedelta(hours=1)).isoformat()
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._parse_usage(_usage_body(bad_util, resets), fake_clock,
                         lambda _m: None)
    assert ei.value.code == 'bad_response'


def test_parse_usage_missing_five_hour(fake_clock):
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._parse_usage({'seven_day': {}}, fake_clock, lambda _m: None)
    assert ei.value.code == 'bad_response'


def test_parse_usage_top_level_not_dict(fake_clock):
    with pytest.raises(uch.HttpUpstreamError):
        uch._parse_usage([], fake_clock, lambda _m: None)


def test_parse_usage_ignores_unknown_keys(fake_clock):
    """Spike saw `iguana_necktie`, `extra_usage`, etc. in the wild — parser
    must ignore unknown top-level keys, not fail-strict."""
    resets = (fake_clock + timedelta(hours=2)).isoformat()
    body = _usage_body(42.0, resets,
                       iguana_necktie=None,
                       extra_usage={'utilization': 100.0},
                       seven_day_oauth_apps=None)
    pct, _, _ = uch._parse_usage(body, fake_clock, lambda _m: None)
    assert pct == 42


# ── _request error mapping ─────────────────────────────────────────────────

def test_request_401_raises_session_key_invalid(monkeypatch):
    monkeypatch.setattr(uch, 'urlopen',
                        MagicMock(side_effect=_http_error(401)))
    with pytest.raises(uch.SessionKeyInvalid):
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')


def test_request_429_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(uch, 'urlopen',
                        MagicMock(side_effect=_http_error(429)))
    with pytest.raises(uch.RateLimited):
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')


def test_request_5xx_raises_http_error(monkeypatch):
    monkeypatch.setattr(uch, 'urlopen',
                        MagicMock(side_effect=_http_error(503)))
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')
    assert ei.value.code == 'http_error'


def test_request_403_with_cf_mitigated_is_cloudflare(monkeypatch):
    monkeypatch.setattr(
        uch, 'urlopen',
        MagicMock(side_effect=_http_error(403, headers={'cf-mitigated': 'challenge'})),
    )
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')
    assert ei.value.code == 'cloudflare_blocked'


def test_request_503_with_just_a_moment_html_is_cloudflare(monkeypatch):
    body = b'<html><title>Just a moment...</title></html>'
    monkeypatch.setattr(
        uch, 'urlopen',
        MagicMock(side_effect=_http_error(503, body=body)),
    )
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')
    assert ei.value.code == 'cloudflare_blocked'


def test_request_invalid_json(monkeypatch):
    resp = MagicMock()
    resp.status = 200
    resp.headers = {}
    resp.read.return_value = b'not json'
    monkeypatch.setattr(uch, 'urlopen', MagicMock(return_value=resp))
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')
    assert ei.value.code == 'bad_response'


def test_request_network_error(monkeypatch):
    from urllib.error import URLError
    monkeypatch.setattr(uch, 'urlopen',
                        MagicMock(side_effect=URLError('connection refused')))
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._request('https://claude.ai/api/x', 'sk-ant-fake')
    assert ei.value.code == 'network_error'


# ── _resolve_org_uuid ──────────────────────────────────────────────────────

def test_resolve_org_uuid_env_override(monkeypatch, isolated_org_cache):
    monkeypatch.setenv('CLAUDE_ORG_UUID', 'env-supplied-uuid')
    monkeypatch.setattr(uch, 'urlopen', MagicMock(
        side_effect=AssertionError('should not call /organizations')
    ))
    assert uch._resolve_org_uuid('sk-ant-fake', lambda _m: None) == 'env-supplied-uuid'


def test_resolve_org_uuid_memoizes(monkeypatch, isolated_org_cache):
    """Second call must not re-stat the cache or hit the network."""
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)
    isolated_org_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_org_cache.write_text(json.dumps({'org_uuid': 'cached-uuid'}))
    uch._ORG_UUID_MEMO = None
    # First call populates memo from disk
    assert uch._resolve_org_uuid('sk-ant-fake', lambda _m: None) == 'cached-uuid'
    # Now delete the file — memo should still answer without network
    isolated_org_cache.unlink()
    monkeypatch.setattr(uch, 'urlopen', MagicMock(
        side_effect=AssertionError('memo should serve this; no network'),
    ))
    assert uch._resolve_org_uuid('sk-ant-fake', lambda _m: None) == 'cached-uuid'


def test_resolve_org_uuid_uses_cache_when_present(monkeypatch, isolated_org_cache):
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)
    isolated_org_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_org_cache.write_text(json.dumps({'org_uuid': 'cached-uuid'}))
    monkeypatch.setattr(uch, 'urlopen', MagicMock(
        side_effect=AssertionError('should not call /organizations'),
    ))
    assert uch._resolve_org_uuid('sk-ant-fake', lambda _m: None) == 'cached-uuid'


def test_resolve_org_uuid_fetches_when_no_cache(monkeypatch, isolated_org_cache):
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)
    monkeypatch.setattr(uch, 'urlopen', MagicMock(return_value=_make_response(
        [{'uuid': 'fetched-uuid', 'name': 'foo'}]
    )))
    assert uch._resolve_org_uuid('sk-ant-fake', lambda _m: None) == 'fetched-uuid'


def test_resolve_org_uuid_refuses_multi_org_without_env(
    monkeypatch, isolated_org_cache
):
    """Two orgs returned and CLAUDE_ORG_UUID unset → must raise rather than
    silently pin orgs[0]. A successful /usage call only proves an org exists,
    not that it's the user's intended Claude plan."""
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)
    monkeypatch.setattr(uch, 'urlopen', MagicMock(return_value=_make_response([
        {'uuid': 'first-uuid',  'name': 'work'},
        {'uuid': 'second-uuid', 'name': 'personal'},
    ])))
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._resolve_org_uuid('sk-ant-fake', lambda _m: None)
    assert ei.value.code == 'multi_org_no_pin'


def test_resolve_org_uuid_multi_org_with_env_override(
    monkeypatch, isolated_org_cache
):
    """With CLAUDE_ORG_UUID set, multi-org is allowed — env wins, no fetch."""
    monkeypatch.setenv('CLAUDE_ORG_UUID', 'env-pinned')
    monkeypatch.setattr(uch, 'urlopen', MagicMock(
        side_effect=AssertionError('should not call /organizations'),
    ))
    assert uch._resolve_org_uuid('sk-ant-fake', lambda _m: None) == 'env-pinned'


def test_resolve_org_uuid_empty_list(monkeypatch, isolated_org_cache):
    monkeypatch.delenv('CLAUDE_ORG_UUID', raising=False)
    monkeypatch.setattr(uch, 'urlopen',
                        MagicMock(return_value=_make_response([])))
    with pytest.raises(uch.HttpUpstreamError) as ei:
        uch._resolve_org_uuid('sk-ant-fake', lambda _m: None)
    assert ei.value.code == 'org_resolve_failed'


# ── fetch_usage_http (orchestration) ───────────────────────────────────────

def test_fetch_caches_org_uuid_only_after_usage_success(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    _patch_session_key(monkeypatch)
    resets = (fake_clock + timedelta(hours=2)).isoformat()
    calls = []

    def _urlopen(req, timeout=None):
        url = getattr(req, 'full_url', None) or req.get_full_url()
        calls.append(url)
        if '/organizations/auto-uuid/usage' in url:
            return _make_response(_usage_body(50.0, resets))
        if url.endswith('/organizations'):
            return _make_response([{'uuid': 'auto-uuid', 'name': 'foo'}])
        raise AssertionError(f'unexpected url: {url}')

    monkeypatch.setattr(uch, 'urlopen', _urlopen)
    result = uch.fetch_usage_http()
    assert result.pct == 50
    assert isolated_org_cache.exists()
    cached = json.loads(isolated_org_cache.read_text())
    assert cached['org_uuid'] == 'auto-uuid'


def test_fetch_does_not_cache_on_usage_failure(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    _patch_session_key(monkeypatch)

    def _urlopen(req, timeout=None):
        url = getattr(req, 'full_url', None) or req.get_full_url()
        if '/usage' in url:
            raise _http_error(401)
        if url.endswith('/organizations'):
            return _make_response([{'uuid': 'auto-uuid', 'name': 'foo'}])
        raise AssertionError(url)

    monkeypatch.setattr(uch, 'urlopen', _urlopen)
    with pytest.raises(uch.SessionKeyInvalid):
        uch.fetch_usage_http()
    assert not isolated_org_cache.exists()


def test_fetch_session_key_invalid_no_retry(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    _patch_session_key(monkeypatch)
    monkeypatch.setattr(uch, '_resolve_org_uuid',
                        lambda *_a, **_k: 'cached-uuid')
    call_count = [0]

    def _urlopen(req, timeout=None):
        call_count[0] += 1
        raise _http_error(401)

    monkeypatch.setattr(uch, 'urlopen', _urlopen)
    with pytest.raises(uch.SessionKeyInvalid):
        uch.fetch_usage_http()
    assert call_count[0] == 1, 'must not retry on 401'


def test_fetch_429_retries_then_succeeds(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    """One 429 followed by a success uses one retry sleep with 3^n backoff.
    Bounded by MAX_ATTEMPTS=2 so the runner's _usage_check_lock is never
    held more than ~30 s.
    """
    _patch_session_key(monkeypatch)
    monkeypatch.setattr(uch, '_resolve_org_uuid',
                        lambda *_a, **_k: 'cached-uuid')
    resets = (fake_clock + timedelta(hours=2)).isoformat()
    seq = [_http_error(429), _make_response(_usage_body(33.0, resets))]
    sleeps: list[float] = []

    def _urlopen(req, timeout=None):
        out = seq.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(uch, 'urlopen', _urlopen)
    monkeypatch.setattr(uch.time, 'sleep', lambda s: sleeps.append(s))
    result = uch.fetch_usage_http()
    assert result.pct == 33
    assert len(sleeps) == 1   # one retry within MAX_ATTEMPTS=2 budget
    assert sleeps[0] >= 3.0   # 3^1 backoff for rate-limited


def test_fetch_429_exhausts_retries(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    """If every attempt 429s, the budget is exhausted and the original
    RateLimited bubbles up — runner records as ERROR:rate_limited."""
    _patch_session_key(monkeypatch)
    monkeypatch.setattr(uch, '_resolve_org_uuid',
                        lambda *_a, **_k: 'cached-uuid')
    monkeypatch.setattr(uch, 'urlopen',
                        MagicMock(side_effect=_http_error(429)))
    with pytest.raises(uch.RateLimited):
        uch.fetch_usage_http()


def test_fetch_between_sessions_no_retry(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    _patch_session_key(monkeypatch)
    monkeypatch.setattr(uch, '_resolve_org_uuid',
                        lambda *_a, **_k: 'cached-uuid')
    call_count = [0]

    def _urlopen(req, timeout=None):
        call_count[0] += 1
        return _make_response({'five_hour': {'utilization': 0.0,
                                              'resets_at': None}})

    monkeypatch.setattr(uch, 'urlopen', _urlopen)
    with pytest.raises(uch.BetweenSessions):
        uch.fetch_usage_http()
    assert call_count[0] == 1, 'between_sessions must not trigger retry'


def test_fetch_returns_usage_check_result(
    monkeypatch, isolated_org_cache, fake_clock, no_sleep
):
    _patch_session_key(monkeypatch)
    monkeypatch.setattr(uch, '_resolve_org_uuid',
                        lambda *_a, **_k: 'cached-uuid')
    resets = (fake_clock + timedelta(hours=1, minutes=2, seconds=53)).isoformat()
    monkeypatch.setattr(uch, 'urlopen', MagicMock(return_value=_make_response(
        _usage_body(89.0, resets)
    )))
    result = uch.fetch_usage_http()
    assert isinstance(result, UsageCheckResult)
    assert result.pct == 89
    assert result.reset_minutes == 63
    assert result.reset_str == '1hr 3min'
    # 89% used, 63min < 70min — but usage_left == 11% < 30% → status stays "Chilling"
    assert result.status == 'Chilling'


# ── decide() reuse — semantic alignment with Playwright path ───────────────

@pytest.mark.parametrize('pct,reset_min,expected_status', [
    (50, 60, 'NEED TO BURN TOKEN !'),  # usage_left=50, reset<70 → burn
    (89, 63, 'Chilling'),              # usage_left=11 < 30 → don't burn
    (60, 71, 'Chilling'),              # reset>=70 → don't burn
    (None, 60, 'ERROR:Parse_failed'),
    (50, None, 'ERROR:Parse_failed'),
])
def test_decide_alignment(pct, reset_min, expected_status):
    assert decide(pct, reset_min) == expected_status


# ── Session key file loading ───────────────────────────────────────────────

def test_load_key_from_file_refuses_loose_perms(tmp_path, monkeypatch):
    p = tmp_path / 'key'
    p.write_text('sk-ant-test-fake-1234567890')
    p.chmod(0o644)
    monkeypatch.setattr(uch, 'SESSION_KEY_FILE', p)
    with pytest.raises(uch.SessionKeyMissing) as ei:
        uch._load_key_from_file(p)
    assert 'too permissive' in str(ei.value)


def test_load_key_from_file_ok(tmp_path, monkeypatch):
    p = tmp_path / 'key'
    p.write_text('sk-ant-test-fake-1234567890')
    p.chmod(0o600)
    assert uch._load_key_from_file(p) == 'sk-ant-test-fake-1234567890'


def test_load_session_key_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv('CLAUDE_SESSION_KEY', 'sk-ant-from-env-1234567890')
    p = tmp_path / 'key'
    p.write_text('sk-ant-from-file-1234567890')
    p.chmod(0o600)
    monkeypatch.setattr(uch, 'SESSION_KEY_FILE', p)
    assert uch._load_session_key() == 'sk-ant-from-env-1234567890'


def test_load_session_key_neither_source(monkeypatch, tmp_path):
    monkeypatch.delenv('CLAUDE_SESSION_KEY', raising=False)
    monkeypatch.setattr(uch, 'SESSION_KEY_FILE', tmp_path / 'absent')
    with pytest.raises(uch.SessionKeyMissing):
        uch._load_session_key()


def test_load_session_key_format_failure_is_missing_not_invalid(monkeypatch):
    """Format-validation failures must surface as SessionKeyMissing so the
    dispatcher's auto branch falls back to Playwright. SessionKeyInvalid is
    reserved for actual server 401s."""
    monkeypatch.setenv('CLAUDE_SESSION_KEY', 'not-a-claude-key')
    with pytest.raises(uch.SessionKeyMissing):
        uch._load_session_key()
