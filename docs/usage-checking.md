# Usage Checking

codex-queue checks token usage before deciding to burn tasks. Two backends are
available, selected by `CODEX_QUEUE_USAGE_BACKEND`.

| Backend | Value | When to use |
|---|---|---|
| Local command | `command` (default) | You have a script that can report usage |
| ChatGPT Codex HTTP | `codex_http` | You want built-in fetching from the Codex cloud analytics page |

---

## Quickstart: token backend

Use the built-in backend when you want codex-queue to fetch Codex cloud usage
with your ChatGPT browser session token.

```bash
cp .env.example .env
chmod 0600 .env
./codex-queue set-chatgpt-token
printf '\nCODEX_QUEUE_USAGE_BACKEND=codex_http\n' >> .env
./codex-queue check-usage
```

When prompted, paste the `__Secure-next-auth.session-token` cookie from
`https://chatgpt.com`. To avoid an interactive prompt:

```bash
printf '%s' "$TOKEN" | ./codex-queue set-chatgpt-token --stdin
```

---

## Backend A — Local command (default)

Set `CODEX_QUEUE_USAGE_COMMAND` to any command. It is split with `shlex.split()`
and run without a shell.

The command must exit 0 and print JSON to stdout:

```json
{"used_pct": 71, "reset_minutes": 58}
```

Fields:

| Field | Required | Meaning |
|---|---:|---|
| `used_pct` | yes | Integer 0-100, percent of the current usage window already used |
| `reset_minutes` | yes | Integer >= 0, minutes until the current usage window resets |

Example `.env`:

```bash
CODEX_QUEUE_USAGE_COMMAND="$HOME/bin/codex-usage-json"
CODEX_QUEUE_USAGE_TIMEOUT_SECONDS=30
```

---

## Backend B — ChatGPT Codex cloud HTTP

Set `CODEX_QUEUE_USAGE_BACKEND=codex_http` to fetch usage directly from the
ChatGPT Codex cloud analytics page at
`https://chatgpt.com/codex/cloud/settings/analytics#usage`.

### Setup

**Step 1 — Get your session token**

1. Log in at https://chatgpt.com
2. Open browser DevTools → **Application** tab → **Cookies** → `chatgpt.com`
3. Find the cookie named `__Secure-next-auth.session-token`
4. Copy its value (it is a long JWT string)

**Step 2 — Store it securely** (choose one):

Option A — `.env` file:
```bash
CODEX_QUEUE_USAGE_BACKEND=codex_http
CODEX_QUEUE_CHATGPT_SESSION_TOKEN=<paste token here>
```

Option B — dedicated file (more secure, no other secrets in the same file):
```bash
codex-queue set-chatgpt-token
# then in .env:
CODEX_QUEUE_USAGE_BACKEND=codex_http
```

**Step 3 — Verify**

```bash
codex-queue check-usage   # or trigger from the web dashboard
```

### Session token refresh

ChatGPT session tokens rotate periodically (typically every 30 days). When the
runner reports `chatgpt_session_token_invalid`, log in again and repeat Step 1-2.

### Troubleshooting the API endpoint

The default usage URL is:
```
https://chatgpt.com/backend-api/wham/usage
```

If this returns an error, check the current endpoint by:
1. Opening DevTools → **Network** tab on
   `https://chatgpt.com/codex/cloud/settings/analytics`
2. Filtering for XHR/Fetch requests
3. Looking for a request that returns usage/analytics data

Then set `CODEX_QUEUE_CHATGPT_USAGE_URL=<new URL>` in your `.env`.

---

## Burn rule

The runner burns tasks when:

- remaining budget is at least 30%, and
- reset is under 70 minutes away.

All provider errors fail closed: the runner stays chilling and tries again on
the next scheduled check.

---

## Error codes

### Command backend

| Code | Meaning |
|---|---|
| `usage_command_missing` | `CODEX_QUEUE_USAGE_COMMAND` is unset, malformed, or the binary is not on PATH |
| `usage_command_failed` | Command exited nonzero |
| `usage_command_timeout` | Command exceeded `CODEX_QUEUE_USAGE_TIMEOUT_SECONDS` |
| `bad_response` | stdout was not valid JSON or fields were missing/out of range |

### ChatGPT Codex HTTP backend

| Code | Meaning |
|---|---|
| `chatgpt_session_token_missing` | Session token not configured or file absent |
| `chatgpt_session_token_invalid` | Server returned 401 — token is expired or wrong |
| `rate_limited` | Server returned 429 |
| `cloudflare_blocked` | Cloudflare challenge page returned |
| `bad_response` | API returned JSON but fields were unrecognisable |
| `network_error` | DNS / TCP failure |
| `http_error` | 5xx or unexpected status code |

---

## Security

The command backend inherits the process environment except queue-private config
keys such as `CODEX_QUEUE_USAGE_COMMAND` and `CODEX_QUEUE_PASSWORD`. Put
provider-specific credentials in the environment or files that your command
reads directly.

The HTTP backend (`codex_http`) stores the session token in the private `.env`
store or in a dedicated `0600` file — it is never exported to child processes.
