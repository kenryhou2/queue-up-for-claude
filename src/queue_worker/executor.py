import os
import re
import signal
import time
import threading
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

from .task import Task, augment_task, now_iso
from .injector import build_codex_md, inject_codex_md, cleanup_codex_md, BackupInfo
from .lock import acquire_task_lock, update_task_lock, release_task_lock
from .queue_ops import append_episodic_entry, augment_stall
from .logger import TaskLogger

_TOKEN_PATTERNS = [
    re.compile(r'total\s+tokens?\s*[=:]\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'([\d,]+)\s+tokens?\s+used', re.IGNORECASE),
    re.compile(r'tokens?\s+used\s*[=:]\s*([\d,]+)', re.IGNORECASE),
]

_ENV_BLOCKER_PATTERNS = [
    re.compile(r'bwrap: .*Failed RTM_NEWADDR: Operation not permitted', re.IGNORECASE),
    re.compile(r'blocked before I can read `?CODEX\.md`?', re.IGNORECASE),
    re.compile(r'every (?:local |shell )?command .* failing .* sandbox', re.IGNORECASE),
    re.compile(r'sandbox (?:setup|wrapper) (?:error|layer)', re.IGNORECASE),
    re.compile(r'No MCP .*resources .*available .*fallback', re.IGNORECASE),
]


def _codex_bin() -> str:
    """Return the Codex CLI executable path for queued task execution."""
    from .config import get_env
    configured = (get_env('CODEX_QUEUE_CODEX_BIN') or '').strip()
    if configured:
        return configured
    found = shutil.which('codex')
    return found or 'codex'


def _codex_exec_policy_args() -> list[str]:
    """Return noninteractive execution policy flags for Codex CLI."""
    from .config import get_env
    if (get_env('CODEX_QUEUE_CODEX_BYPASS_SANDBOX') or '').strip() == '1':
        return ['--dangerously-bypass-approvals-and-sandbox']
    return ['--full-auto']


@dataclass
class ExecuteResult:
    status: str                        # 'done' | 'unfinished' | 'failed'
    stall_reason: Optional[str] = None
    stall_detail: Optional[str] = None
    duration_minutes: float = 0.0
    tokens_used: Optional[int] = None


def _run_codex(cmd: list[str], cwd: str,
               timeout_seconds: int, log_fn,
               lock_path: Optional[Path] = None
               ) -> tuple[int, bool, Optional[int], Optional[str]]:
    """
    Spawn Codex as a subprocess in its own process group.
    Stream merged stdout+stderr line by line to log_fn.
    Returns (exit_code, timed_out, tokens_used, environment_blocker_detail).
    """
    # Scrub queue-private config from the subprocess env: the spawned Codex
    # process runs untrusted task prompts and has no business reading dashboard
    # or usage-provider secrets. Defense-in-depth on top of config._DOTENV
    # staying out of os.environ.
    from .config import subprocess_env
    proc = subprocess.Popen(
        cmd, cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env=subprocess_env(),
        preexec_fn=os.setsid,
    )

    # Store subprocess PID and PGID so the cancel API can kill the process group
    # directly without resolving PGID later (avoids PID-reuse race).
    if lock_path:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        update_task_lock(lock_path, {'subprocess_pid': proc.pid, 'subprocess_pgid': pgid})

    timed_out = False
    captured_tokens: list[Optional[int]] = [None]
    environment_blocker: list[Optional[str]] = [None]

    def stream():
        for line in proc.stdout:
            stripped = line.rstrip()
            log_fn(stripped)
            if environment_blocker[0] is None:
                for pat in _ENV_BLOCKER_PATTERNS:
                    if pat.search(stripped):
                        environment_blocker[0] = stripped[:300]
                        break
            for pat in _TOKEN_PATTERNS:
                m = pat.search(stripped)
                if m:
                    captured_tokens[0] = int(m.group(1).replace(',', ''))

    t = threading.Thread(target=stream, daemon=True)
    t.start()

    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()

    t.join(timeout=2)
    return proc.returncode, timed_out, captured_tokens[0], environment_blocker[0]


def _find_new_checkpoint(agent_dir: Path, since: float) -> Optional[Path]:
    checkpoints = agent_dir / 'checkpoints'
    if not checkpoints.exists():
        return None
    for f in sorted(checkpoints.glob('*.yaml')):
        if f.stat().st_mtime > since:
            return f
    return None


def _read_checkpoint(path: Path) -> dict:
    import yaml
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def execute_task(task: Task, log: TaskLogger) -> ExecuteResult:
    """
    Full single-task lifecycle:
    1. Inject CODEX.md into project dir
    2. Spawn `codex exec --full-auto`
    3. Determine outcome (checkpoint > dry-run > timeout > exit code > success)
    4. Cleanup CODEX.md in finally block (always runs)
    """
    start_time = time.monotonic()
    start_epoch = time.time()
    agent_dir = Path(task.resolved_dir) / '.agent'
    lock_path = acquire_task_lock(task.id, task.resolved_dir)
    backup: Optional[BackupInfo] = None

    try:
        # 1. Build and inject CODEX.md
        log.task(task.id, 'building CODEX.md')
        content = build_codex_md(task)
        backup = inject_codex_md(task.resolved_dir, content)
        update_task_lock(lock_path, {
            'codex_md_written': True,
            'backed_up_original': backup.had_original,
        })

        # 2. Spawn Codex. The context is still written to CODEX.md for
        # continuity with interactive sessions, but it is also included in the
        # initial prompt so the agent can start even if its first shell read
        # would fail.
        if task.session_id:
            prompt = (
                'You have been resumed by codex-queue to execute a queued task. '
                'Your full task context is included below and is also written '
                'to CODEX.md in this directory for reference. Complete the task.\n\n'
                '--- BEGIN CODEX.md ---\n'
                f'{content}\n'
                '--- END CODEX.md ---'
            )
            cmd = [_codex_bin(), 'exec', 'resume',
                   *_codex_exec_policy_args(), '--skip-git-repo-check',
                   task.session_id, prompt]
        else:
            prompt = (
                'You have been started by codex-queue. '
                'Your full task context is included below and is also written '
                'to CODEX.md in this directory for reference. Complete the task.\n\n'
                '--- BEGIN CODEX.md ---\n'
                f'{content}\n'
                '--- END CODEX.md ---'
            )
            cmd = [_codex_bin(), 'exec',
                   *_codex_exec_policy_args(), '--skip-git-repo-check',
                   '-C', task.resolved_dir, prompt]
        timeout_s = task.budget.max_minutes * 60

        mode = f'resume {task.session_id[:12]}...' if task.session_id else 'exec'
        log.task(task.id, f'spawning codex {mode} (timeout: {task.budget.max_minutes}min)')
        exit_code, timed_out, tokens_used, environment_blocker = _run_codex(
            cmd=cmd, cwd=task.resolved_dir,
            timeout_seconds=timeout_s,
            log_fn=lambda line: log.task(task.id, f'  {line}'),
            lock_path=lock_path,
        )

        duration = (time.monotonic() - start_time) / 60

        if tokens_used:
            log.task(task.id, f'tokens used: {tokens_used:,}')

        # 3. Determine outcome (check in this order)

        # a) Checkpoint?
        checkpoint_path = _find_new_checkpoint(agent_dir, start_epoch)
        if checkpoint_path:
            log.task(task.id, f'checkpoint detected: {checkpoint_path.name}')
            cp = _read_checkpoint(checkpoint_path)
            augment_stall(task, 'checkpoint',
                          cp.get('question', 'Agent wrote a checkpoint.'),
                          checkpoint_content=cp)
            augment_task(task.yaml_path, {'checkpoint_file': str(checkpoint_path)})
            _write_episodic(task, 'unfinished', 'checkpoint', duration, tokens_used)
            return ExecuteResult('unfinished', 'checkpoint',
                                cp.get('question'), duration, tokens_used)

        # b) Dry-run?
        if task.dry_run:
            today = datetime.now().strftime('%Y%m%d')
            dryrun_dir = agent_dir / 'dry-run' / today
            if dryrun_dir.exists():
                log.task(task.id, 'dry-run output detected')
                augment_stall(task, 'dry_run_complete',
                              f'Proposed changes in .agent/dry-run/{today}/')
                _write_episodic(task, 'unfinished', 'dry_run_complete', duration, tokens_used)
                return ExecuteResult('unfinished', 'dry_run_complete',
                                    f'Review .agent/dry-run/{today}/', duration, tokens_used)

        # c) Timeout?
        if timed_out:
            log.task(task.id, 'timed out')
            augment_stall(task, 'timeout',
                          f'Exceeded {task.budget.max_minutes} minute budget.')
            _write_episodic(task, 'unfinished', 'timeout', duration, tokens_used)
            return ExecuteResult('unfinished', 'timeout',
                                f'Exceeded {task.budget.max_minutes}min budget',
                                duration, tokens_used)

        # d) Codex reported it could not execute the task?
        if environment_blocker:
            detail = f'codex environment blocker: {environment_blocker}'
            log.task(task.id, detail)
            _write_episodic(task, 'failed', None, duration, tokens_used)
            return ExecuteResult('failed', None, detail, duration, tokens_used)

        # e) Non-zero exit?
        if exit_code != 0:
            detail = f'codex exited with code {exit_code}'
            log.task(task.id, detail)
            _write_episodic(task, 'failed', None, duration, tokens_used)
            return ExecuteResult('failed', None, detail, duration, tokens_used)

        # f) Success
        briefing = agent_dir / 'briefings' / f'{datetime.now().strftime("%Y%m%d")}.md'
        if not briefing.exists():
            log.task(task.id, 'warning: agent did not write a briefing')
        log.task(task.id, f'done ({duration:.1f}min)')
        _write_episodic(task, 'done', None, duration, tokens_used)
        return ExecuteResult('done', duration_minutes=duration, tokens_used=tokens_used)

    finally:
        if backup is not None:
            cleanup_codex_md(task.resolved_dir, backup)
        release_task_lock(lock_path)


def _write_episodic(task: Task, status: str, stall_reason: Optional[str],
                    duration_minutes: float,
                    tokens_used: Optional[int] = None) -> None:
    entry = {
        'ts': now_iso(),
        'task_id': task.id,
        'status': status,
        'stall_reason': stall_reason,
        'duration_minutes': round(duration_minutes, 1),
        'level': task.level,
    }
    if tokens_used:
        entry['tokens_used'] = tokens_used
    append_episodic_entry(task.resolved_dir, entry)
