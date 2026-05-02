"""Locate and summarize Codex session transcripts on disk.

Used by the web dashboard to show which conversation a queued task will
resume. Read-only — the queue only stores session_id in task YAML.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional


CODEX_HOME = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
_SESSIONS_DIR = CODEX_HOME / 'sessions'
_STATE_DB = CODEX_HOME / 'state_5.sqlite'
_HEAD_BYTES = 64 * 1024
_HEAD_LINES = 100
_TAIL_BYTES = 128 * 1024
_TAIL_LINES = 200
_MSG_CHARS = 300
_META_SCAN_LINES = 50

_SESSION_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

_SYNTHETIC_PREFIXES = (
    'You have been resumed by codex-queue',
    'You have been started by codex-queue',
)


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id or ''))


def _cwds_match(transcript_cwd: str, resolved_dir: str) -> bool:
    if transcript_cwd == resolved_dir:
        return True
    try:
        from .task import expand_path
        return expand_path(transcript_cwd) == resolved_dir
    except Exception:
        return False


def _read_session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open('r', encoding='utf-8-sig', errors='replace') as f:
            for i, line in enumerate(f):
                if i >= _META_SCAN_LINES:
                    return {}
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get('type') == 'session_meta':
                    payload = obj.get('payload')
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def _candidate_from_db(resolved_dir: str, session_id: str) -> Optional[Path]:
    if not _STATE_DB.exists():
        return None
    try:
        con = sqlite3.connect(f'file:{_STATE_DB}?mode=ro', uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            'select rollout_path, cwd from threads where id = ?',
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    rollout_path, cwd = row
    if not isinstance(rollout_path, str) or not isinstance(cwd, str):
        return None
    if not _cwds_match(cwd, resolved_dir):
        return None
    path = Path(rollout_path).expanduser()
    return path if path.exists() else None


def _candidate_from_scan(resolved_dir: str, session_id: str) -> Optional[Path]:
    if not _SESSIONS_DIR.exists():
        return None
    pattern = f'*{session_id}.jsonl'
    for candidate in _SESSIONS_DIR.glob(f'**/{pattern}'):
        meta = _read_session_meta(candidate)
        cwd = meta.get('cwd')
        sid = meta.get('id')
        if sid != session_id:
            continue
        if isinstance(cwd, str) and _cwds_match(cwd, resolved_dir):
            return candidate
    return None


def find_transcript(resolved_dir: str, session_id: str) -> Optional[Path]:
    """Return the Codex JSONL transcript path for (cwd, session_id), or None."""
    if not is_valid_session_id(session_id):
        return None
    return (
        _candidate_from_db(resolved_dir, session_id)
        or _candidate_from_scan(resolved_dir, session_id)
    )


def project_dir_for(_resolved_dir: str) -> Path:
    """Directory where Codex JSONL transcripts are normally stored."""
    return _SESSIONS_DIR


def _content_to_text(content) -> Optional[str]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get('text'), str):
            parts.append(item['text'])
    return '\n'.join(parts) if parts else None


def _extract_user_text(obj: dict) -> Optional[str]:
    if obj.get('type') != 'response_item':
        return None
    payload = obj.get('payload')
    if not isinstance(payload, dict) or payload.get('role') != 'user':
        return None
    text = _content_to_text(payload.get('content'))
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith('<environment_context>'):
        return None
    if any(stripped.startswith(p) for p in _SYNTHETIC_PREFIXES):
        return None
    return stripped


def _trim(text: str) -> str:
    return ' '.join(text.split())[:_MSG_CHARS]


def _scan_for_user_messages(lines: list[str]) -> tuple[Optional[str], Optional[str], int]:
    first: Optional[str] = None
    last: Optional[str] = None
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _extract_user_text(obj)
        if text is None:
            continue
        count += 1
        if first is None:
            first = text
        last = text
    return first, last, count


def _read_head(path: Path) -> list[str]:
    out: list[str] = []
    bytes_read = 0
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            bytes_read += len(line.encode('utf-8', errors='ignore'))
            out.append(line)
            if len(out) >= _HEAD_LINES or bytes_read >= _HEAD_BYTES:
                break
    return out


def _read_tail(path: Path) -> list[str]:
    with path.open('rb') as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return []
        start = max(0, size - _TAIL_BYTES)
        f.seek(start)
        chunk = f.read()
    text = chunk.decode('utf-8', errors='replace')
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-_TAIL_LINES:]


def summarize_session(path: Path) -> dict[str, Any]:
    """Read head + tail of a transcript and return a dashboard summary."""
    head_lines = _read_head(path)
    first, head_last, head_count = _scan_for_user_messages(head_lines)

    file_size = path.stat().st_size
    if file_size <= _HEAD_BYTES:
        last = head_last
        msg_count = head_count
    else:
        tail_lines = _read_tail(path)
        _, tail_last, _ = _scan_for_user_messages(tail_lines)
        last = tail_last or head_last
        msg_count = head_count

    return {
        'transcript_path': str(path),
        'message_count': msg_count,
        'first_user_message': _trim(first) if first else '',
        'last_user_message': _trim(last) if last else '',
    }
