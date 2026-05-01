import logging
from pathlib import Path
from datetime import datetime
from typing import Optional


class TaskLogger:
    """
    Writes to both stdout and a rolling daily log file.
    File handler is cached and rotated on date change.
    """

    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = log_dir
        self._current_date: str = ''
        self._file_handler: logging.FileHandler | None = None
        self._setup()

    def _setup(self):
        self._logger = logging.getLogger('queue_worker')
        self._logger.setLevel(logging.DEBUG)
        if not self._logger.handlers:
            fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s',
                                    datefmt='%Y-%m-%d %H:%M:%S')
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            self._logger.addHandler(sh)

    def _ensure_file_handler(self):
        """Ensure the file handler points to today's log. Rotate on date change."""
        date_str = datetime.now().strftime('%Y-%m-%d')
        if date_str != self._current_date:
            if self._file_handler:
                self._logger.removeHandler(self._file_handler)
                self._file_handler.close()
            log_path = self._log_dir / f"{date_str}.log"
            self._file_handler = logging.FileHandler(log_path, encoding='utf-8')
            self._file_handler.setFormatter(logging.Formatter(
                '[%(asctime)s] [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'))
            self._logger.addHandler(self._file_handler)
            self._current_date = date_str

    def _log(self, level: int, msg: str):
        self._ensure_file_handler()
        self._logger.log(level, msg)

    def info(self, msg: str):   self._log(logging.INFO,    msg)
    def warn(self, msg: str):   self._log(logging.WARNING, msg)
    def error(self, msg: str):  self._log(logging.ERROR,   msg)

    def task(self, task_id: str, msg: str):
        self._log(logging.INFO, f"[task:{task_id}] {msg}")


def read_log_lines(log_dir: Path, date: Optional[str] = None,
                   task_id: Optional[str] = None) -> tuple[str, list[str]]:
    """Read a daily log file, optionally filtered to lines tagged with a
    given task ID. Returns (date_str, lines). Lines are returned with
    trailing newlines stripped. If the file doesn't exist, lines is empty.
    """
    date_str = date or datetime.now().strftime('%Y-%m-%d')
    log_file = log_dir / f'{date_str}.log'
    if not log_file.exists():
        return date_str, []
    needle = f'[task:{task_id}]' if task_id else None
    lines: list[str] = []
    with open(log_file) as f:
        for line in f:
            if needle and needle not in line:
                continue
            lines.append(line.rstrip())
    return date_str, lines
