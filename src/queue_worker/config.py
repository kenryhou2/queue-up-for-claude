"""Shared project paths, bootstrap, logger singleton, and private .env loader.

The .env loader is deliberately private — values are NOT exported into
``os.environ``. The runner spawns ``claude -p`` subprocesses with
``env=subprocess_env()`` (executor.py), which copies ``os.environ`` and
filters out queue-worker-private keys. Secrets in particular
(CLAUDE_SESSION_KEY, QUEUE_WORKER_PASSWORD) must never leak that way.
Use ``get_env()`` instead of ``os.environ.get()`` everywhere queue_worker
reads its own configuration.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT  = Path(__file__).resolve().parents[2]
QUEUE_DIR     = PROJECT_ROOT / 'queue'
RUNNING_DIR   = QUEUE_DIR / 'running'
LOG_DIR       = PROJECT_ROOT / 'logs'
PROFILES_PATH = PROJECT_ROOT / 'config' / 'profiles.yaml'
STATIC_DIR    = Path(__file__).resolve().parent / 'static'

_logger = None

# Private-key store loaded from .env. Read via get_env(); NEVER mutated into
# os.environ so child processes (claude -p) cannot inherit secrets from .env.
_DOTENV: dict[str, str] = {}


def get_env(key: str, default: str | None = None) -> str | None:
    """Read configuration with `os.environ` precedence over the private .env.

    Process env wins so explicit shell exports and CI/systemd `Environment=`
    always override .env. Falls back to the private .env dict (which is NOT
    in os.environ, see module docstring).
    """
    val = os.environ.get(key)
    if val is not None and val != '':
        return val
    return _DOTENV.get(key, default)


# Env vars that must NEVER reach `claude -p` subprocesses spawned by the
# executor or the usage-check kick. They have no business in subprocess
# space and any leak to a task prompt is a credential exposure.
_SUBPROCESS_SECRET_KEYS = (
    'CLAUDE_SESSION_KEY',
    'CLAUDE_ORG_UUID',
    'QUEUE_WORKER_PASSWORD',
)


def subprocess_env() -> dict[str, str]:
    """Return a copy of `os.environ` with queue-worker secrets stripped.

    Used by both `executor._run_claude` (task subprocesses) and
    `usage_check._kick_via_cli` (the session-kick subprocess). Defense in
    depth on top of `_DOTENV` not polluting `os.environ` in the first place.
    """
    return {k: v for k, v in os.environ.items()
            if k not in _SUBPROCESS_SECRET_KEYS}


def check_secret_file_perms(path: Path) -> str | None:
    """Validate that a secrets-bearing file is safely owned and readable.

    Returns None if OK, or a human-readable error string explaining why the
    file should not be loaded. Used by both the .env loader and the
    session-key file loader; consolidates the symlink/owner/mode rules so
    they can't drift apart.
    """
    if path.is_symlink():
        return f'{path}: refusing to load (is symlink — security)'
    try:
        st = path.lstat()
    except OSError as e:
        return f'{path}: {e}'
    if hasattr(os, 'getuid') and st.st_uid != os.getuid():
        return (f'{path}: owner does not match current user — '
                f'run `chown $USER {path}`')
    if st.st_mode & 0o077:
        return (f'{path}: mode {st.st_mode & 0o777:o} too permissive — '
                f'run `chmod 0600 {path}` (file contains secrets)')
    return None


def _load_dotenv(path: Path = PROJECT_ROOT / '.env') -> None:
    """Load .env into the private _DOTENV dict (NOT os.environ).

    Same secret-file gating as the session-key file: refuses symlinks,
    wrong owner, or mode wider than 0600. Existing process env always
    wins over .env (see get_env).
    """
    if not path.exists():
        return
    err = check_secret_file_perms(path)
    if err:
        sys.stderr.write(f'queue-worker: refusing to load .env — {err}\n')
        return
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            _DOTENV[k] = v


_load_dotenv()


def bootstrap():
    """Inject paths into modules that use module-level globals."""
    from . import lock as lock_mod
    from . import queue_ops as queue_mod
    from . import profiles as prof_mod
    lock_mod.RUNNING_DIR   = RUNNING_DIR
    queue_mod.QUEUE_DIR    = QUEUE_DIR
    prof_mod.PROFILES_PATH = PROFILES_PATH


def get_logger():
    """Return a cached TaskLogger instance."""
    global _logger
    if _logger is None:
        from .logger import TaskLogger
        _logger = TaskLogger(LOG_DIR)
    return _logger
