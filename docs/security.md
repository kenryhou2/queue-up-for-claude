# Security

codex-queue is built for personal use on a trusted host. It is not hardened for
multi-tenant or public deployment.

## Trust Model

Protected:

- dashboard login when `CODEX_QUEUE_PASSWORD` is set
- `.env` values kept out of `os.environ`
- queue-private env keys stripped from Codex subprocesses
- likely API keys and emails redacted from dashboard status strings

Not protected:

- filesystem isolation for queued tasks
- process isolation between tasks
- hard enforcement of capability profiles
- unrestricted file-browser API access

## Codex Execution

The executor runs `codex exec --full-auto`. Capability profiles are prompt-level
instructions compiled into `CODEX.md`; they are not OS permissions. Use a
separate user account, filesystem permissions, containers, or VM boundaries for
hard isolation.

## Sensitive Data

| Data | Location | Notes |
|---|---|---|
| Usage provider command | `CODEX_QUEUE_USAGE_COMMAND` or `.env` | `.env` must be mode `0600` |
| Dashboard password | `CODEX_QUEUE_PASSWORD` or `.env` | stripped from Codex subprocess env |
| Usage history | `usage_history.csv` | timestamps + percentages |
| Task/output logs | `queue/`, `logs/` | may contain arbitrary command output |

## File Browser

The `/api/files/*` endpoints are not sandboxed. They can browse and read paths
accessible to the process user. Do not expose the dashboard publicly without a
strong network-level access gate.
