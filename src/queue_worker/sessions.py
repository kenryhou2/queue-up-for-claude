"""Locate and summarize Claude Code session transcripts on disk.

Used by the web dashboard to show which conversation a queued task will
resume. Read-only — the queue skill stays the only writer of session_id.

Slug rule (verified empirically against ~/.claude/projects/):
  Claude encodes a cwd by replacing every non-[A-Za-z0-9-] char with '-'.
  Leading '/' becomes a leading '-'. Dots become dashes. Existing hyphens
  in the path are preserved as-is, which makes decode genuinely ambiguous —
  so we never decode, we only encode and look up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_PROJECTS_DIR = Path.home() / '.claude' / 'projects'

# Head + tail reads bound the cost on long transcripts. Head finds the first
# user message; tail finds the last. A typical Claude session jsonl is
# 100KB-2MB; transcripts past that exist for long /resume threads.
_HEAD_BYTES = 64 * 1024
_HEAD_LINES = 100
_TAIL_BYTES = 128 * 1024
_TAIL_LINES = 200
_MSG_CHARS = 300

# Cap on how many leading JSONL rows to scan when extracting `cwd` for the
# integrity check. Real transcripts record cwd within the first 3-5 rows;
# 50 is generous for transcripts with extra meta/snapshot lines up front.
_CWD_SCAN_LINES = 50

# Used to gate filesystem lookups — the session_id comes from a YAML field
# the queue skill writes, but the YAML is also editable by users, so an
# invalid id (e.g. "../../etc/passwd") must be rejected before path joins.
_SESSION_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id or ''))

# Synthetic prefixes the executor injects when resuming/spawning a task — these
# are not the human's original prompt and should be skipped when picking the
# first/last user message.
_SYNTHETIC_PREFIXES = (
    'You have been resumed by queue-worker',
    'You have been started by queue-worker',
)


def slugify_cwd(abs_path: str) -> str:
    """Encode a cwd the way Claude encodes it under ~/.claude/projects/."""
    return re.sub(r'[^A-Za-z0-9-]', '-', abs_path)


def _read_first_cwd(path: Path) -> Optional[str]:
    """Scan the head of a JSONL transcript for the first `cwd` field.
    Bounded to _CWD_SCAN_LINES so a malformed transcript can't cost us a
    full file read. Returns None if no cwd is found or the file is unreadable.
    Opens with utf-8-sig so a leading BOM doesn't poison the first json.loads."""
    try:
        with path.open('r', encoding='utf-8-sig', errors='replace') as f:
            for i, line in enumerate(f):
                if i >= _CWD_SCAN_LINES:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get('cwd')
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _cwds_match(transcript_cwd: str, resolved_dir: str) -> bool:
    """True if `transcript_cwd` (recorded by Claude) refers to the same
    directory as `resolved_dir` (already passed through expand_path).
    Handles macOS /tmp ↔ /private/tmp symlink-resolution mismatches via
    a fallback expand_path comparison."""
    if transcript_cwd == resolved_dir:
        return True
    try:
        from .task import expand_path
        return expand_path(transcript_cwd) == resolved_dir
    except Exception:
        return False


def find_transcript(resolved_dir: str, session_id: str) -> Optional[Path]:
    """Return the JSONL transcript path for (cwd, session_id), or None.

    Strategy: try the slug-derived path first (covers >99% of cases), then
    fall back to scanning ~/.claude/projects/*/<uuid>.jsonl for slug
    edge-cases (symlinks, unusual unicode, future Claude encoding changes).
    EVERY match is verified against the transcript's recorded `cwd` field
    before being accepted, so a slug collision or mistyped session_id can't
    surface a different project's conversation.

    Returns None for malformed session_ids (path-traversal guard).
    """
    if not is_valid_session_id(session_id):
        return None

    direct = _PROJECTS_DIR / slugify_cwd(resolved_dir) / f'{session_id}.jsonl'
    if direct.exists():
        cwd = _read_first_cwd(direct)
        if cwd is None or _cwds_match(cwd, resolved_dir):
            # cwd is None for empty/meta-only transcripts where Claude hasn't
            # recorded a cwd yet — accept under the slug match (the directory
            # name itself is the only signal we have, and we got there via
            # the deterministic encode of resolved_dir).
            return direct

    if not _PROJECTS_DIR.exists():
        return None
    for d in _PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        candidate = d / f'{session_id}.jsonl'
        if not candidate.exists():
            continue
        cwd = _read_first_cwd(candidate)
        # On the fallback path the cwd MUST be present and match — without
        # it we have no way to confirm we found the right project.
        if cwd and _cwds_match(cwd, resolved_dir):
            return candidate
    return None


def project_dir_for(resolved_dir: str) -> Path:
    """Where the transcript dir would live if it existed."""
    return _PROJECTS_DIR / slugify_cwd(resolved_dir)


def _extract_user_text(obj: dict) -> Optional[str]:
    """If `obj` is a human user message, return its plain text. Else None.

    Skips:
    - sidechain entries (subagent traffic)
    - tool results / system messages (role != 'user')
    - synthetic kick prompts injected by the executor
    """
    if obj.get('type') != 'user':
        return None
    if obj.get('isSidechain') is True:
        return None
    msg = obj.get('message')
    if not isinstance(msg, dict) or msg.get('role') != 'user':
        return None
    content = msg.get('content')
    text: Optional[str] = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get('text'), str):
            text = first['text']
    if not text:
        return None
    if any(text.startswith(p) for p in _SYNTHETIC_PREFIXES):
        return None
    return text


def _trim(text: str, n: int = _MSG_CHARS) -> str:
    s = ' '.join(text.split())
    return s[:n]


def _scan_for_user_messages(lines: list[str]) -> tuple[Optional[str], Optional[str], int]:
    """Walk JSONL lines, return (first, last, count) of human user messages."""
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
    """First _HEAD_LINES (or _HEAD_BYTES, whichever first) of a JSONL file."""
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
    """Last _TAIL_BYTES of a file as line list. Drops the first (possibly
    truncated) line of the chunk to avoid yielding a partial JSONL row."""
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
        # First line was likely cut mid-record by the seek; drop it.
        lines = lines[1:]
    return lines[-_TAIL_LINES:]


def summarize_session(path: Path) -> dict[str, Any]:
    """Read head + tail of a transcript and return a summary dict.

    For short files head and tail overlap completely — that's fine, we
    dedupe on identity. For long files (multi-MB resume threads), the
    tail pass picks up the genuinely-last user message so the dashboard
    preview reflects what the user most recently said.
    """
    head_lines = _read_head(path)
    first, head_last, head_count = _scan_for_user_messages(head_lines)

    file_size = path.stat().st_size
    if file_size <= _HEAD_BYTES:
        # Whole file fit in head — head_last IS the conversation last.
        last = head_last
        msg_count = head_count
    else:
        tail_lines = _read_tail(path)
        _, tail_last, _ = _scan_for_user_messages(tail_lines)
        last = tail_last or head_last
        # message_count is approximate on long files (head + tail may overlap
        # or skip the middle). Document by rounding up: we know there are at
        # least this many.
        msg_count = head_count

    return {
        'transcript_path': str(path),
        'message_count': msg_count,
        'first_user_message': _trim(first) if first else '',
        'last_user_message': _trim(last) if last else '',
    }
