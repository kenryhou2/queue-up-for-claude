# Usage checking

queue-up-for-claude reads your Claude.ai plan usage by calling the same `/api/organizations/{uuid}/usage` endpoint the claude.ai UI uses, authenticated with your `sessionKey` cookie. No browser is launched. There is no scraper fallback.

## Setup: sessionKey cookie

**Chrome / Edge**
1. Open [claude.ai](https://claude.ai)
2. `F12` → Application → Cookies → `https://claude.ai`
3. Copy the value of the `sessionKey` cookie (starts with `sk-ant-`)

**Safari**
1. Open [claude.ai](https://claude.ai)
2. Develop → Show Web Inspector → Storage → Cookies → `https://claude.ai`
3. Copy `sessionKey`

**Firefox**
1. Open [claude.ai](https://claude.ai)
2. `F12` → Storage → Cookies → `https://claude.ai`
3. Copy `sessionKey`

Then store it. Recommended (project-local, gitignored, mode 0600):

```bash
cp .env.example .env
chmod 0600 .env
$EDITOR .env             # paste sessionKey into CLAUDE_SESSION_KEY=
```

Alternatives:

```bash
# Process env (always wins over .env)
export CLAUDE_SESSION_KEY="sk-ant-..."

# Or a 0600 file under XDG config
install -m 0600 /dev/null ~/.config/queue-worker/session_key
echo 'sk-ant-...' > ~/.config/queue-worker/session_key
```

## Environment variables

| Variable | Required? | Effect |
|---|---|---|
| `CLAUDE_SESSION_KEY` | yes | The cookie value above |
| `CLAUDE_ORG_UUID` | only on multi-org | Pin a specific org. **Required if your account has more than one org** — otherwise the HTTP backend refuses to auto-pick rather than risk querying the wrong one |

## Error codes

The HTTP backend produces a stable `error_code` on each failure. The runner displays the latest one in the dashboard's "last check" panel.

| Code | What it means | What to do |
|---|---|---|
| `session_key_missing` | No key configured (env, .env, and key file all empty) | Set `CLAUDE_SESSION_KEY` |
| `session_key_invalid` | The API returned 401 | Rotate the key from your browser cookies — sessions expire |
| `between_sessions` | API has no active 5-hour window. The runner kicks `claude -p "hi"` once, settles 3s, then re-fetches. If recovery succeeds, this is invisible | (transient) |
| `cloudflare_blocked` | Cloudflare challenged the API call | Wait. If it persists, your IP / headers may be flagged — there is no scraper fallback to bypass it |
| `rate_limited` | API returned 429 | Backs off; retried on the next scheduled check |
| `network_error` | DNS / TCP / TLS failure | Transient, retried on next check |
| `http_error` | Other 5xx or unexpected response shape | Transient, retried on next check |
| `bad_response` | Response body wasn't JSON or didn't have expected keys | Likely an upstream API change — check for queue-worker updates |
| `multi_org_no_pin` | Your account has more than one org and `CLAUDE_ORG_UUID` is not set | Set `CLAUDE_ORG_UUID` to the org you want queried |
| `org_resolve_failed` | The org list endpoint returned nothing usable | Likely an upstream API change — check for queue-worker updates |
| `parse_failed` | Numbers were missing from the response | Likely an upstream API change — check for queue-worker updates |

## The `between_sessions` kick

The Claude.ai 5-hour window starts on the **first message** sent in a new bucket. If the runner first checks usage when no session is active, the API has nothing to report.

To recover: the runner runs `claude -p "hi"` as a subprocess. The Claude CLI shares the same plan and same 5-hour bucket as `claude.ai`, so one short message starts the window. After a 3-second settle the runner re-fetches and gets real numbers.

If `claude` isn't on PATH, the kick fails with a clear pointer to install Claude Code. Auth / rate-limit / network failures from the kick surface (with redaction) in the runner log and the dashboard's last-check status.

## CSV history

Every successful check appends a row to `usage_history.csv` at the project root:

```
timestamp,pct_used,reset_minutes,status
```

The web dashboard's Usage view plots this with Today / Week / Month / All ranges. The file is gitignored — local-only telemetry, no prompts or chat content.

Exactly one row is appended per logical check, even when a kick recovery happened in the middle. The chart never sees phantom rows for transient errors that recovered.

## Manual check

```bash
# CLI doesn't expose a one-shot check; use the web API:
curl -X POST http://localhost:51002/api/check-usage
```

Or click `Check usage now` on the dashboard. Same lock and burn-preservation logic as the background loop.

## Tuning the burn trigger

```python
# src/queue_worker/usage_check.py
BURN_USAGE_THRESHOLD_PCT = 30   # only burn if usage_left ≥ 30%
BURN_RESET_WINDOW_MIN    = 70   # only burn if reset_minutes < 70
```

The runner imports both. See [runner-state-machine.md](runner-state-machine.md) for why these defaults pair with the check schedule.

## What this tool does NOT do

- **No browser automation.** Earlier versions had a Playwright/CDP fallback that drove a real Chrome instance to scrape the rendered usage page. It was removed for v1 to drop the Chrome dependency. If the cookie API stops working (TOS change, Cloudflare hardening, endpoint shape change), the runner will report errors and stay in the chilling state until a fix lands.
- **No interactive login flow.** You paste the cookie value once. When it expires (typically when you log out of claude.ai or hit the session-rotation interval), you re-paste. Sorry.
