"""Runtime configuration.

Secrets are not read from source code. Install writes them under ``data\\``,
and tests can supply ``PASSWORD_HASH`` and ``SESSION_SECRET`` in the environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def install_dir() -> Path:
    """Folder that keeps the password, logs, and .env across restarts.

    A packaged exe unpacks into a temporary folder on every launch, so those
    files cannot live next to the code. The installed app uses LocalAppData.
    """
    override = os.environ.get("PHONEUPLOADS_HOME")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "PhoneUploads"
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Folder that contains templates and static files."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent


BASE_DIR = install_dir()
RESOURCE_DIR = resource_dir()

DEFAULT_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".3gp",
    ".m4v",
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".gif",
    ".webp",
)


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs. Existing environment variables win. No interpolation."""
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise SystemExit(f"Configuration {name} must be an integer.") from exc


def _read_secret(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def assert_storage_allowed(path: Path, base_dir: Path) -> Path:
    resolved = path.resolve()
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")).resolve()
    if resolved == windir or windir in resolved.parents:
        raise SystemExit(f"Refusing storage directory inside {windir}.")
    if resolved == base_dir.resolve():
        raise SystemExit(
            "Choose a storage folder that is not the application folder itself."
        )
    return resolved


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    host: str
    port: int
    storage_dir: Path
    data_dir: Path
    log_dir: Path
    max_upload_bytes: int
    chunk_size_bytes: int
    allowed_extensions: frozenset[str]
    session_secret: str
    password_hash: str
    username: str
    session_max_seconds: int
    login_max_attempts: int
    login_window_seconds: int
    login_lockout_seconds: int
    cookie_secure: bool
    incomplete_max_age_seconds: int
    log_max_bytes: int
    log_backup_count: int

    @property
    def incomplete_dir(self) -> Path:
        return self.storage_dir / ".incomplete"

    @property
    def password_file(self) -> Path:
        return self.data_dir / "password.hash"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / "session.secret"

    @property
    def history_file(self) -> Path:
        return self.data_dir / "history.jsonl"

    @property
    def attempts_file(self) -> Path:
        return self.data_dir / "login_attempts.json"

    @property
    def listen_file(self) -> Path:
        return self.data_dir / "listen.json"

    @property
    def pid_file(self) -> Path:
        return self.data_dir / "server.pid"


def load_settings() -> Settings:
    if os.environ.get("PHONEUPLOADS_ENV_FILE") != "off":
        env_override = os.environ.get("PHONEUPLOADS_ENV_FILE")
        env_path = Path(env_override) if env_override else BASE_DIR / ".env"
        load_env_file(env_path)

    data_dir = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
    if not data_dir.is_absolute():
        data_dir = (BASE_DIR / data_dir).resolve()

    log_dir = Path(os.environ.get("LOG_DIR") or (BASE_DIR / "logs"))
    if not log_dir.is_absolute():
        log_dir = (BASE_DIR / log_dir).resolve()

    storage_raw = os.environ.get("STORAGE_DIR") or str(BASE_DIR / "PhoneUploads")
    storage_dir = Path(storage_raw)
    if not storage_dir.is_absolute():
        storage_dir = BASE_DIR / storage_dir
    storage_dir = assert_storage_allowed(storage_dir, BASE_DIR)

    raw_extensions = os.environ.get("ALLOWED_EXTENSIONS") or ",".join(DEFAULT_EXTENSIONS)
    allowed: set[str] = set()
    for item in raw_extensions.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = f".{item}"
        allowed.add(item)
    if not allowed:
        raise SystemExit("ALLOWED_EXTENSIONS must list at least one extension.")

    session_secret = os.environ.get("SESSION_SECRET", "").strip() or _read_secret(
        data_dir / "session.secret"
    )
    password_hash = os.environ.get("PASSWORD_HASH", "").strip() or _read_secret(
        data_dir / "password.hash"
    )

    port = _int("PORT", 8787)
    if not 1024 <= port <= 65535:
        raise SystemExit(
            "PORT must be between 1024 and 65535 so the server can run without administrator rights."
        )

    max_upload = _int("MAX_UPLOAD_BYTES", 20 * 1024 * 1024 * 1024)
    chunk = _int("CHUNK_SIZE_BYTES", 4 * 1024 * 1024)
    if max_upload < 1:
        raise SystemExit("MAX_UPLOAD_BYTES must be positive.")
    if not 256 * 1024 <= chunk <= 32 * 1024 * 1024:
        raise SystemExit("CHUNK_SIZE_BYTES must be between 256 KiB and 32 MiB.")

    session_hours = _int("SESSION_MAX_HOURS", 168)
    if session_hours < 1:
        raise SystemExit("SESSION_MAX_HOURS must be at least 1.")

    return Settings(
        base_dir=BASE_DIR,
        host=(os.environ.get("HOST") or "auto").strip() or "auto",
        port=port,
        storage_dir=storage_dir,
        data_dir=data_dir,
        log_dir=log_dir,
        max_upload_bytes=max_upload,
        chunk_size_bytes=chunk,
        allowed_extensions=frozenset(allowed),
        session_secret=session_secret,
        password_hash=password_hash,
        username=(os.environ.get("APP_USERNAME") or "owner").strip() or "owner",
        session_max_seconds=session_hours * 3600,
        login_max_attempts=max(1, _int("LOGIN_MAX_ATTEMPTS", 5)),
        login_window_seconds=max(30, _int("LOGIN_WINDOW_SECONDS", 900)),
        login_lockout_seconds=max(30, _int("LOGIN_LOCKOUT_SECONDS", 900)),
        cookie_secure=_bool("COOKIE_SECURE", False),
        incomplete_max_age_seconds=max(1, _int("INCOMPLETE_MAX_AGE_HOURS", 168)) * 3600,
        log_max_bytes=max(100_000, _int("LOG_MAX_BYTES", 2_000_000)),
        log_backup_count=max(1, _int("LOG_BACKUP_COUNT", 5)),
    )


settings = load_settings()
