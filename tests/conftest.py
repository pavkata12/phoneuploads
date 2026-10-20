import os
import shutil
import tempfile
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from support import PASSWORD
ROOT = Path(tempfile.mkdtemp(prefix="phoneuploads-test-"))

os.environ["PHONEUPLOADS_ENV_FILE"] = "off"
os.environ["STORAGE_DIR"] = str(ROOT / "storage")
os.environ["DATA_DIR"] = str(ROOT / "data")
os.environ["LOG_DIR"] = str(ROOT / "logs")
os.environ["SESSION_SECRET"] = "test-session-secret-value-0123456789abcdef"
os.environ["HOST"] = "127.0.0.1"
os.environ["PORT"] = "8799"
os.environ["CHUNK_SIZE_BYTES"] = "262144"
os.environ["MAX_UPLOAD_BYTES"] = str(8 * 1024 * 1024)
os.environ["LOGIN_MAX_ATTEMPTS"] = "3"
os.environ["LOGIN_WINDOW_SECONDS"] = "900"
os.environ["LOGIN_LOCKOUT_SECONDS"] = "900"
os.environ["COOKIE_SECURE"] = "false"
os.environ["APP_USERNAME"] = "owner"
os.environ["PASSWORD_HASH"] = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(PASSWORD)

import app as app_module
from app import app
from settings import settings
from storage import ensure_dirs


def _wipe() -> None:
    ensure_dirs()
    for folder in (settings.storage_dir, settings.data_dir, settings.log_dir):
        if not folder.exists():
            continue
        for child in list(folder.iterdir()):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                continue
            try:
                child.unlink(missing_ok=True)
            except PermissionError:
                # The server log stays open for the whole test process on Windows.
                continue
    ensure_dirs()
    app_module.guard.reset()


@pytest.fixture(autouse=True)
def clean_state():
    _wipe()
    yield
    _wipe()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client

