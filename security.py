"""Password checks, login throttling, and safe file names."""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
import unicodedata
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FORBIDDEN = '<>:"/\\|?*'
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class FilenameError(ValueError):
    pass


def verify_password(password: str, encoded_hash: str) -> bool:
    if not password or not encoded_hash or len(password) > 256:
        return False
    try:
        return _HASHER.verify(encoded_hash, password)
    except (VerifyMismatchError, InvalidHash, VerificationError):
        return False


def tokens_equal(expected: str, supplied: str) -> bool:
    if not expected or not supplied:
        return False
    try:
        return hmac.compare_digest(expected, supplied)
    except (TypeError, ValueError):
        return False


def sanitize_upload_name(original: str, allowed: frozenset[str]) -> tuple[str, str, str]:
    """Return ``(stem, extension, cleaned_original)`` or raise FilenameError.

    The returned stem is only used inside a generated unique name. The
    extension must be on the allow-list. Path components are discarded.
    """
    if original is None:
        raise FilenameError("Липсва име на файл.")
    name = unicodedata.normalize("NFC", str(original))
    name = name.replace("\\", "/").split("/")[-1]
    name = _CONTROL.sub("", name).strip().strip(".")
    if not name or name in {".", ".."}:
        raise FilenameError("Невалидно име на файл.")
    cleaned = []
    for char in name:
        cleaned.append("_" if char in _FORBIDDEN else char)
    cleaned_name = "".join(cleaned).strip().strip(".")
    if not cleaned_name:
        raise FilenameError("Невалидно име на файл.")
    suffix = Path(cleaned_name).suffix.lower()
    if suffix not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        shown = suffix or "(няма)"
        raise FilenameError(
            f"Разширението „{shown}“ не е разрешено. Позволени: {allowed_text}."
        )
    stem = cleaned_name[: -len(suffix)]
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:60]
    if not stem:
        stem = "file"
    # Prefix reserved device names after trimming. A later strip must not
    # remove that prefix, or Windows would treat CON.mp4 as a device.
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    return stem, suffix, cleaned_name[:180]


def child_file(root: Path, name: str) -> Path:
    """Resolve a direct child of ``root`` and reject traversal or links."""
    if not isinstance(name, str) or not name or len(name) > 240:
        raise FilenameError("Невалидно име на файл.")
    if name != Path(name).name or any(char in name for char in "/\\:"):
        raise FilenameError("Невалидно име на файл.")
    if name in {".", ".."} or name.startswith("."):
        raise FilenameError("Невалидно име на файл.")
    candidate = root / name
    if candidate.is_symlink():
        raise FilenameError("Невалиден файл.")
    if not candidate.is_file():
        raise FilenameError("Файлът не е намерен.")
    resolved = candidate.resolve()
    if resolved.parent != root.resolve():
        raise FilenameError("Невалиден път.")
    return resolved


class LoginGuard:
    """Small on-disk lockout used by the single server process."""

    def __init__(self, path: Path, max_attempts: int, window: int, lockout: int) -> None:
        self.path = path
        self.max_attempts = max_attempts
        self.window = window
        self.lockout = lockout
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()

    def check(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            data = self._load()
            record = self._prune(data, ip, now)
            locked_until = float(record.get("locked_until") or 0)
            if locked_until > now:
                self._save(data)
                return False, max(1, int(locked_until - now))
            record["locked_until"] = 0
            self._save(data)
            return True, 0

    def failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            data = self._load()
            record = self._prune(data, ip, now)
            failures = list(record.get("failures") or [])
            failures.append(now)
            record["failures"] = failures[-50:]
            if len(record["failures"]) >= self.max_attempts:
                record["locked_until"] = now + self.lockout
            self._save(data)

    def success(self, ip: str) -> None:
        with self._lock:
            data = self._load()
            data.get("ips", {}).pop(self._key(ip), None)
            self._save(data)

    def _key(self, ip: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z:.\-]", "", ip or "")[:64]
        return cleaned or "unknown"

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"ips": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ips": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("ips"), dict):
            return {"ips": {}}
        return payload

    def _save(self, data: dict) -> None:
        ips = data.setdefault("ips", {})
        if len(ips) > 500:
            data["ips"] = dict(list(ips.items())[-500:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os_replace(temporary, self.path)

    def _prune(self, data: dict, ip: str, now: float) -> dict:
        ips = data.setdefault("ips", {})
        key = self._key(ip)
        record = ips.get(key)
        if not isinstance(record, dict):
            record = {"failures": [], "locked_until": 0}
        recent = [
            float(stamp)
            for stamp in record.get("failures") or []
            if now - float(stamp) <= self.window
        ]
        record["failures"] = recent
        ips[key] = record
        return record


def os_replace(source: Path, destination: Path) -> None:
    source.replace(destination)
