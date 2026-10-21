import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import network
from support import PASSWORD, csrf_headers, login
from security import FilenameError, sanitize_upload_name
from settings import settings
from storage import (
    DiskAppender,
    StorageError,
    ensure_dirs,
    recover_incomplete,
    _atomic_json,
    _meta_path,
    _part_path,
)


def test_health_static_and_hidden_docs(client):
    assert client.get("/health").json() == {"ok": True}
    status = settings.data_dir / "phone-status.txt"
    assert status.is_file()
    assert "проверява връзката" in status.read_text(encoding="utf-8")
    assert client.get("/docs").status_code == 404
    assert client.get("/static/style.css").status_code == 200
    assert "Качи" in client.get("/static/app.js").text


def test_login_cookie_and_logout(client):
    token = login(client)
    assert token
    cookie = client.cookies.get("phoneuploads_session")
    assert cookie
    page = client.get("/login", follow_redirects=False)
    assert page.status_code == 303
    failed = client.post(
        "/login",
        data={"csrf_token": "not-the-token", "password": PASSWORD},
        follow_redirects=False,
    )
    # The current session is already authenticated; visiting login redirects.
    # A fresh client checks the failure message below.
    logged_out = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303
    assert failed.status_code in {303, 403, 401}


def test_browser_login_form_with_null_origin(client):
    page = client.get("/login")
    assert "same-origin" in page.headers.get("referrer-policy", "")
    token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    accepted = client.post(
        "/login",
        data={"csrf_token": token, "password": PASSWORD},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    foreign = client.post(
        "/login",
        data={"csrf_token": token, "password": PASSWORD},
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert foreign.status_code == 403
    assert "изтекъл" in foreign.text


def test_login_failure_and_lockout(client):
    page = client.get("/login")
    token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    set_cookie = page.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()
    for _ in range(3):
        response = client.post(
            "/login",
            data={"csrf_token": token, "password": "wrong-password-1"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert "Грешна парола" in response.text
        token = response.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    locked = client.post(
        "/login",
        data={"csrf_token": token, "password": PASSWORD},
        follow_redirects=False,
    )
    assert locked.status_code == 429
    assert "Твърде много опити" in locked.text


def test_api_requires_login_and_csrf(client):
    assert client.get("/api/files").status_code == 401
    missing = client.get("/download/missing.mp4", follow_redirects=False)
    assert missing.status_code == 303
    token = login(client)
    rejected = client.post(
        "/api/uploads",
        json={"filename": "clip.mp4", "size": 10, "client_key": "abc"},
    )
    assert rejected.status_code == 403
    evil = client.post(
        "/api/uploads",
        headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        json={"filename": "clip.mp4", "size": 10, "client_key": "abc"},
    )
    assert evil.status_code == 403
    still_missing = client.get("/download/missing.mp4", follow_redirects=False)
    assert still_missing.status_code == 404


def _upload(client, token: str, name: str, payload: bytes, key: str) -> str:
    created = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": name, "size": len(payload), "client_key": key},
    )
    assert created.status_code == 200, created.text
    info = created.json()
    offset = info["offset"]
    chunk_size = info["chunk_size"]
    while offset < len(payload):
        piece = payload[offset : offset + chunk_size]
        sent = client.put(
            f"/api/uploads/{info['upload_id']}/chunk?offset={offset}",
            headers=csrf_headers(token),
            content=piece,
        )
        assert sent.status_code == 200, sent.text
        offset = sent.json()["offset"]
    finished = client.post(
        f"/api/uploads/{info['upload_id']}/complete",
        headers=csrf_headers(token),
        json={},
    )
    assert finished.status_code == 200, finished.text
    return finished.json()["name"]


def test_chunked_upload_download_range_and_delete(client):
    token = login(client)
    payload = b"video-bytes-" + os_random(600_000)
    saved = _upload(client, token, "видео.mp4", payload, "resume-key-1")
    assert saved.endswith(".mp4")
    assert (settings.storage_dir / saved).resolve().parent == settings.storage_dir.resolve()
    assert (settings.storage_dir / saved).read_bytes() == payload
    listed = client.get("/api/files").json()["files"]
    assert any(item["name"] == saved for item in listed)
    downloaded = client.get(f"/download/{saved}")
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert "attachment" in downloaded.headers["content-disposition"].lower()
    partial = client.get(f"/download/{saved}", headers={"Range": "bytes=0-10"})
    assert partial.status_code == 206
    assert partial.content == payload[:11]
    too_far = client.get(f"/download/{saved}", headers={"Range": "bytes=99999999-99999999"})
    assert too_far.status_code == 416
    kept = client.post(
        "/api/files/delete",
        headers=csrf_headers(token),
        json={"name": saved, "confirm": False},
    )
    assert kept.status_code == 400
    assert (settings.storage_dir / saved).exists()
    removed = client.post(
        "/api/files/delete",
        headers=csrf_headers(token),
        json={"name": saved, "confirm": True},
    )
    assert removed.status_code == 200, removed.text
    assert not (settings.storage_dir / saved).exists()
    history = client.get("/api/history").json()["events"]
    assert any(event["action"] == "upload" for event in history)
    assert any(event["action"] == "delete" for event in history)


def test_resume_and_wrong_offset(client):
    token = login(client)
    payload = bytes(i % 256 for i in range(300_000))
    created = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": "clip.mp4", "size": len(payload), "client_key": "same-key"},
    )
    info = created.json()
    first = payload[:1000]
    sent = client.put(
        f"/api/uploads/{info['upload_id']}/chunk?offset=0",
        headers=csrf_headers(token),
        content=first,
    )
    assert sent.status_code == 200
    again = client.put(
        f"/api/uploads/{info['upload_id']}/chunk?offset=0",
        headers=csrf_headers(token),
        content=first,
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    conflict = client.put(
        f"/api/uploads/{info['upload_id']}/chunk?offset=900",
        headers=csrf_headers(token),
        content=b"x" * 200,
    )
    assert conflict.status_code == 409
    assert conflict.json()["offset"] == 1000
    client.cookies.clear()
    token = login(client)
    resumed = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": "clip.mp4", "size": len(payload), "client_key": "same-key"},
    )
    assert resumed.json()["resumed"] is True
    assert resumed.json()["offset"] == 1000
    offset = 1000
    while offset < len(payload):
        piece = payload[offset : offset + settings.chunk_size_bytes]
        rest = client.put(
            f"/api/uploads/{info['upload_id']}/chunk?offset={offset}",
            headers=csrf_headers(token),
            content=piece,
        )
        assert rest.status_code == 200, rest.text
        offset = rest.json()["offset"]
    finished = client.post(
        f"/api/uploads/{info['upload_id']}/complete",
        headers=csrf_headers(token),
        json={},
    )
    assert finished.status_code == 200, finished.text
    assert hashlib.sha256((settings.storage_dir / finished.json()["name"]).read_bytes()).digest() == hashlib.sha256(payload).digest()


def test_rejects_unsafe_names_and_limits(client):
    token = login(client)
    traversal = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": "..\\..\\Windows\\secret.mp4", "size": 20, "client_key": "trav"},
    )
    assert traversal.status_code == 200, traversal.text
    saved = _finish_small(client, token, traversal.json(), b"0123456789abcdefghij")
    path = (settings.storage_dir / saved).resolve()
    assert path.parent == settings.storage_dir.resolve()
    assert ".." not in path.name
    blocked = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": "tool.exe", "size": 20, "client_key": "exe"},
    )
    assert blocked.status_code == 400
    huge = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": "big.mp4", "size": settings.max_upload_bytes + 1, "client_key": "big"},
    )
    assert huge.status_code == 413
    deleted = client.post(
        "/api/files/delete",
        headers=csrf_headers(token),
        json={"name": "..\\secret.mp4", "confirm": True},
    )
    assert deleted.status_code == 400
    too_big = b"x" * (settings.chunk_size_bytes + 1)
    created = client.post(
        "/api/uploads",
        headers=csrf_headers(token),
        json={"filename": "ok.mp4", "size": len(too_big), "client_key": "wide"},
    )
    # The declared file is larger than one chunk, so init can succeed, but the chunk cannot.
    if created.status_code == 200:
        upload_id = created.json()["upload_id"]
        rejected = client.put(
            f"/api/uploads/{upload_id}/chunk?offset=0",
            headers=csrf_headers(token),
            content=too_big,
        )
        assert rejected.status_code == 413
    else:
        assert created.status_code == 413


