"""Durable, restart-safe upload storage.

Completed files live in the storage root. A transfer is committed one chunk
at a time: bytes are fsynced, then the offset in the sidecar metadata is
advanced. A crash can only lose the uncommitted tail of the current chunk.
On startup that tail is truncated back to the last committed offset.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from security import FilenameError, child_file, sanitize_upload_name
from settings import settings

logger = logging.getLogger("phoneuploads")

_create_lock = threading.Lock()
_history_lock = threading.Lock()
_META_LOCK = threading.Lock()
MAX_INCOMPLETE = 30
_IGNORED = {"thumbs.db", "desktop.ini", ".ds_store"}
_UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")


class StorageError(Exception):
    pass


def ensure_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.incomplete_dir.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def valid_upload_id(upload_id: str) -> bool:
    return bool(_UPLOAD_ID.fullmatch(upload_id or ""))


def _meta_path(upload_id: str) -> Path:
    if not valid_upload_id(upload_id):
        raise StorageError("Невалиден номер на качване.")
    return settings.incomplete_dir / f"{upload_id}.json"


def _part_path(upload_id: str) -> Path:
    if not valid_upload_id(upload_id):
        raise StorageError("Невалиден номер на качване.")
    return settings.incomplete_dir / f"{upload_id}.part"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_meta(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Skipping unreadable upload metadata %s", path.name)
        return None
    if not isinstance(payload, dict) or not valid_upload_id(str(payload.get("upload_id") or "")):
        return None
    return payload


def iter_metas() -> list[dict]:
    if not settings.incomplete_dir.is_dir():
        return []
    found = []
    for path in settings.incomplete_dir.glob("*.json"):
        meta = _read_meta(path)
        if meta is not None:
            found.append(meta)
    return found


def load_meta(upload_id: str) -> dict:
    path = _meta_path(upload_id)
    if not path.is_file():
        raise StorageError("Качването не е намерено. Започнете го отново.")
    meta = _read_meta(path)
    if meta is None:
        raise StorageError("Данните за качването са повредени. Отхвърлете го и започнете отново.")
    return meta


def append_history(action: str, message: str, name: str = "", size: int | None = None, ok: bool = True) -> None:
    event = {
        "time": _now(),
        "action": action,
        "name": name,
        "size": size,
        "ok": ok,
        "message": message,
    }
    line = json.dumps(event, ensure_ascii=False) + "\n"
    path = settings.history_file
    with _history_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > 1_500_000:
                rotated = path.with_name("history.1.jsonl")
                older = path.with_name("history.2.jsonl")
                if rotated.exists():
                    if older.exists():
                        older.unlink()
                    os.replace(rotated, older)
                os.replace(path, rotated)
        except OSError:
            logger.exception("Could not rotate history")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def read_history(limit: int = 80) -> list[dict]:
    path = settings.history_file
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-max(1, min(limit, 200)) :]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    events.reverse()
    return events


def recover_incomplete() -> None:
    """Make partial uploads match their last committed offset."""
    ensure_dirs()
    now = time.time()
    with _META_LOCK:
        for meta in iter_metas():
            upload_id = meta["upload_id"]
            part = _part_path(upload_id)
            try:
                updated = datetime.fromisoformat(str(meta.get("updated") or meta.get("created"))).timestamp()
            except (TypeError, ValueError):
                updated = now
            if now - updated > settings.incomplete_max_age_seconds:
                _discard_files(upload_id)
                append_history(
                    "discard",
                    "Незавършено качване беше премахнато, защото беше твърде старо.",
                    name=str(meta.get("original") or ""),
                    ok=True,
                )
                logger.info("Discarded expired upload %s", upload_id)
                continue
            safe_name = str(meta.get("safe_name") or "")
            if safe_name != Path(safe_name).name or any(char in safe_name for char in "/\\:"):
                logger.warning("Upload %s has an unsafe stored name; leaving it untouched", upload_id)
                continue
            final = settings.storage_dir / safe_name
            if final.is_file() and not final.is_symlink() and final.stat().st_size == int(meta.get("size") or -1):
                part.unlink(missing_ok=True)
                _meta_path(upload_id).unlink(missing_ok=True)
                logger.info("Recovered already completed upload %s", upload_id)
                continue
            offset = int(meta.get("offset") or 0)
            if not part.exists():
                if offset == 0:
                    part.touch()
                else:
                    meta["offset"] = 0
                    meta["updated"] = _now()
                    meta["error"] = "missing-part"
                    _atomic_json(_meta_path(upload_id), meta)
                    part.touch()
                    logger.warning("Upload %s lost its partial file; reset to the start", upload_id)
                continue
            actual = part.stat().st_size
            if actual > offset:
                with part.open("r+b") as handle:
                    handle.truncate(offset)
                    handle.flush()
                    os.fsync(handle.fileno())
                logger.warning("Truncated upload %s from %s back to committed %s", upload_id, actual, offset)
            elif actual < offset:
                meta["offset"] = actual
                meta["updated"] = _now()
                _atomic_json(_meta_path(upload_id), meta)
                logger.warning("Upload %s metadata was ahead of the file; rewound to %s", upload_id, actual)


def _discard_files(upload_id: str) -> None:
    _part_path(upload_id).unlink(missing_ok=True)
    _meta_path(upload_id).unlink(missing_ok=True)
    for leftover in settings.incomplete_dir.glob(f"{upload_id}.json.tmp"):
        leftover.unlink(missing_ok=True)


def discard_upload(upload_id: str) -> dict:
    with _META_LOCK:
        meta = load_meta(upload_id)
        _discard_files(upload_id)
    append_history("discard", "Незавършеното качване беше отхвърлено.", name=str(meta.get("original") or ""), ok=True)
    logger.info("Discarded upload %s", upload_id)
    return meta


def _unique_name(stem: str, extension: str) -> str:
    for _ in range(8):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        token = secrets.token_hex(4)
        candidate = f"{stamp}_{token}_{stem}{extension}"
        if not (settings.storage_dir / candidate).exists() and not (settings.incomplete_dir / candidate).exists():
            return candidate
    raise StorageError("Не успях да запазя уникално име. Опитайте отново.")


def _free_bytes() -> int:
    target = settings.storage_dir if settings.storage_dir.exists() else settings.storage_dir.parent
    return shutil.disk_usage(target).free


def init_upload(filename: str, size: int, client_key: str) -> dict:
    if size < 1:
        raise StorageError("Празен файл не може да бъде качен.")
    if size > settings.max_upload_bytes:
        raise StorageError(
            f"Файлът е твърде голям. Максимумът е {_human_bytes(settings.max_upload_bytes)}."
        )
    if any(char in client_key for char in "\r\n\x00"):
        raise StorageError("Невалиден идентификатор на качването.")
    try:
        stem, extension, cleaned = sanitize_upload_name(filename, settings.allowed_extensions)
    except FilenameError as exc:
        raise StorageError(str(exc)) from exc

    with _create_lock:
        existing = _find_resumable(client_key, cleaned, size)
        if existing is not None:
            remaining = size - int(existing.get("offset") or 0)
            if _free_bytes() < max(0, remaining):
                raise StorageError("Няма достатъчно свободно място на диска за този файл.")
            logger.info("Resuming upload %s at %s", existing["upload_id"], existing.get("offset"))
            return {
                "upload_id": existing["upload_id"],
                "offset": int(existing.get("offset") or 0),
                "chunk_size": settings.chunk_size_bytes,
                "size": size,
                "resumed": True,
                "original": cleaned,
            }
        active = [meta for meta in iter_metas() if not meta.get("error")]
        if len(active) >= MAX_INCOMPLETE:
            raise StorageError(
                "Има твърде много незавършени качвания. Отхвърлете старите и опитайте отново."
            )
        if _free_bytes() < size + 8 * 1024 * 1024:
            raise StorageError("Няма достатъчно свободно място на диска за този файл.")
        upload_id = secrets.token_hex(16)
        safe_name = _unique_name(stem, extension)
        now = _now()
        meta = {
            "upload_id": upload_id,
            "original": cleaned,
            "safe_name": safe_name,
            "size": size,
            "offset": 0,
            "client_key": client_key,
            "created": now,
            "updated": now,
        }
        part = _part_path(upload_id)
        part.touch()
        _atomic_json(_meta_path(upload_id), meta)
        logger.info("Started upload %s for %s (%s bytes)", upload_id, cleaned, size)
        return {
            "upload_id": upload_id,
            "offset": 0,
            "chunk_size": settings.chunk_size_bytes,
            "size": size,
            "resumed": False,
            "original": cleaned,
        }


def _find_resumable(client_key: str, cleaned: str, size: int) -> dict | None:
    keyed = None
    same_file = []
    for meta in iter_metas():
        if meta.get("error"):
            continue
        if int(meta.get("size") or -1) != size:
            continue
        if meta.get("client_key") == client_key:
            keyed = meta
            break
        if meta.get("original") == cleaned:
            same_file.append(meta)
    if keyed is not None:
        return keyed
    if not same_file:
        return None
    same_file.sort(key=lambda item: str(item.get("updated") or ""), reverse=True)
    return same_file[0]


def begin_chunk(upload_id: str, offset: int, length: int) -> dict:
    if length < 1:
        raise StorageError("Празното парче не може да бъде записано.")
    if length > settings.chunk_size_bytes:
        raise StorageError("Парчето е по-голямо от разрешения размер.")
    with _META_LOCK:
        meta = load_meta(upload_id)
        if meta.get("error"):
            raise StorageError("Това качване е повредено. Отхвърлете го и започнете отново.")
        committed = int(meta.get("offset") or 0)
        total = int(meta.get("size") or 0)
        if offset < 0 or length < 1:
            raise StorageError("Невалидна позиция в файла.")
        if offset + length > total:
            raise StorageError("Парчето излиза извън обявения размер на файла.")
        if offset == committed:
            return {"meta": meta, "duplicate": False, "offset": committed}
        if offset < committed and offset + length <= committed:
            return {"meta": meta, "duplicate": True, "offset": committed}
        raise ChunkConflict("Отместването не съвпада. Качването ще продължи от последната записана позиция.", committed)


class ChunkConflict(StorageError):
    def __init__(self, message: str, offset: int) -> None:
        super().__init__(message)
        self.offset = offset


class DiskAppender:
    """Write one HTTP chunk straight to the partial file.

    ``feed`` writes each socket buffer immediately. It does not accumulate
    the chunk, and it never sees the rest of a multi-gigabyte file.
    """

    def __init__(self, path: Path, offset: int, expected_len: int) -> None:
        self.path = path
        self.offset = offset
        self.expected_len = expected_len
        self.written = 0
        self._aborted = False
        self._finished = False
        self._file = path.open("r+b")
        try:
            end = self._file.seek(0, os.SEEK_END)
            if end < offset:
                raise StorageError("Частичният файл е по-къс от записаната позиция.")
            if end > offset:
                self._file.truncate(offset)
            self._file.seek(offset)
        except Exception:
            self._file.close()
            raise

    def feed(self, data: bytes) -> None:
        if self._finished or self._aborted:
            raise StorageError("Качването вече е приключено.")
        if not data:
            return
        if self.written + len(data) > self.expected_len:
            self.abort()
            raise StorageError("Получени са повече данни от обявения размер.")
        self._file.write(data)
        self.written += len(data)

    def finish(self) -> int:
        try:
            if self.written != self.expected_len:
                raise StorageError("Връзката прекъсна преди края на парчето. Можете да опитате отново.")
            self._file.flush()
            os.fsync(self._file.fileno())
            self._finished = True
            return self.offset + self.written
        except Exception:
            self.abort()
            raise
        finally:
            self._close()

    def abort(self) -> None:
        if self._aborted or self._finished:
            self._close()
            return
        self._aborted = True
        try:
            if not self._file.closed:
                self._file.flush()
                self._file.truncate(self.offset)
                self._file.flush()
                os.fsync(self._file.fileno())
        except OSError:
            logger.exception("Failed to roll back a partial chunk")
        finally:
            self._close()

    def _close(self) -> None:
        if not self._file.closed:
            self._file.close()


def commit_chunk(upload_id: str, new_offset: int) -> dict:
    with _META_LOCK:
        meta = load_meta(upload_id)
        meta["offset"] = new_offset
        meta["updated"] = _now()
        meta.pop("error", None)
        _atomic_json(_meta_path(upload_id), meta)
        return meta


def complete_upload(upload_id: str) -> dict:
    with _META_LOCK:
        meta = load_meta(upload_id)
        total = int(meta.get("size") or 0)
        offset = int(meta.get("offset") or 0)
        if offset != total:
            raise StorageError(
                "Файлът още не е получен изцяло. Продължете качването или опитайте отново."
            )
        part = _part_path(upload_id)
        if not part.is_file() or part.stat().st_size != total:
            raise StorageError("Частичният файл не съвпада с обявения размер. Опитайте отново.")
        safe_name = str(meta.get("safe_name") or "")
        if not safe_name or safe_name != Path(safe_name).name or any(char in safe_name for char in "/\\:"):
            raise StorageError("Невалидно име за запис.")
        target = settings.storage_dir / safe_name
        if target.exists():
            stem, extension, _cleaned = sanitize_upload_name(safe_name, settings.allowed_extensions)
            safe_name = _unique_name(stem, extension)
            target = settings.storage_dir / safe_name
            meta["safe_name"] = safe_name
        os.replace(part, target)
        _meta_path(upload_id).unlink(missing_ok=True)
    append_history(
        "upload",
        "Файлът е записан.",
        name=target.name,
        size=total,
        ok=True,
    )
    logger.info("Stored %s (%s bytes)", target.name, total)
    return {"name": target.name, "size": total, "original": meta.get("original") or target.name}


def list_incomplete() -> list[dict]:
    items = []
    for meta in iter_metas():
        items.append(
            {
                "upload_id": meta["upload_id"],
                "original": meta.get("original") or "",
                "size": int(meta.get("size") or 0),
                "offset": int(meta.get("offset") or 0),
                "updated": meta.get("updated") or "",
                "error": bool(meta.get("error")),
            }
        )
    items.sort(key=lambda item: str(item["updated"]), reverse=True)
    return items


def list_files() -> list[dict]:
    entries = []
    if not settings.storage_dir.is_dir():
        return entries
    for path in settings.storage_dir.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith(".") or path.name.lower() in _IGNORED:
            continue
        try:
            resolved = child_file(settings.storage_dir, path.name)
        except FilenameError:
            continue
        stat = resolved.stat()
        entries.append(
            {
                "name": resolved.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    entries.sort(key=lambda item: item["modified"], reverse=True)
    return entries[:1000]


def delete_file(name: str) -> dict:
    try:
        path = child_file(settings.storage_dir, name)
    except FilenameError as exc:
        raise StorageError(str(exc)) from exc
    size = path.stat().st_size
    path.unlink()
    append_history("delete", "Файлът е изтрит.", name=path.name, size=size, ok=True)
    logger.info("Deleted %s", path.name)
    return {"name": path.name, "size": size}


def resolve_download(name: str) -> Path:
    try:
        return child_file(settings.storage_dir, name)
    except FilenameError as exc:
        raise StorageError(str(exc)) from exc


def used_bytes() -> int:
    total = 0
    for item in list_files():
        total += int(item["size"])
    return total


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
