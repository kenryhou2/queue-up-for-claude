import os
import json
from pathlib import Path
from dataclasses import dataclass
from .task import now_iso

# Set by config.bootstrap() at startup
RUNNING_DIR: Path = None   # type: ignore[assignment]


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_task_lock(task_id: str, project_dir: str) -> Path:
    """Write a lock file to queue/running/<id>.lock with crash-recovery metadata."""
    RUNNING_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = RUNNING_DIR / f"{task_id}.lock"
    lock_path.write_text(json.dumps({
        'task_id': task_id,
        'pid': os.getpid(),
        'started_at': now_iso(),
        'dir': project_dir,
        'claude_md_written': False,
        'backed_up_original': False,
    }, indent=2))
    return lock_path


def update_task_lock(lock_path: Path, fields: dict) -> None:
    data = json.loads(lock_path.read_text())
    data.update(fields)
    lock_path.write_text(json.dumps(data, indent=2))


def release_task_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


@dataclass
class StaleLock:
    lock_path: Path
    task_id: str
    pid: int
    project_dir: str
    claude_md_written: bool
    backed_up_original: bool


def recover_stale_locks() -> list[StaleLock]:
    """Scan queue/running/*.lock, return any whose PID is dead."""
    stale = []
    if not RUNNING_DIR or not RUNNING_DIR.exists():
        return stale
    for lock_file in RUNNING_DIR.glob('*.lock'):
        try:
            data = json.loads(lock_file.read_text())
            pid = data.get('pid', 0)
            if not is_pid_alive(pid):
                stale.append(StaleLock(
                    lock_path=lock_file,
                    task_id=data.get('task_id', lock_file.stem),
                    pid=pid,
                    project_dir=data.get('dir', ''),
                    claude_md_written=data.get('claude_md_written', False),
                    backed_up_original=data.get('backed_up_original', False),
                ))
        except json.JSONDecodeError:
            stale.append(StaleLock(
                lock_path=lock_file, task_id=lock_file.stem, pid=0,
                project_dir='', claude_md_written=False, backed_up_original=False,
            ))
    return stale
