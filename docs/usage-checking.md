# Usage checking

queue-up-for-claude has two backends for reading your Claude.ai plan usage:

- **HTTP** (preferred): calls the same `/api/organizations/{uuid}/usage` endpoint the claude.ai UI uses, authenticated with your `sessionKey` cookie. Fast, stateless, no browser required for the check itself.
- **Playwright** (fallback): scrapes the rendered `claude.ai/settings/usage` page through a long-running Chrome instance over CDP. Slower, heavier, but resilient to API shape changes and works without needing a sessionKey.

The dispatcher picks one based on `QUEUE_WORKER_USAGE_BACKEND` and falls back gracefully (see [Recovery flow](#recovery-flow)).

## Setup: sessionKey cookie

Required for the HTTP backend.

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

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_SESSION_KEY` | unset | Required for HTTP backend; the cookie value above |
| `CLAUDE_ORG_UUID` | unset | Pin a specific org. **Required if your account has more than one org** — otherwise the HTTP backend refuses to auto-pick rather than risk querying the wrong one |
| `QUEUE_WORKER_USAGE_BACKEND` | `auto` | `auto` (HTTP if key set, else Playwright), `http`, or `playwright` |

## Recovery flow

When `QUEUE_WORKER_USAGE_BACKEND=auto`:

1. **HTTP first** when a session key is configured.
2. On `between_sessions` (the API has no fresh `resets_at`), kick a fresh 5-hour window by running `claude -p "hi"` as a subprocess, settle 3s, then retry HTTP once.
3. If HTTP still fails, or HTTP returns `session_key_missing` / `cloudflare_blocked`, fall back to Playwright. Playwright runs its own scrape → kick → retry loop as a final safety net.
4. **Does not fall back on `session_key_invalid`** — rotate the key from your browser cookies. A bad key is a config issue that needs surfacing, not silently bypassing.

The "kick" trick — running `claude -p "hi"` to start the rolling window — works because the Claude CLI and `claude.ai` share the same plan and same 5-hour bucket. One short message via the CLI flips the API state, and the next read returns real numbers.

## Playwright backend specifics

The Playwright backend connects to a long-running Chrome over CDP using a dedicated profile at `.chrome-profile/` (separate from your daily Chrome — keeps you logged in to Claude without affecting your normal browsing, and avoids bot-detection issues that would hit a generic automated profile).

- The runner **auto-launches Chrome** when it needs to scrape — you don't have to start it manually.
- The first launch redirects to login. Log in once in that Chrome window — the profile persists across runs.
- Each check writes a screenshot to `logs/usage_check_img/<ts>__<label>.png`. Pruned by age (7 days) with a hard cap of 5000 files as a safety net against runaway disk use.
- If the page renders the "Starts when a message is sent" placeholder (window not yet started), the scraper kicks via `claude -p "hi"`, waits 3s, and re-reads (up to 3 retries).

If you'd rather launch Chrome yourself before the runner starts:

```bash
python scripts/check_usage.py start
```

## CSV history

Every successful check appends a row to `usage_history.csv` at the project root:

```
timestamp,pct_used,reset_minutes,status,backend
```

The web dashboard's Usage view plots this with Today / Week / Month / All ranges. The file is gitignored — it's local-only telemetry, no prompts or chat content.

## Manual checks

```bash
# CLI doesn't expose a one-shot check command; use the web API:
curl -X POST http://localhost:51002/api/check-usage
```

Or click `Check usage now` on the dashboard. Same lock and burn-preservation logic as the background loop.

## Tuning the burn trigger

```python
# src/queue_worker/usage_check.py
BURN_USAGE_THRESHOLD_PCT = 30   # only burn if usage_left ≥ 30%
BURN_RESET_WINDOW_MIN    = 70   # only burn if reset_minutes < 70
```

Both must be true. See [runner-state-machine.md](runner-state-machine.md) for why these defaults pair with the check schedule.
