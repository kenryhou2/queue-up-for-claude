# Usage Checking

codex-queue reads usage from a local command configured by
`CODEX_QUEUE_USAGE_COMMAND`. The command is split with `shlex.split()` and run
without a shell.

## Command contract

The command must exit 0 and print JSON to stdout:

```json
{"used_pct": 71, "reset_minutes": 58}
```

Fields:

| Field | Required | Meaning |
|---|---:|---|
| `used_pct` | yes | Integer 0-100, percent of the current usage window already used |
| `reset_minutes` | yes | Integer >= 0, minutes until the current usage window resets |

Example:

```bash
export CODEX_QUEUE_USAGE_COMMAND="$HOME/bin/codex-usage-json"
export CODEX_QUEUE_USAGE_TIMEOUT_SECONDS=30
```

## Burn rule

The runner burns tasks when:

- remaining budget is at least 30%, and
- reset is under 70 minutes away.

All provider errors fail closed: the runner stays chilling and tries again on
the next scheduled check.

## Error codes

| Code | Meaning |
|---|---|
| `usage_command_missing` | `CODEX_QUEUE_USAGE_COMMAND` is unset, malformed, or the binary is not on PATH |
| `usage_command_failed` | Command exited nonzero |
| `usage_command_timeout` | Command exceeded `CODEX_QUEUE_USAGE_TIMEOUT_SECONDS` |
| `bad_response` | stdout was not valid JSON or fields were missing/out of range |

## Security

The usage command inherits the process environment except queue-private config
keys such as `CODEX_QUEUE_USAGE_COMMAND` and `CODEX_QUEUE_PASSWORD`. Put
provider-specific credentials in the environment or files that your command
reads directly.
