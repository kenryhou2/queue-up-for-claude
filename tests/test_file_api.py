"""Integration tests for /api/files/* endpoints."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _disable_auth():
    with patch("queue_worker.auth.is_enabled", return_value=False):
        yield


@pytest.fixture
def client():
    os.environ.pop("CODEX_QUEUE_PASSWORD", None)
    from queue_worker.web import app
    return TestClient(app, raise_server_exceptions=False)


class TestFilesListAPI:
    def test_list_directory(self, client, tmp_path):
        (tmp_path / "a.py").write_text("hi")
        (tmp_path / "subdir").mkdir()
        r = client.get(f"/api/files/list?path={tmp_path}")
        assert r.status_code == 200
        j = r.json()
        assert j["path"] == str(tmp_path)
        assert len(j["entries"]) == 2
        assert j["entries"][0]["type"] == "dir"
        assert j["entries"][1]["kind"] == "text"

    def test_list_missing(self, client):
        r = client.get("/api/files/list?path=/nonexistent_dir_xyz")
        assert r.status_code == 404

    def test_list_file_not_dir(self, client, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        r = client.get(f"/api/files/list?path={f}")
        assert r.status_code == 400


class TestFilesReadAPI:
    def test_read_text(self, client, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("print('hi')")
        r = client.get(f"/api/files/read?path={f}")
        assert r.status_code == 200
        j = r.json()
        assert j["reason"] == "ok"
        assert j["content"] == "print('hi')"

    def test_read_missing(self, client):
        r = client.get("/api/files/read?path=/nonexistent_file_xyz.py")
        assert r.status_code == 404

    def test_read_too_large(self, client, tmp_path):
        f = tmp_path / "big.py"
        f.write_bytes(b"x" * (256 * 1024 + 1))
        r = client.get(f"/api/files/read?path={f}")
        assert r.status_code == 200
        assert r.json()["reason"] == "too_large"

    def test_read_binary_file(self, client, tmp_path):
        # Unknown extensions are tried as text; the null-byte probe rejects
        # them at read time with reason='binary'.
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01")
        r = client.get(f"/api/files/read?path={f}")
        assert r.status_code == 200
        assert r.json()["reason"] == "binary"


class TestFilesRawAPI:
    def test_raw_image(self, client, tmp_path):
        f = tmp_path / "logo.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        r = client.get(f"/api/files/raw?path={f}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")

    def test_raw_missing(self, client):
        r = client.get("/api/files/raw?path=/nonexistent_xyz.png")
        assert r.status_code == 404

    def test_raw_text_rejected(self, client, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x = 1")
        r = client.get(f"/api/files/raw?path={f}")
        assert r.status_code == 415
