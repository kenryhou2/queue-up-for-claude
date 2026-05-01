# Security model and known limitations

queue-up-for-claude is built for **personal use on a trusted host**. It is not hardened for multi-tenant or public deployment. This document spells out what's protected, what isn't, and the recommended deployment shape.

## Threat model

**In scope** — what the system tries to defend against:

- Credential theft from a curious user on the same machine (session key + dashboard password storage).
- Brute-force attacks against the dashboard password (per-IP and global rate limits).
- Header spoofing of `CF-Connecting-IP` (only honored when the request peer is loopback).
- Accidental commit of secrets (`.env` and `state/` are gitignored; `usage_history.csv` too).
- Stored-XSS via SVG preview (SVG is excluded from inline rendering and falls back to text source).
- Session cookies in transit (HttpOnly + SameSite=Lax; `Secure` flag set unless `QUEUE_WORKER_COOKIE_SECURE=0`).

**Out of scope** — not defended against:

- A compromised host. If someone has shell access as your user, they have everything.
- A leaked dashboard password or session key. Rotate via the steps below.
- An attacker already past your tunnel/VPN.
- Network-level attacks on Claude.ai itself.
- Process isolation between tasks. Each `claude -p` runs as your user with full access to the project directory and any caps the level grants.

## Known limitations

### File-browser API is not sandboxed

`/api/files/list`, `/api/files/read`, `/api/files/raw`, and `/api/browse` accept arbitrary absolute paths and allow `..` navigation. Any authenticated caller (or anyone if auth is disabled) can browse and read files outside any task workspace.

This is **fine for personal use on a trusted network** (loopback or Tailscale). Do not expose the dashboard to the public internet without:

1. A strong password (`QUEUE_WORKER_PASSWORD`), and ideally
2. A second-factor gate in front (Tailscale or Cloudflare Access), and
3. Awareness that authenticated users can read anything your shell user can read.

### `--dangerously-skip-permissions`

The executor spawns `claude -p --dangerously-skip-permissions`. The capability boundaries enforced by queue-up-for-claude are **advisory** — they're injected into `CLAUDE.md` and Claude is asked to honor them, but a sufficiently determined or hallucinating model could ignore them.

If you need hard isolation, queue tasks under levels with the smallest cap set that gets the job done (`observer` for read-only audits, `craftsman` for code changes without git access, etc.) and run the worker inside a container or VM that scopes the host filesystem accordingly.

### `caps_override.remove` is also advisory

Capabilities are model-prompted, not OS-enforced. `caps_override.remove: [delete_files]` makes the CLAUDE.md tell the agent it cannot delete files; it does not remove `unlink` from the syscall surface.

### Task prompts are stored in plaintext YAML

Don't put secrets in task prompts. The YAML lives at `queue/{pending,running,done,...}/<id>.yaml` and is also exposed via `/api/tasks/{id}`.

### Logs may contain shell output

`logs/YYYY-MM-DD.log` captures stdout/stderr from `claude -p` subprocesses, which may include arbitrary shell command output. Treat the log directory as sensitive.

## Egress redaction

Strings sent to the dashboard (e.g., `last_check_error`, `last_check_status`) pass through `usage_check_http.redact()` which strips:

- `sk-ant-...` keys
- Email addresses

This is **defense in depth** — the source code is supposed to never put a secret in those fields in the first place. It exists to catch regressions.

## Storage

| What | Where | Mode | Gitignored |
|---|---|---|---|
| Session key | `.env` (project local) **or** `~/.config/queue-worker/session_key` **or** `CLAUDE_SESSION_KEY` env | `0600` recommended for files | yes (`.env`) |
| Dashboard password | `QUEUE_WORKER_PASSWORD` env only | n/a | n/a |
| Org UUID cache | `state/org_cache.json` | default | yes (`state/`) |
| Reset anchor | `state/runner_state.json` | default | yes |
| Usage history | `usage_history.csv` | default | yes |
| Task YAMLs | `queue/<bucket>/<id>.yaml` | default | `running/` is gitignored; the rest is your call |
| Per-task output | `logs/YYYY-MM-DD.log` | default | yes |
| Browser profile | `.chrome-profile/` | default | yes |

## Rotation

**Session key compromised**

1. Open `claude.ai` in your browser, sign out and back in to invalidate the old key.
2. Copy the new `sessionKey` cookie value.
3. Replace it in `.env` (or `~/.config/queue-worker/session_key`, or your shell profile).
4. Restart `queue-worker-web` / `queue-worker run`.

**Dashboard password compromised**

1. Stop the web server.
2. Update `QUEUE_WORKER_PASSWORD`.
3. Restart.

Active session cookies signed under the old password are **not** invalidated by changing the password — they're stored server-side in memory only, so a server restart drops all sessions. Restart the web server after rotation.

## Reporting a vulnerability

This project has no formal security team. If you find a real issue, open a public GitHub issue or a private email to the maintainer; expect a slow response. Do not assume it's safe to deploy this for anyone other than yourself.
