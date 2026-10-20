"""Personal upload server.

The process listens only on a Tailscale address, or on 127.0.0.1 when started
with the local-test switch. Authentication is still required on that private
network. Uploads are written to disk in bounded chunks.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from network import bind_mode, resolve_host
from security import LoginGuard, tokens_equal, verify_password
from settings import RESOURCE_DIR, settings
from storage import (
    ChunkConflict,
    DiskAppender,
    StorageError,
    append_history,
    begin_chunk,
    commit_chunk,
    complete_upload,
    discard_upload,
    ensure_dirs,
    init_upload,
    list_files,
    list_incomplete,
    load_meta,
    read_history,
    recover_incomplete,
    resolve_download,
    used_bytes,
    valid_upload_id,
    _part_path,
)

logger = logging.getLogger("phoneuploads")
templates = Jinja2Templates(directory=str(RESOURCE_DIR / "templates"))
_upload_locks: dict[str, asyncio.Lock] = {}

guard = LoginGuard(
    settings.attempts_file,
    settings.login_max_attempts,
    settings.login_window_seconds,
    settings.login_lockout_seconds,
)


def configure_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        handler = RotatingFileHandler(
            settings.log_dir / "server.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


def uvicorn_log_config() -> dict:
    path = str(settings.log_dir / "access.log")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "plain",
                "filename": path,
                "maxBytes": settings.log_max_bytes,
                "backupCount": settings.log_backup_count,
                "encoding": "utf-8",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }


def _require_configured() -> None:
    if settings.session_secret and settings.password_hash:
        return
    configure_logging()
    logger.error("Missing password hash or session secret. Run install.ps1 before starting the server.")
    raise RuntimeError("Missing password hash or session secret. Run install.ps1 before starting the server.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _require_configured()
    ensure_dirs()
    configure_logging()
    recover_incomplete()
    logger.info("Storage directory: %s", settings.storage_dir)
    logger.info(
        "Maximum upload size: %s bytes; chunk size: %s bytes",
        settings.max_upload_bytes,
        settings.chunk_size_bytes,
    )
    yield


class HeaderMiddleware:
    """Add security headers without reading the request body.

    BaseHTTPMiddleware buffers request bodies, which would defeat streaming
    uploads. This wrapper only inspects the Content-Length header.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if method in {"POST", "PUT", "DELETE"} and "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError:
                await _send_json(send, 400, "Невалиден Content-Length.")
                return
            is_chunk = method == "PUT" and path.startswith("/api/uploads/") and path.endswith("/chunk")
            limit = settings.chunk_size_bytes if is_chunk else 1_000_000
            if length > limit:
                await _send_json(send, 413, "Заявката е твърде голяма.")
                return
        cache = b"public, max-age=3600" if path.startswith("/static/") else b"no-store"

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in _ADDED_HEADERS
                ]
                message = {**message, "headers": kept + _header_pairs(cache)}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_ADDED_HEADERS = {
    b"x-content-type-options",
    b"x-frame-options",
    b"referrer-policy",
    b"permissions-policy",
    b"content-security-policy",
    b"cache-control",
}


