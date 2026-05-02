"""Tests for file_browser.py — pure function tests using tmp_path."""

import os

from queue_worker.file_browser import (
    classify_file,
    normalize_path,
    list_directory,
    read_text_file,
    get_raw_mime,
    TEXT_SIZE_CAP,
    LIST_ENTRY_CAP,
)


# ── classify_file ──


class TestClassifyFile:
    def test_text_extensions(self):
        assert classify_file("main.py") == "text"
        assert classify_file("readme.md") == "text"
        assert classify_file("config.yaml") == "text"
        assert classify_file("style.css") == "text"
        assert classify_file("app.tsx") == "text"
        assert classify_file(".gitignore") == "text"

    def test_text_case_insensitive(self):
        assert classify_file("README.MD") == "text"
        assert classify_file("script.PY") == "text"

    def test_special_names(self):
        assert classify_file("Makefile") == "text"
        assert classify_file("Dockerfile") == "text"
        assert classify_file("LICENSE") == "text"
        assert classify_file("Gemfile") == "text"

    def test_image_extensions(self):
        assert classify_file("logo.png") == "image"
        assert classify_file("photo.jpg") == "image"
        assert classify_file("pic.JPEG") == "image"

    def test_svg_is_text(self):
        # SVG is intentionally classified as text — inline same-origin SVG
        # is a stored-XSS vector, so it falls back to source-view.
        assert classify_file("icon.svg") == "text"

    def test_pdf(self):
        assert classify_file("doc.pdf") == "pdf"
        assert classify_file("report.PDF") == "pdf"

    def test_unknown_extensions_default_to_text(self):
        # No "unsupported" classification — anything not image/pdf is tried as
        # text, and the null-byte probe in read_text_file catches real binaries.
        assert classify_file("archive.zip") == "text"
        assert classify_file("binary.exe") == "text"
        assert classify_file("data.bin") == "text"
        assert classify_file("unknown") == "text"


# ── normalize_path ──


class TestNormalizePath:
    def test_tilde_expansion(self):
        p = normalize_path("~/test")
        assert str(p).startswith("/")
        assert "~" not in str(p)

    def test_absolute_path(self, tmp_path):
        p = normalize_path(str(tmp_path))
        assert p == tmp_path


# ── list_directory ──


class TestListDirectory:
    def test_basic_listing(self, tmp_path):
        (tmp_path / "file.py").write_text("hello")
        (tmp_path / "subdir").mkdir()
        result = list_directory(tmp_path)

        assert result["path"] == str(tmp_path)
        assert result["parent"] == str(tmp_path.parent)
        assert result["truncated"] is False
        assert len(result["entries"]) == 2

        # Dirs first
        assert result["entries"][0]["name"] == "subdir"
        assert result["entries"][0]["type"] == "dir"
        assert result["entries"][0]["kind"] is None

        assert result["entries"][1]["name"] == "file.py"
        assert result["entries"][1]["type"] == "file"
        assert result["entries"][1]["kind"] == "text"
        assert result["entries"][1]["size"] == 5

    def test_hidden_files_shown(self, tmp_path):
        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / ".git").mkdir()
        result = list_directory(tmp_path)
        names = [e["name"] for e in result["entries"]]
        assert ".hidden" in names
        assert ".git" in names

    def test_sorting_dirs_first_alpha(self, tmp_path):
        (tmp_path / "zebra.txt").write_text("")
        (tmp_path / "alpha.txt").write_text("")
        (tmp_path / "beta_dir").mkdir()
        (tmp_path / "alpha_dir").mkdir()
        result = list_directory(tmp_path)
        names = [e["name"] for e in result["entries"]]
        assert names == ["alpha_dir", "beta_dir", "alpha.txt", "zebra.txt"]

    def test_truncation(self, tmp_path):
        for i in range(LIST_ENTRY_CAP + 10):
            (tmp_path / f"file_{i:05d}.txt").write_text("")
        result = list_directory(tmp_path)
        assert result["truncated"] is True
        assert len(result["entries"]) == LIST_ENTRY_CAP

    def test_empty_dir(self, tmp_path):
        result = list_directory(tmp_path)
        assert result["entries"] == []
        assert result["truncated"] is False

    def test_entries_have_full_path(self, tmp_path):
        (tmp_path / "test.py").write_text("")
        result = list_directory(tmp_path)
        assert result["entries"][0]["path"] == str(tmp_path / "test.py")

    def test_file_kind_classification(self, tmp_path):
        (tmp_path / "code.py").write_text("")
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        (tmp_path / "doc.pdf").write_bytes(b"%PDF")
        (tmp_path / "data.bin").write_bytes(b"\x00\x01")
        result = list_directory(tmp_path)
        kinds = {e["name"]: e["kind"] for e in result["entries"]}
        assert kinds["code.py"] == "text"
        assert kinds["image.png"] == "image"
        assert kinds["doc.pdf"] == "pdf"
        # Unknown extensions classify as text; the binary probe in
        # read_text_file catches actual binaries at read time.
        assert kinds["data.bin"] == "text"

    def test_broken_symlink(self, tmp_path):
        target = tmp_path / "missing_target"
        link = tmp_path / "broken_link"
        link.symlink_to(target)
        result = list_directory(tmp_path)
        assert len(result["entries"]) == 1
        e = result["entries"][0]
        assert e["name"] == "broken_link"
        assert e["type"] == "file"
        # Broken symlink (no extension) classifies as text by default.
        assert e["kind"] == "text"


