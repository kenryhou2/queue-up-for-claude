# Web Dashboard

Start:

```bash
codex-queue-web
```

Open `http://localhost:51002`.

Do not run `codex-queue run` alongside `codex-queue-web` against the same
queue. Both would try to process tasks.

## Auth

If `CODEX_QUEUE_PASSWORD` is set, all routes require a session cookie. If it is
unset, auth is disabled for local/trusted access.

## Main Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | Runner state + queue counts |
| `POST` | `/api/check-usage` | Trigger a usage command check |
| `GET` | `/api/tasks` | List tasks |
| `POST` | `/api/tasks` | Add task |
| `GET` | `/api/tasks/{id}` | Task detail |
| `GET` | `/api/tasks/{id}/output` | Tail per-task Codex output |
| `GET` | `/api/tasks/{id}/resume-info` | Summarize Codex transcript for a UUID session id |
| `GET` | `/api/context/{id}` | Render the `CODEX.md` that will be injected |
| `GET` | `/api/usage-history` | CSV-backed usage history |

The file-browser endpoints are intentionally broad and are only appropriate
for loopback, Tailscale, or similarly trusted access.