def _header_pairs(cache: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (
            b"content-security-policy",
            b"default-src 'self'; img-src 'self' data:; style-src 'self'; "
            b"script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        ),
        (b"cache-control", cache),
    ]


async def _send_json(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                *_header_pairs(b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


app = FastAPI(title="Phone uploads", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(RESOURCE_DIR / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "not-configured",
    session_cookie="phoneuploads_session",
    max_age=settings.session_max_seconds,
    same_site="lax",
    https_only=settings.cookie_secure,
    path="/",
)
app.add_middleware(HeaderMiddleware)


def render(request: Request, name: str, context: dict) -> Response:
    payload = {"request": request, **context}
    try:
        return templates.TemplateResponse(request, name, payload)
    except TypeError:
        return templates.TemplateResponse(name, payload)


def client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str) or len(token) < 20:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def current_user(request: Request) -> str:
    user = request.session.get("user")
    issued = request.session.get("iat")
    if not isinstance(user, str) or not isinstance(issued, int):
        raise HTTPException(status_code=401, detail="Необходим е вход.")
    if time.time() - issued > settings.session_max_seconds:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Сесията изтече. Влезте отново.")
    return user


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    host = request.headers.get("host", "")
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


def require_user(request: Request) -> str:
    return current_user(request)


def require_csrf(request: Request) -> None:
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Отхвърлен източник на заявката.")
    if not tokens_equal(str(request.session.get("csrf") or ""), request.headers.get("x-csrf-token", "")):
        raise HTTPException(
            status_code=403,
            detail="Липсва или е невалиден защитен токен. Презаредете страницата.",
        )


def form_values(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=20)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _status_for_storage(exc: StorageError) -> int:
    text = str(exc)
    if "не е намерен" in text or "не е намерено" in text:
        return 404
    if "голям" in text or "Парчето е по-голямо" in text:
        return 413
    if "място" in text:
        return 507
    if "много незавършени" in text:
        return 429
    return 400


def _lock_for(upload_id: str) -> asyncio.Lock:
    lock = _upload_locks.get(upload_id)
    if lock is None:
        lock = asyncio.Lock()
        _upload_locks[upload_id] = lock
    return lock


class InitRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    size: int = Field(gt=0)
    client_key: str = Field(min_length=1, max_length=400)


class DeleteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    confirm: bool = False


api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user") and isinstance(request.session.get("iat"), int):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"error": "", "csrf_token": ensure_csrf(request), "status_code": 200})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    ip = client_ip(request)
    body = await request.body()
    form = form_values(body)
    allowed, retry_after = guard.check(ip)
    token_ok = tokens_equal(str(request.session.get("csrf") or ""), form.get("csrf_token", ""))
    if not origin_allowed(request) or not token_ok:
        logger.info("Rejected login form from %s", ip)
        response = render(
            request,
            "login.html",
            {
                "error": "Формулярът е изтекъл. Опитайте отново.",
                "csrf_token": ensure_csrf(request),
            },
        )
        response.status_code = 403
        return response
    if not allowed:
        minutes = max(1, (retry_after + 59) // 60)
        logger.info("Login locked for %s", ip)
        response = render(
            request,
            "login.html",
            {
                "error": f"Твърде много опити. Опитайте отново след около {minutes} мин.",
                "csrf_token": ensure_csrf(request),
            },
        )
        response.status_code = 429
        return response
    if not verify_login(form.get("password", "")):
        guard.failure(ip)
        logger.info("Failed login from %s", ip)
        response = render(
            request,
            "login.html",
            {"error": "Грешна парола.", "csrf_token": ensure_csrf(request)},
        )
        response.status_code = 401
        return response
    guard.success(ip)
    request.session.clear()
    request.session["user"] = settings.username
    request.session["iat"] = int(time.time())
    request.session["csrf"] = secrets.token_urlsafe(32)
    logger.info("Login succeeded from %s", ip)
    return RedirectResponse("/", status_code=303)


def verify_login(password: str) -> bool:
    return verify_password(password, settings.password_hash)


@app.post("/logout")
async def logout(request: Request):
    body = await request.body()
    form = form_values(body)
    supplied = request.headers.get("x-csrf-token") or form.get("csrf_token", "")
    if not origin_allowed(request) or not tokens_equal(str(request.session.get("csrf") or ""), supplied):
        return RedirectResponse("/login", status_code=303)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    token = ensure_csrf(request)
    boot = {
        "csrfToken": token,
        "chunkSize": settings.chunk_size_bytes,
        "maxUploadBytes": settings.max_upload_bytes,
        "allowedExtensions": sorted(settings.allowed_extensions),
    }
    return render(request, "index.html", {"csrf_token": token, "boot": boot})


@api.get("/overview")
def overview() -> dict:
    from shutil import disk_usage

    target = settings.storage_dir if settings.storage_dir.exists() else settings.storage_dir.parent
    usage = disk_usage(target)
    return {
        "free_bytes": usage.free,
        "used_bytes": used_bytes(),
        "max_upload_bytes": settings.max_upload_bytes,
        "chunk_size": settings.chunk_size_bytes,
        "allowed_extensions": sorted(settings.allowed_extensions),
        "incomplete": list_incomplete(),
        "storage_dir": str(settings.storage_dir),
    }


@api.get("/files")
def files() -> dict:
    return {"files": list_files()}


@api.get("/history")
def history() -> dict:
    return {"events": read_history(80)}


@api.post("/uploads")
def create_upload(body: InitRequest, _: None = Depends(require_csrf)) -> dict:
    try:
        return init_upload(body.filename, body.size, body.client_key)
    except StorageError as exc:
        append_history("error", str(exc), name=body.filename, size=body.size, ok=False)
        logger.info("Rejected upload %s: %s", body.filename, exc)
        raise HTTPException(status_code=_status_for_storage(exc), detail=str(exc)) from exc


@api.get("/uploads/{upload_id}")
def upload_status(upload_id: str) -> dict:
    if not valid_upload_id(upload_id):
        raise HTTPException(status_code=400, detail="Невалиден номер на качване.")
    try:
        meta = load_meta(upload_id)
    except StorageError as exc:
        raise HTTPException(status_code=_status_for_storage(exc), detail=str(exc)) from exc
    return {
        "upload_id": upload_id,
        "offset": int(meta.get("offset") or 0),
        "size": int(meta.get("size") or 0),
        "original": meta.get("original") or "",
    }


@api.put("/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, offset: int, _: None = Depends(require_csrf)):
    if not valid_upload_id(upload_id):
        raise HTTPException(status_code=400, detail="Невалиден номер на качване.")
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return JSONResponse({"detail": "Липсва дължина на парчето."}, status_code=411)
    try:
        length = int(raw_length)
    except ValueError:
        return JSONResponse({"detail": "Невалиден Content-Length."}, status_code=400)
    async with _lock_for(upload_id):
        try:
            decision = begin_chunk(upload_id, offset, length)
        except ChunkConflict as exc:
            return JSONResponse({"detail": str(exc), "offset": exc.offset}, status_code=409)
        except StorageError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=_status_for_storage(exc))
        if decision["duplicate"]:
            return {"offset": decision["offset"], "duplicate": True}
        writer = DiskAppender(_part_path(upload_id), offset, length)
        try:
            async for piece in request.stream():
                writer.feed(piece)
            new_offset = writer.finish()
            commit_chunk(upload_id, new_offset)
        except StorageError as exc:
            writer.abort()
            logger.info("Chunk failed for %s: %s", upload_id, exc)
            return JSONResponse({"detail": str(exc)}, status_code=_status_for_storage(exc))
        except Exception:
            writer.abort()
            logger.exception("Chunk write failed for %s", upload_id)
            return JSONResponse(
                {
                    "detail": "Качването беше прекъснато. Опитайте отново — ще продължи от последната успешна част."
                },
                status_code=400,
            )
        return {"offset": new_offset, "duplicate": False}


@api.post("/uploads/{upload_id}/complete")
async def finish_upload(upload_id: str, _: None = Depends(require_csrf)) -> dict:
    if not valid_upload_id(upload_id):
        raise HTTPException(status_code=400, detail="Невалиден номер на качване.")
    async with _lock_for(upload_id):
        try:
            return complete_upload(upload_id)
        except StorageError as exc:
            append_history("error", str(exc), ok=False)
            raise HTTPException(status_code=_status_for_storage(exc), detail=str(exc)) from exc


@api.post("/uploads/{upload_id}/discard")
async def discard(upload_id: str, _: None = Depends(require_csrf)) -> dict:
    if not valid_upload_id(upload_id):
        raise HTTPException(status_code=400, detail="Невалиден номер на качване.")
    async with _lock_for(upload_id):
        try:
            discard_upload(upload_id)
        except StorageError as exc:
            raise HTTPException(status_code=_status_for_storage(exc), detail=str(exc)) from exc
    return {"ok": True}


@api.post("/files/delete")
def remove_file(body: DeleteRequest, _: None = Depends(require_csrf)) -> dict:
    if body.confirm is not True:
        raise HTTPException(status_code=400, detail="Изтриването изисква потвърждение.")
    from storage import delete_file

    try:
        return delete_file(body.name)
    except StorageError as exc:
        raise HTTPException(status_code=_status_for_storage(exc), detail=str(exc)) from exc


def _content_disposition(name: str) -> str:
    fallback = name.encode("ascii", "ignore").decode().replace('"', "") or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header or not header.startswith("bytes="):
        return None
    piece = header.split("=", 1)[1].split(",", 1)[0].strip()
    start_text, sep, end_text = piece.partition("-")
    if not sep:
        return None
    try:
        if start_text == "":
            length = int(end_text)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _iter_range(path: Path, start: int, end: int):
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/download/{name}")
def download(name: str, request: Request):
    try:
        current_user(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    try:
        path = resolve_download(name)
    except StorageError as exc:
        status = _status_for_storage(exc)
        return JSONResponse({"detail": str(exc)}, status_code=status)
    size = path.stat().st_size
    disposition = _content_disposition(path.name)
    requested = _parse_range(request.headers.get("range", ""), size)
    if request.headers.get("range") and requested is None:
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{size}",
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
            },
        )
    if requested is None:
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
            content_disposition_type="attachment",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
        )
    start, end = requested
    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Disposition": disposition,
        "Content-Length": str(end - start + 1),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        _iter_range(path, start, end),
        status_code=206,
        media_type="application/octet-stream",
        headers=headers,
    )


app.include_router(api)


@app.exception_handler(StarletteHTTPException)
async def http_error(_request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Грешка."
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    logger.info("Invalid request %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Невалидни данни в заявката."}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse({"detail": "Вътрешна грешка. Вижте журнала на лаптопа."}, status_code=500)


def _write_runtime_files(host: str) -> None:
    ensure_dirs()
    settings.pid_file.write_text(str(os.getpid()), encoding="ascii")
    payload = {
        "host": host,
        "port": settings.port,
        "pid": os.getpid(),
        "mode": bind_mode(host),
        "url": f"http://[{host}]:{settings.port}/" if ":" in host else f"http://{host}:{settings.port}/",
        "storage_dir": str(settings.storage_dir),
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    settings.listen_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="ascii")


def _remove_runtime_files() -> None:
    settings.pid_file.unlink(missing_ok=True)
    settings.listen_file.unlink(missing_ok=True)


def _ensure_stdio() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def build_server(host: str) -> uvicorn.Server:
    """Create a server object the desktop app can start and stop."""
    _ensure_stdio()
    ensure_dirs()
    configure_logging()
    _require_configured()
    _write_runtime_files(host)
    config = uvicorn.Config(
        app,
        host=host,
        port=settings.port,
        log_config=uvicorn_log_config(),
        server_header=False,
        proxy_headers=False,
        forwarded_allow_ips="",
        timeout_keep_alive=120,
        limit_concurrency=30,
        access_log=True,
    )
    return uvicorn.Server(config)


def main() -> None:
    _ensure_stdio()
    os.chdir(settings.base_dir)
    ensure_dirs()
    configure_logging()
    try:
        _require_configured()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        host = resolve_host(settings.host)
    except SystemExit as exc:
        logger.error("%s", exc)
        raise
    _write_runtime_files(host)
    atexit.register(_remove_runtime_files)
    mode = bind_mode(host)
    url = f"http://[{host}]:{settings.port}/" if ":" in host else f"http://{host}:{settings.port}/"
    logger.info("Listening on %s mode=%s", url, mode)
    if mode == "local":
        logger.warning("Local test mode. The phone cannot reach 127.0.0.1. Start without -Local for Tailscale.")
    try:
        uvicorn.run(
            "app:app",
            host=host,
            port=settings.port,
            workers=1,
            log_config=uvicorn_log_config(),
            server_header=False,
            proxy_headers=False,
            forwarded_allow_ips="",
            timeout_keep_alive=120,
            limit_concurrency=30,
            access_log=True,
        )
    except OSError as exc:
        logger.exception("Server failed to bind %s:%s: %s", host, settings.port, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
