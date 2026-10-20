"""Start the server on 127.0.0.1 and check a real chunked upload.

This is separate from pytest. It binds a socket, uploads through HTTP, and
prints the addresses that are actually listening.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "live-check-pass-1"
CHUNK = 256 * 1024


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def tcp_open(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.8)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def lan_addresses() -> list[str]:
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | "
        "Select-Object -ExpandProperty IPAddress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=0x08000000,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def listeners(port: int) -> list[str]:
    script = (
        "[System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()."
        f"GetActiveTcpListeners() | Where-Object {{ $_.Port -eq {port} }} | "
        "ForEach-Object { $_.Address.ToString() }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=0x08000000,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="phoneuploads-live-"))
    port = find_free_port()
    password_hash = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(PASSWORD)
    env = os.environ.copy()
    env.update(
        {
            "PHONEUPLOADS_ENV_FILE": "off",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "STORAGE_DIR": str(work / "storage"),
            "DATA_DIR": str(work / "data"),
            "LOG_DIR": str(work / "logs"),
            "SESSION_SECRET": "live-check-session-secret-0123456789abcdef",
            "PASSWORD_HASH": password_hash,
            "CHUNK_SIZE_BYTES": str(CHUNK),
            "MAX_UPLOAD_BYTES": str(20 * 1024 * 1024),
            "COOKIE_SECURE": "false",
            "APP_USERNAME": "owner",
            "PYTHONUTF8": "1",
        }
    )
    python = Path(sys.executable)
    output_path = work / "process.log"
    output = output_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(python), str(ROOT / "app.py")],
        cwd=str(ROOT),
        env=env,
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        wait_for_health(base, process)
        bound = listeners(port)
        print("LISTEN", ", ".join(bound) or "(none)")
        if bound != ["127.0.0.1"]:
            raise RuntimeError(f"Expected only 127.0.0.1, got {bound}")
        print("LOCAL", "open" if tcp_open("127.0.0.1", port) else "closed")
        for address in lan_addresses():
            state = "open" if tcp_open(address, port) else "closed"
            print("LAN", address, state)
            if state == "open":
                raise RuntimeError(f"Port {port} was reachable on LAN address {address}")
        payload = os.urandom(900_000)
        saved = upload_and_check(base, payload)
        print("UPLOAD", saved, "sha256", hashlib.sha256(payload).hexdigest())
        print("LIVE CHECK PASSED")
        return 0
    except Exception as exc:
        print("LIVE CHECK FAILED:", exc)
        log = work / "logs" / "server.log"
        if log.exists():
            print(log.read_text(encoding="utf-8")[-4000:])
        return 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        output.close()
        if output_path.exists():
            text = output_path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                print(text[-2000:])


def wait_for_health(base: str, process: subprocess.Popen) -> None:
    deadline = time.time() + 20
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Server exited before the health check succeeded. See process.log and server.log.")
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.3)
    raise RuntimeError(f"Health check timed out: {last_error}")


def upload_and_check(base: str, payload: bytes) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login_page = opener.open(base + "/login", timeout=10).read().decode("utf-8")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page)
    if not token:
        raise RuntimeError("Login page did not contain a CSRF token.")
    body = urllib.parse.urlencode({"csrf_token": token.group(1), "password": PASSWORD}).encode()
    request = urllib.request.Request(base + "/login", data=body, method="POST")
    try:
        opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code not in {303, 302}:
            raise
    home = opener.open(base + "/", timeout=10).read().decode("utf-8")
    boot = re.search(r'<script id="boot" type="application/json">(.*?)</script>', home, re.DOTALL)
    if not boot:
        raise RuntimeError("Upload page did not contain configuration.")
    csrf = json.loads(boot.group(1))["csrfToken"]
    created = request_json(
        opener,
        base + "/api/uploads",
        {"filename": "live.mp4", "size": len(payload), "client_key": "live"},
        csrf,
    )
    offset = 0
    while offset < len(payload):
        piece = payload[offset : offset + CHUNK]
        put = urllib.request.Request(
            f"{base}/api/uploads/{created['upload_id']}/chunk?offset={offset}",
            data=piece,
            method="PUT",
            headers={"X-CSRF-Token": csrf},
        )
        with opener.open(put, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        offset = result["offset"]
    finished = request_json(opener, f"{base}/api/uploads/{created['upload_id']}/complete", {}, csrf)
    with opener.open(base + "/download/" + urllib.parse.quote(finished["name"]), timeout=30) as response:
        downloaded = response.read()
    if downloaded != payload:
        raise RuntimeError("Downloaded bytes did not match the upload.")
    return finished["name"]


def request_json(opener, url: str, payload: dict, csrf: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
    )
    with opener.open(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