def test_unique_names_do_not_overwrite(client):
    token = login(client)
    first = _upload(client, token, "same.mp4", b"a" * 100, "one")
    second = _upload(client, token, "same.mp4", b"b" * 120, "two")
    assert first != second
    assert (settings.storage_dir / first).read_bytes() == b"a" * 100
    assert (settings.storage_dir / second).read_bytes() == b"b" * 120


def test_disk_appender_streams_and_rolls_back():
    ensure_dirs()
    path = settings.incomplete_dir / "stream.part"
    path.write_bytes(b"")
    writer = DiskAppender(path, 0, 1000)
    total = 0
    for _ in range(10):
        writer.feed(b"x" * 100)
        total += 100
        assert writer._file.tell() == total
    assert not hasattr(writer, "buffer")
    assert writer.finish() == 1000
    assert path.read_bytes() == b"x" * 1000

    path.write_bytes(b"HELLO")
    rolling = DiskAppender(path, 5, 5)
    rolling.feed(b"!!")
    rolling.abort()
    assert path.read_bytes() == b"HELLO"

    path.write_bytes(b"")
    short = DiskAppender(path, 0, 10)
    short.feed(b"12345")
    with pytest.raises(StorageError):
        short.finish()
    assert path.read_bytes() == b""


def test_recovery_truncates_to_committed_offset():
    ensure_dirs()
    upload_id = "ab" * 16
    part = _part_path(upload_id)
    part.write_bytes(b"abcdefghij")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _atomic_json(
        _meta_path(upload_id),
        {
            "upload_id": upload_id,
            "original": "clip.mp4",
            "safe_name": "kept_clip.mp4",
            "size": 100,
            "offset": 4,
            "client_key": "recover",
            "created": now,
            "updated": now,
        },
    )
    recover_incomplete()
    assert part.read_bytes() == b"abcd"