# ── read_text_file ──


class TestReadTextFile:
    def test_ok(self, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("print('hi')\n")
        result = read_text_file(f)
        assert result["reason"] == "ok"
        assert result["content"] == "print('hi')\n"
        assert result["name"] == "hello.py"
        assert result["size"] == f.stat().st_size

    def test_too_large(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_bytes(b"x" * (TEXT_SIZE_CAP + 1))
        result = read_text_file(f)
        assert result["reason"] == "too_large"
        assert result["content"] is None

    def test_binary_detection(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_bytes(b"hello\x00world")
        result = read_text_file(f)
        assert result["reason"] == "binary"
        assert result["content"] is None

    def test_zip_caught_by_binary_probe(self, tmp_path):
        # Unknown extensions are tried as text; the null-byte probe rejects
        # real binaries at read time. (No "unsupported" reason — that
        # classification was removed in favor of the byte-level probe.)
        # A real zip has null bytes after the PK header for length fields.
        f = tmp_path / "archive.zip"
        f.write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00")
        result = read_text_file(f)
        assert result["reason"] == "binary"
        assert result["content"] is None

    def test_utf8_with_replacement(self, tmp_path):
        f = tmp_path / "mixed.txt"
        f.write_bytes(b"hello \xff world")
        result = read_text_file(f)
        assert result["reason"] == "ok"
        assert "\ufffd" in result["content"]

    def test_not_regular_file(self, tmp_path):
        fifo_path = tmp_path / "my_fifo.txt"
        os.mkfifo(str(fifo_path))
        result = read_text_file(fifo_path)
        assert result["reason"] == "not_regular"
        assert result["content"] is None


# ── get_raw_mime ──


class TestGetRawMime:
    def test_image(self, tmp_path):
        f = tmp_path / "logo.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert get_raw_mime(f) == "image/png"

    def test_pdf(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        assert get_raw_mime(f) == "application/pdf"

    def test_text_not_raw(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("print('hi')")
        assert get_raw_mime(f) is None

    def test_unsupported_not_raw(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01")
        assert get_raw_mime(f) is None

    def test_not_regular(self, tmp_path):
        fifo_path = tmp_path / "fifo.png"
        os.mkfifo(str(fifo_path))
        result = get_raw_mime(fifo_path)
        assert result is None
