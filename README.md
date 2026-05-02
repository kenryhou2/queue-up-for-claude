# queue-up-for-codex

A usage-aware job queue for Codex CLI.

You queue tasks against your projects. The runner checks a local usage command,
waits until unused budget is near reset, then runs one `codex exec --full-auto`
subprocess per task. Each task gets a fresh Codex session with project-specific
identity, capability boundaries, and rolling memory injected via `CODEX.md`.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Anthropic. Reads your Claude.ai plan usage by calling the same web endpoints the claude.ai UI uses with your `sessionKey` cookie. **This may violate Anthropic's Terms of Service.** See [Disclaimer](#disclaimer). If the cookie API ever stops working, this tool stops working — there is no browser-scraping fallback.

> ⚠️ **Full-disk file access.** This tool can read any file on your computer that your user account can read. Two reasons:
> 1. The dashboard's file-browser API (`/api/files/list|read|raw`) is **not sandboxed** — it accepts any absolute path and resolves it via the OS. Anyone who can reach the dashboard can browse your whole home directory. Keep it on `127.0.0.1` / Tailscale and put a password on it before exposing it.
> 2. Each task runs `claude -p --dangerously-skip-permissions`, so the Claude Code subprocess has no per-file permission prompts — it can read/write anywhere your user can.
>
> **Nothing is uploaded to the author and there is no telemetry.** The session key, usage history, task prompts, and logs all stay on your machine. The two outbound destinations are the ones you'd expect: `claude.ai` (for usage checks, with your cookie) and Anthropic's API (whatever `claude -p` sends as part of running your tasks — prompts and any files it reads, same as using Claude Code directly). See [Data storage](#data-storage).

---

## Why

If you're on a Claude Max plan, your token allotment refills every five hours regardless of whether you used the previous bucket. Pure on-demand usage leaves a lot on the table. queue-up-for-claude lets you stage work asynchronously and only burns it when you have unused budget about to expire — turning idle plan capacity into shipped code.

The runner is also a structured way to give a Claude Code subprocess a stable identity per project (who it is, what the project is, what it's allowed to do, what it remembers from prior sessions) without you re-typing context each time.
This is a local personal automation tool. It is not affiliated with or supported
by OpenAI. It does not call an OpenAI usage API directly; you provide a local
command that reports usage as JSON.

---

## How it works

Two states: **chilling** (default) and **burning** (active).

```
              +-------------+  hourly + reset-anchored checks
              |  CHILLING   |  (HH:00 + T-60 / T-10 / T+5)
              +------+------+ 
                     |  remaining >= 30% AND reset < 70min
              +------v------+
              |   BURNING   |  one task at a time until reset
              +------+------+
                     |
                     v
       codex exec --full-auto per task,
       fresh CODEX.md injected per task,
       outcome recorded, queue advances.
```

For each task the runner:

1. Resolves dependencies + priority to pick the next ready task.
2. Builds a `CODEX.md` from the project's `.agent/` files and injects it into the project dir.
3. Spawns `codex exec --full-auto -C <project> <prompt>`.
4. Watches for checkpoint files, dry-run output, timeouts, and exit codes.
5. Restores the original `CODEX.md`, moves the task YAML, and updates episodic memory.

---

## Quick start

Requires **Python 3.11+** and **Codex CLI** installed and authenticated.

```bash
git clone <your-fork-url> queue-up-for-codex
cd queue-up-for-codex
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env && chmod 0600 .env
# set CODEX_QUEUE_USAGE_COMMAND=...; see docs/usage-checking.md

./codex-queue init ~/projects/my-app
./codex-queue add ~/projects/my-app "Add input validation to /signup" --level committer

codex-queue-web
# open http://localhost:51002
```

The usage command must print:

```json
{"used_pct": 71, "reset_minutes": 58}
```

---

## Features

| Area | What | Read more |
|---|---|---|
| Usage-aware runner | Chilling/burning state machine, reset-anchored scheduling, local command usage provider | [runner-state-machine.md](docs/runner-state-machine.md), [usage-checking.md](docs/usage-checking.md) |
| Per-project agent identity | `.agent/` directory with AGENT/CONTEXT/BEHAVIOR + rolling memory, checkpoints, briefings, proposed memory edits | [agent-context.md](docs/agent-context.md) |
| Capability boundaries | Four levels compiled into ALLOWED / NOT ALLOWED sections of injected `CODEX.md`; per-task overrides | [agent-context.md](docs/agent-context.md#capability-levels) |
| Web dashboard | FastAPI + Alpine.js SPA at `localhost:51002` | [web-dashboard.md](docs/web-dashboard.md) |
| Auth + remote access | Optional password gate with brute-force protection | [auth-and-remote-access.md](docs/auth-and-remote-access.md) |
| CLI | `codex-queue {init,add,ls,status,next,context,logs,retry,remove,compile,run,...}` | [cli.md](docs/cli.md) |
| Crash recovery | Per-task lock files, dead-PID detection, `CODEX.md` backup restoration, durable reset anchor | [runner-state-machine.md](docs/runner-state-machine.md#crash-recovery) |

---

## Repo layout

```
queue-up-for-codex/
├── .agent/                 ← this repo's own codex-queue context
├── codex-queue             ← bash wrapper for the CLI
├── pyproject.toml          ← package metadata + entry points
├── config/profiles.yaml    ← capability level definitions
├── docs/                   ← per-feature documentation
├── src/queue_worker/       ← Python code
│   ├── cli.py              ← Click commands + .agent/ templates
│   ├── web.py              ← FastAPI dashboard + background runner thread
│   ├── runner.py           ← state machine + scheduling
│   ├── usage_check.py      ← dispatcher + CSV write
│   ├── usage_check_command.py ← local JSON command backend
│   ├── executor.py         ← Codex subprocess lifecycle
│   ├── injector.py         ← CODEX.md builder + inject/cleanup
│   ├── sessions.py         ← Codex transcript locator
│   └── static/             ← dashboard SPA
└── tests/                  ← pytest unit tests
```

---

## Data storage

<<<<<<< HEAD
**This is unofficial software.** It is not affiliated with, endorsed by, or supported by Anthropic PBC.

queue-up-for-claude reads your Claude.ai plan usage by calling the same web API endpoints the claude.ai UI uses, authenticated with your `sessionKey` browser cookie.

**This may violate Anthropic's Terms of Service.** By using this software you accept that:

- Anthropic may block, restrict, or terminate your access at any time.
- Your Claude account could be affected by using unofficial usage-checking methods.
- Token usage from automated `claude -p` runs counts against your normal plan limits.
- **You use this at your own risk.** The author assumes no liability.

### Data storage

- The session key lives in `.env` (gitignored, `0600` recommended), in `~/.config/queue-worker/session_key` (mode `0600`), or in process env. **Never transmitted anywhere except claude.ai.**
- Usage history is appended to `usage_history.csv` in the project directory — timestamps + percentages only, no prompts or chat content.
- Task prompts and per-task `claude -p` output live in `queue/` and `logs/`.
- Strings sent to the dashboard pass through a redactor that strips `sk-ant-...` keys and email addresses (defense in depth).
- **No data is sent to the author and there is no telemetry.** Outbound traffic only goes to `claude.ai` (usage check, authenticated with your cookie) and Anthropic's API (whatever `claude -p` sends while running your tasks — prompts and the contents of any files it reads, same as using Claude Code directly).
- The dashboard's file browser API is **not path-scoped**: it can list and read any file the process user can read. Treat it as you would a remote shell — bind to loopback / Tailscale, set a password, do not expose publicly.

See [docs/security.md](docs/security.md) for the full security model and known limitations (notably: the file-browser API is not sandboxed — fine for loopback / Tailscale, not safe to expose publicly without scoping).

---

## License
=======
- Usage history is appended to `usage_history.csv` in the project directory.
- Task prompts and per-task Codex output live in `queue/` and `logs/`.
- `.env` is loaded into a private in-process store and is not exported into Codex subprocesses.
- Strings sent to the dashboard pass through a redactor for likely API keys and email addresses.
>>>>>>> 160762e (editing for codex)

MIT — see [LICENSE](LICENSE).