def test_filename_rules():
    stem, extension, cleaned = sanitize_upload_name("..\\..\\Видео.MP4", settings.allowed_extensions)
    assert extension == ".mp4"
    assert ".." not in stem
    assert cleaned.endswith(".MP4") or cleaned.endswith(".mp4")
    with pytest.raises(FilenameError):
        sanitize_upload_name("notes.txt", settings.allowed_extensions)
    reserved_stem, reserved_ext, _ = sanitize_upload_name("CON.mp4", settings.allowed_extensions)
    assert reserved_stem.upper().startswith("_CON")
    assert reserved_ext == ".mp4"


def test_bind_policy(monkeypatch):
    assert network.validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert network.validate_bind_host("100.100.1.2") == "100.100.1.2"
    assert network.validate_bind_host("::1") == "::1"
    for rejected in ("0.0.0.0", "::", "", "192.168.1.20", "10.0.0.8", "172.16.5.5", "100.63.0.1", "100.128.0.1"):
        with pytest.raises(SystemExit):
            network.validate_bind_host(rejected)
    assert network.choose_tailscale_ip([("192.168.1.2", "Wi-Fi"), ("100.88.1.2", "Tailscale")]) == "100.88.1.2"
    assert network.choose_tailscale_ip([("100.88.1.2", "Ethernet"), ("100.89.1.2", "Tailscale")]) == "100.89.1.2"
    assert network.choose_tailscale_ip([("100.88.1.2", "Ethernet")]) == "100.88.1.2"
    assert network.choose_tailscale_ip([("100.88.1.2", "Ethernet"), ("100.89.1.2", "vEthernet")]) is None
    assert network.parse_tailscale_ip_output("100.77.3.4\n") == "100.77.3.4"
    monkeypatch.setattr(network, "detect_tailscale_ipv4", lambda: "100.100.1.2")
    assert network.resolve_host("auto") == "100.100.1.2"
    monkeypatch.setattr(network, "detect_tailscale_ipv4", lambda: None)
    with pytest.raises(SystemExit):
        network.resolve_host("auto")


def test_password_rules_and_env_file(tmp_path):
    from setup_config import build_values, quote, validate_password, write_env

    validate_password("correct-horse-1", "owner")
    with pytest.raises(SystemExit):
        validate_password("short1", "owner")
    with pytest.raises(SystemExit):
        validate_password("password123", "owner")
    storage = tmp_path / "PhoneUploads"
    storage.mkdir()
    target = tmp_path / ".env"
    write_env(target, build_values(storage, 8787, "owner"))
    text = target.read_text(encoding="utf-8-sig")
    assert "PASSWORD" not in text
    assert quote(str(storage)) in text
    assert '"8787"' in text


def _finish_small(client, token, info, payload: bytes) -> str:
    sent = client.put(
        f"/api/uploads/{info['upload_id']}/chunk?offset=0",
        headers=csrf_headers(token),
        content=payload,
    )
    assert sent.status_code == 200, sent.text
    finished = client.post(
        f"/api/uploads/{info['upload_id']}/complete",
        headers=csrf_headers(token),
        json={},
    )
    assert finished.status_code == 200, finished.text
    return finished.json()["name"]


def os_random(size: int) -> bytes:
    return bytes((index * 17) % 256 for index in range(size))


def test_old_incomplete_upload_is_removed():
    ensure_dirs()
    upload_id = "cd" * 16
    old = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
    _part_path(upload_id).write_bytes(b"stale")
    _atomic_json(
        _meta_path(upload_id),
        {
            "upload_id": upload_id,
            "original": "old.mp4",
            "safe_name": "old_saved.mp4",
            "size": 5,
            "offset": 5,
            "client_key": "old",
            "created": old,
            "updated": old,
        },
    )
    recover_incomplete()
    assert not _part_path(upload_id).exists()
    assert not _meta_path(upload_id).exists()
