# Runner State Machine

The runner has two states:

- `chilling`: usage checks run on the schedule, but tasks do not execute.
- `burning`: ready tasks run one at a time until the current usage window reset.

The burn decision is:

```text
100 - used_pct >= 30  and  reset_minutes < 70
```

## Checks

Checks run at:

- process start
- every HH:00 while chilling
- reset-anchored T-60, T-10, and T+5 slots
- after each completed task while burning
- manual `/api/check-usage`

Only one usage check can run at a time. The lock prevents CSV row interleaving
and overlapping usage-provider commands.

## Anchors

Successful checks derive a predicted reset timestamp from `finished_at` plus
`reset_minutes`. The runner stores a durable `next_reset_at` anchor in
`state/runner_state.json`, sanity-banded to avoid one bad provider response
repainting the schedule.

Burn decisions use the safer of fetched reset minutes and the persisted anchor.

## Task Execution

Only one task runs at a time across:

- background runner
- dashboard Run All
- dashboard Run This Task

The executor writes `CODEX.md`, spawns `codex exec --full-auto`, watches for
checkpoint/dry-run/timeout/failure conditions, then restores `CODEX.md`.

## Crash Recovery

On startup, stale locks in `queue/running/` are inspected. If a process died
after writing `CODEX.md`, the backup is restored. Orphaned running tasks move
to `unfinished/` with `stall_reason: crash`.
