# Runner state machine

The runner has two states: **chilling** (default) and **burning** (active queue work).

```
              ┌─────────────┐
              │  CHILLING   │  ← default
              │             │
              │ HH:00 hourly check        ──┐
              │ + T-60 / T-10 / T+5 anchored │  → updates `next_reset_at`
              └──────┬──────┘                │
                     │                       │
                     │ pct_remaining ≥ 30%   │
                     │ AND reset_min < 70    │
                     ▼                       │
              ┌─────────────┐                │
              │   BURNING   │ ← runs tasks   │
              │             │                │
              │ usage check │ ←──────────────┘
              │ after each  │   (does NOT downgrade state;
              │ task finish │    burn window expires naturally)
              └──────┬──────┘
                     │ burn_until passes after current task finishes
                     ▼
                  CHILLING
```

## Chilling

Default. No queued tasks run automatically. The runner only checks usage on a schedule.

**Schedule**

- **Hourly**: every hour's `:00` mark (10:00, 11:00, ...).
- **Reset-anchored**: 60 min before reset (`t_minus_60`), 10 min before reset (`pre_reset`), and 5 min after reset (`post_reset`). The post_reset check is the canonical re-anchor for the next 5-hour cycle.
- **Suppression**: when a reset-anchored slot is within ±10 min of an HH:00 tick, the hourly is suppressed so two checks don't land minutes apart.
- **Anchor persistence**: `next_reset_at` is written to `state/runner_state.json`. A restart mid-cycle re-arms the schedule from the persisted anchor.
- **Anchor sanity bands**: anchor updates are accepted only within ±15 min of the prior anchor (or after a 30-min stale grace). Noisy single fetches can't repaint the truth, but a fallback-promoted hourly takes over if `post_reset` never lands.

**Burn trigger**

```python
should_burn = (usage_left_pct >= 30) and (effective_reset_min < 70)
effective_reset_min = min(latest_fetch_reset, anchor_remaining)
```

Fresh evidence wins over a stale anchor — a burn cannot fire on an anchor that the latest fetch has contradicted.

**Why 30 / 70?** They live as constants in `src/queue_worker/usage_check.py`:

```python
BURN_USAGE_THRESHOLD_PCT = 30
BURN_RESET_WINDOW_MIN    = 70
```

The 70-minute window pairs with the hourly + T-60 schedule: at ~60 min remaining, the check falls inside the window with 10 min of slack for session-start jitter, so the burn fires roughly an hour before reset rather than seconds before (which would bleed into the next session).

**Manual operations** — `Run This Task`, `Run All (once)`, and `Check usage now` bypass the state machine and always execute. Manual checks re-anchor like any other.

## Burning

The runner processes pending tasks one at a time, in dependency + priority order, until `burn_until` passes.

- `burn_until = check_time + reset_minutes` (the actual session reset time, not an arbitrary window).
- After each task finishes, a usage check runs to keep the percentage current. **These checks never downgrade state** — the burn window expires naturally at `burn_until`.
- If the queue empties before `burn_until`, the runner waits in the burn loop for new tasks.
- When `burn_until` passes mid-task, the running task finishes (the deadline is re-checked only between tasks), then the runner returns to chilling.

## Correctness invariants

- **One task at a time.** A process-wide `_execute_lock` serializes `claude -p` invocations across all three execution paths (background runner, `Run All (once)`, `Run This Task`). Two tasks never run concurrently even with the dashboard open in three tabs.
- **Lock-before-select.** The background runner acquires `_execute_lock` *before* choosing a task so a concurrent web run can't snipe it. After the lock, it re-checks `burn_until` so long waits can't push starts past the deadline.
- **Concurrent checks coalesced.** A `_usage_check_lock` ensures only one usage check is in flight at a time. Prevents double `claude -p "hi"` kicks and interleaved CSV writes. Extra manual checks during an in-flight fetch are silently dropped.
- **Manual checks during burn don't downgrade state.** They record the latest pct/reset but leave `state` and `burn_until` alone. Transient fetch errors are recorded without touching state.
- **Concurrent task mutations are tolerated.** If a task disappears between selection and `begin_task` (e.g. cancelled from another tab), the runner catches `FileNotFoundError` and retries on the next tick.

## Crash recovery

On startup, the runner:

1. Scans `queue/running/*.lock` for dead PIDs.
2. Cleans up any injected `CLAUDE.md` (restores backup).
3. Moves orphaned tasks to `unfinished/` with `stall_reason: crash`.
4. Catches YAML files in `running/` with no matching lock file.
5. Loads the persisted reset anchor from `state/runner_state.json` and re-arms the pre/post_reset slot queue.
6. Reconciles queue filenames via `queue_ops.reconcile_filenames` — if a YAML's filename stem doesn't match its `id` field (e.g. iCloud `X [conflicted].yaml` or hand-renamed copies), it's renamed to canonical when no canonical exists, otherwise quarantined to `failed/<id>__corrupt-<stem>.yaml` with `stall_reason: corrupt_filename`.

## Tuning

Edit the constants in `src/queue_worker/usage_check.py` if you want a more aggressive (larger window, smaller usage threshold) or more conservative trigger. Both must be true for the burn to fire.

The chilling-mode check schedule is hardcoded — it's clock-aligned so multiple runner instances would check at the same wall-clock times regardless of when they started (this also means you should not run `queue-worker run` and `queue-worker-web` concurrently against the same queue).
