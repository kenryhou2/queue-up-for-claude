"""Tests for parse_usage_text — the Playwright-path scraper parser.

Pins the contract that we always report the CURRENT SESSION reset time,
never the weekly limit's reset time. The page renders both, and Anthropic
has changed weekly's format in the past ("Resets Wed 8:00 PM" today, but
nothing prevents them from going back to "Resets in N days" tomorrow).
A leak between sections would silently corrupt the burn decision.
"""

from __future__ import annotations

import pytest

from queue_worker.usage_check import parse_usage_text


# ── Real-page text fixtures (transcribed from saved screenshots) ──────────

# Active session — the happy path. Current session has its own "Resets in",
# weekly has the older "Resets Wed 8:00 PM" format.
ACTIVE_PAGE = """\
Settings
Plan usage limits Max (5x)
Current session
Resets in 2 hr 17 min
42% used

Weekly limits
Learn more about usage limits
All models
Resets Wed 8:00 PM
11% used
Sonnet only
You haven't used Sonnet yet
0% used
Claude Design
You haven't used Claude Design yet
0% used

Last updated: just now
"""

# Between-sessions placeholder — current session has no reset, just the
# "Starts when a message is sent" copy. This is what triggers the kick path.
BETWEEN_SESSIONS_PAGE = """\
Settings
Plan usage limits Max (5x)
Current session
Starts when a message is sent
0% used

Weekly limits
Learn more about usage limits
All models
Resets Wed 8:00 PM
8% used
Sonnet only
You haven't used Sonnet yet
0% used

Last updated: less than a minute ago
"""

# Hypothetical: Anthropic flips weekly back to "Resets in N days" format.
# The session bound MUST keep weekly's reset out of the session reading.
HYPOTHETICAL_WEEKLY_RESETS_IN = """\
Settings
Plan usage limits Max (5x)
Current session
Resets in 1 hr 5 min
89% used

Weekly limits
All models
Resets in 3 days
73% used
"""

# Edge: section header missing entirely (page layout broke). Parser should
# still bound itself somehow — sanity bound on reset_minutes catches it.
NO_SECTION_HEADER = """\
Settings
Current session
Resets in 1 hr 5 min
89% used
All models
Resets in 6 days
73% used
"""


# ── Tests ──────────────────────────────────────────────────────────────────

def test_active_session_parses_correctly():
    pct, reset_str, reset_min = parse_usage_text(ACTIVE_PAGE)
    assert pct == 42
    assert reset_str == '2hr 17min'
    assert reset_min == 137  # 2*60 + 17


def test_between_sessions_returns_pct_zero_no_reset():
    """The unstarted session shows '0% used' as legitimate text — pct is 0,
    not None — but no reset is parseable. The dispatcher uses absence of
    reset_min (not pct) to detect this state."""
    pct, reset_str, reset_min = parse_usage_text(BETWEEN_SESSIONS_PAGE)
    assert pct == 0
    assert reset_str is None
    assert reset_min is None


def test_weekly_reset_in_format_does_not_leak_into_session():
    """Critical: even if Anthropic flips weekly to 'Resets in N days', we
    must keep reading the current session's reset, not weekly's. The
    bounded section between 'Current session' and 'Weekly limits' makes
    the regex match only the session block."""
    pct, reset_str, reset_min = parse_usage_text(HYPOTHETICAL_WEEKLY_RESETS_IN)
    assert pct == 89
    assert reset_min == 65  # 1 hr 5 min from CURRENT SESSION, not 3 days from weekly
    assert '3 days' not in (reset_str or '')


def test_implausible_session_reset_dropped():
    """Without the section header, the regex could pick up '6 days' from a
    weekly-style reset (= 8640 min). The sanity bound (≤ 5h+10min) drops it
    rather than report a wildly-wrong value."""
    pct, reset_str, reset_min = parse_usage_text(NO_SECTION_HEADER)
    assert pct == 89
    # Either we picked up the legitimate session "1 hr 5 min" (= 65 min) OR
    # we tripped the sanity bound and got None. Both are correct outcomes;
    # what we MUST never have is "6 days" leaking through.
    if reset_min is not None:
        assert reset_min == 65
    assert '6 days' not in (reset_str or '')


def test_no_current_session_block_returns_none():
    """If 'Current session' isn't on the page at all (login redirect, error
    page), bail out with all-None — caller will treat as parse failure."""
    pct, reset_str, reset_min = parse_usage_text('not the usage page at all')
    assert pct is None
    assert reset_str is None
    assert reset_min is None


def test_sub_minute_residue_rounds_up():
    """Original semantic: any seconds residue rounds reset_minutes UP so
    the 'imminent reset' signal stays non-zero (decide() compares < 70min)."""
    text = """\
Plan usage limits Max (5x)
Current session
Resets in 0 hr 0 min 30 sec
99% used

Weekly limits
"""
    _, reset_str, reset_min = parse_usage_text(text)
    assert reset_min == 1   # 30 seconds rounds to 1 min
    assert reset_str == '30sec'


def test_pct_takes_first_match_in_session_only():
    """Pct regex must match within the session bound, not weekly's pct."""
    text = """\
Current session
Resets in 1 hr 0 min
75% used

Weekly limits
All models
Resets in 3 days
99% used
"""
    pct, _, _ = parse_usage_text(text)
    assert pct == 75   # the session pct, not weekly's 99
