"""File browser helpers — pure functions, no FastAPI imports."""

import os
import stat
from pathlib import Path
from typing import Literal

# Extensions we can preview as images, with their MIME types. Single source
# of truth for classification and raw-serving. PDF lives separately because
# it has its own render path (<embed>).
#
# .svg is deliberately absent: inline same-origin SVG is a stored-XSS vector
# if any task writes <script>-bearing SVG into its workspace. SVG files still
# appear in listings and render as text source.
IMAGE_MIMES: dict[str, str] = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif",
}

TEXT_SIZE_CAP = 256 * 1024
RAW_SIZE_CAP = 50 * 1024 * 1024
LIST_ENTRY_CAP = 1000
BINARY_PROBE_BYTES = 8192


def classify_file(name: str) -> Literal["text", "image", "pdf"]:
    """Classify by extension. Anything that isn't a known image or PDF is
    attempted as text; the null-byte probe in read_text_file catches files
    that aren't actually text.
    """
    ext = Path(name).suffix.lower()
    if ext in IMAGE_MIMES:
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "text"


def normalize_path(p: str) -> Path:
    return Path(p).expanduser().resolve(strict=False)


def _is_dir_safe(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def list_directory(path: Path) -> dict:
    result = {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "entries": [],
        "truncated": False,
    }

    children: list[Path] = []
    try:
        for child in path.iterdir():
            children.append(child)
            if len(children) >= LIST_ENTRY_CAP:
                result["truncated"] = True
                break
    except PermissionError:
        return result

    children.sort(key=lambda p: (not _is_dir_safe(p), p.name.lower()))

    for child in children:
        is_dir = _is_dir_safe(child)
        try:
            size = os.lstat(str(child)).st_size
            denied = False
        except OSError:
            size = None
            denied = True

        if is_dir:
            result["entries"].append({
                "name": child.name, "path": str(child),
                "type": "dir", "kind": None, "size": None, "denied": denied,
            })
        else:
            result["entries"].append({
                "name": child.name, "path": str(child),
                "type": "file", "kind": classify_file(child.name),
                "size": size, "denied": denied,
            })

    return result


def read_text_file(path: Path) -> dict:
    """Read a file as UTF-8 text. Trust the null-byte probe to reject files
    that aren't really text, even if the caller expected them to be.
    """
    base = {"path": str(path), "name": path.name, "kind": "text", "size": None}

    try:
        st = os.stat(str(path))
    except FileNotFoundError:
        return {**base, "reason": "not_regular", "content": None}
    except OSError:
        return {**base, "reason": "denied", "content": None}

    base["size"] = st.st_size

    if not stat.S_ISREG(st.st_mode):
        return {**base, "reason": "not_regular", "content": None}

    if st.st_size > TEXT_SIZE_CAP:
        return {**base, "reason": "too_large", "content": None}

    try:
        with open(path, "rb") as f:
            probe = f.read(BINARY_PROBE_BYTES)
            if b"\x00" in probe:
                return {**base, "reason": "binary", "content": None}
            # Bound the tail read in case the file grew since stat.
            # +1 so an over-cap result is detectable.
            remaining = TEXT_SIZE_CAP - len(probe) + 1
            rest = f.read(remaining) if remaining > 0 else b""
    except OSError:
        return {**base, "reason": "denied", "content": None}

    if len(probe) + len(rest) > TEXT_SIZE_CAP:
        return {**base, "reason": "too_large", "content": None}

    payload = probe + rest
    # Re-check across the full payload — a text-looking prefix can hide
    # a binary tail past the 8 KB probe.
    if b"\x00" in rest:
        return {**base, "reason": "binary", "content": None}

    content = payload.decode("utf-8", errors="replace")
    return {**base, "reason": "ok", "content": content}


def get_raw_mime(path: Path) -> str | None:
    """Return the MIME type if the file can be streamed as image or PDF,
    else None (unsupported, non-regular, missing, or over the size cap).
    """
    try:
        st = os.stat(str(path))
    except OSError:
        return None

    if not stat.S_ISREG(st.st_mode) or st.st_size > RAW_SIZE_CAP:
        return None

    ext = path.suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    return IMAGE_MIMES.get(ext)
