"""Windows application for the phone upload server.

Double-click the exe on the laptop. The first launch asks for a password and
a folder, then keeps the server running. Python does not have to be installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183


def app_home() -> Path:
    override = os.environ.get("PHONEUPLOADS_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "PhoneUploads"


os.environ.setdefault("PHONEUPLOADS_HOME", str(app_home()))

# Imported here so the packaged program contains the server, not only the window.
import app  # noqa: E402,F401
import setup_config  # noqa: E402,F401


def _known_folder(csidl: int) -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        return Path.home() / "Desktop"
    return Path(buffer.value)


def _desktop_dir() -> Path:
    return _known_folder(0x0010)


def _programs_dir() -> Path:
    return _known_folder(0x0002)


def _startup_dir() -> Path:
    return _known_folder(0x0007)


def _message(text: str, title: str = "Качване от телефона") -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def _single_instance() -> bool:
    import ctypes

    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\PhoneUploadsApp")
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def _installed_exe(home: Path) -> Path:
    return home / "PhoneUploads.exe"


def _report_error(exc: BaseException) -> None:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "no such file" in lowered or "cannot find" in lowered or "не може да намери" in lowered:
        text = (
            "Липсват файлове до програмата. Копирай цялата папка PhoneUploads-laptop, "
            "не само PhoneUploads.exe.\n\n" + text
        )
    try:
        log = app_home() / "error.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    _message(text)


def _copy_into_app_folder() -> None:
    """Running the folder from a USB stick installs every file under LocalAppData."""
    if not getattr(sys, "frozen", False):
        return
    home = app_home()
    target = _installed_exe(home)
    current = Path(sys.executable).resolve()
    source_dir = current.parent
    if current == target.resolve():
        internal = Path(getattr(sys, "_MEIPASS", ""))
        template = internal / "templates" / "login.html"
        if internal and not template.is_file():
            _message(
                "Липсват файловете до програмата. Копирай цялата папка PhoneUploads-laptop, не само PhoneUploads.exe."
            )
            os._exit(1)
        return
    home.mkdir(parents=True, exist_ok=True)
    try:
        for item in source_dir.iterdir():
            dest = home / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
    except OSError:
        _message("Затвори вече отворената програма и копирай цялата папка отново.")
        os._exit(1)
    subprocess.Popen([str(target)], cwd=str(home))
    os._exit(0)


def _relaunch() -> None:
    if getattr(sys, "frozen", False):
        command = [sys.executable]
        cwd = str(Path(sys.executable).resolve().parent)
    else:
        command = [sys.executable, str(Path(__file__).resolve())]
        cwd = str(Path(__file__).resolve().parent)
    subprocess.Popen(command, cwd=cwd)
    os._exit(0)


def _create_shortcut(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$link = $shell.CreateShortcut('{path}'); "
        f"$link.TargetPath = '{target}'; "
        f"$link.WorkingDirectory = '{target.parent}'; "
        "$link.WindowStyle = 7; "
        "$link.Description = 'Качване от телефона'; "
        "$link.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def _set_autostart(enabled: bool) -> None:
    link = _startup_dir() / "Качване от телефона.lnk"
    if not enabled:
        link.unlink(missing_ok=True)
        return
    target = _installed_exe(app_home()) if getattr(sys, "frozen", False) else Path(sys.executable)
    if getattr(sys, "frozen", False) and not target.is_file():
        target = Path(sys.executable)
    _create_shortcut(link, target)


def _install_shortcuts() -> None:
    target = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__).resolve()
    _create_shortcut(_desktop_dir() / "Качване от телефона.lnk", target)
    _create_shortcut(_programs_dir() / "Качване от телефона.lnk", target)


def _allow_firewall(port: int) -> None:
    script = Path(os.environ.get("TEMP", ".")) / "phoneuploads-firewall.ps1"
    script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    exit 1
}}
$adapters = @(Get-NetAdapter | Where-Object {{ $_.InterfaceDescription -match 'Tailscale' -or $_.Name -match 'Tailscale' }})
if ($adapters.Count -eq 0) {{ exit 2 }}
$names = @($adapters | ForEach-Object {{ $_.Name }})
Get-NetFirewallRule -DisplayName 'PhoneUploads (Tailscale only)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'PhoneUploads (Tailscale only)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort {int(port)} -InterfaceAlias $names -RemoteAddress '100.64.0.0/10' -Profile Any -EdgeTraversalPolicy Block | Out-Null
""",
        encoding="utf-8-sig",
    )
    command = f"Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{script}\"'"
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def _bulgarian_error(exc: BaseException) -> str:
    text = str(exc.code if isinstance(exc, SystemExit) and exc.code is not None else exc)
    known = {
        "Password must be at least 12 characters.": "Паролата трябва да е поне 12 знака.",
        "Password must be at most 128 characters.": "Паролата трябва да е най-много 128 знака.",
        "Password must contain at least one letter and one digit.": "Паролата трябва да има поне една буква и една цифра.",
        "Choose a less common password.": "Избери по-трудна парола.",
        "Passwords did not match.": "Двете пароли не съвпадат.",
        "Choose a storage folder that is not the application folder itself.": "Избери друга папка. Тази е папката на самата програма.",
    }
    if text in known:
        return known[text]
    if text.startswith("Refusing storage directory"):
        return "Тази папка е в Windows и не може да се ползва."
    return text


def _configured(home: Path) -> bool:
    return (home / "data" / "password.hash").is_file() and (home / ".env").is_file()


def _default_storage() -> str:
    import ctypes

    if ctypes.windll.kernel32.GetDriveTypeW("D:\\") == 3:
        return r"D:\PhoneUploads"
    return str(Path.home() / "PhoneUploads")


def _run_wizard() -> bool:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from setup_config import main as setup_main
    from setup_config import validate_password

    root = tk.Tk()
    root.title("Инсталиране")
    root.geometry("560x460")
    root.minsize(520, 430)
    root.configure(padx=16, pady=12)

    ttk.Label(root, text="Качване от телефона", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(
        root,
        text="Слагаш програмата на лаптопа веднъж. После от телефона, през Tailscale,\nкачваш видеото и то се записва на диска.",
        justify="left",
    ).pack(anchor="w", pady=(4, 12))

    store = tk.StringVar(value=_default_storage())
    port = tk.StringVar(value="8787")
    autostart = tk.BooleanVar(value=True)
    firewall = tk.BooleanVar(value=True)
    done = {"ok": False}

    def row(label: str, widget: tk.Widget) -> None:
        ttk.Label(root, text=label).pack(anchor="w")
        widget.pack(fill="x", pady=(2, 8))

    password = ttk.Entry(root, show="*")
    repeat = ttk.Entry(root, show="*")
    row("Парола (поне 12 знака, буква и цифра)", password)
    row("Повтори паролата", repeat)

    folder_row = ttk.Frame(root)
    folder_entry = ttk.Entry(folder_row, textvariable=store)
    folder_entry.pack(side="left", fill="x", expand=True)

    def browse() -> None:
        chosen = filedialog.askdirectory(title="Папка за видеата")
        if chosen:
            store.set(chosen)

    ttk.Button(folder_row, text="Избери", command=browse).pack(side="left", padx=(8, 0))
    row("Папка за файловете", folder_row)
    row("Порт", ttk.Entry(root, textvariable=port))
    ttk.Checkbutton(root, text="Да тръгва само, когато вляза в Windows", variable=autostart).pack(anchor="w")
    ttk.Checkbutton(root, text="Защитната стена: разреши само през Tailscale", variable=firewall).pack(anchor="w", pady=(4, 12))

    def install() -> None:
        first = password.get()
        if first != repeat.get():
            messagebox.showerror("Парола", "Двете пароли не съвпадат.", parent=root)
            return
        try:
            validate_password(first, "owner")
            chosen_port = int(port.get().strip())
            if not 1024 <= chosen_port <= 65535:
                raise ValueError
        except SystemExit as exc:
            messagebox.showerror("Парола", _bulgarian_error(exc), parent=root)
            return
        except ValueError:
            messagebox.showerror("Порт", "Портът трябва да е число между 1024 и 65535.", parent=root)
            return
        os.environ["PHONEUPLOADS_NEW_PASSWORD"] = first
        try:
            setup_main(["--storage", store.get().strip(), "--port", str(chosen_port)])
        except SystemExit as exc:
            if exc.code not in (0, None):
                messagebox.showerror("Инсталиране", _bulgarian_error(exc), parent=root)
                return
        except OSError as exc:
            messagebox.showerror(
                "Папка",
                "Тази папка не може да се създаде. Избери друга, например в Документи.\n\n" + str(exc),
                parent=root,
            )
            return
        finally:
            os.environ.pop("PHONEUPLOADS_NEW_PASSWORD", None)
        if getattr(sys, "frozen", False):
            try:
                _install_shortcuts()
                _set_autostart(autostart.get())
            except OSError:
                pass
        if firewall.get():
            _allow_firewall(chosen_port)
        done["ok"] = True
        root.destroy()

    ttk.Button(root, text="Инсталирай", command=install).pack(fill="x", ipady=6)
    root.mainloop()
    return done["ok"]


class ControlWindow:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = tk.Tk()
        self.root.title("Качване от телефона")
        self.root.geometry("640x460")
        self.root.minsize(560, 400)
        self.local = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Стартиране...")
        self.address = tk.StringVar(value="")
        self.folder = tk.StringVar(value="")
        self._server = None
        self._thread: threading.Thread | None = None
        self._error = ""
        self._host = ""

        pad = ttk.Frame(self.root, padding=16)
        pad.pack(fill="both", expand=True)
        ttk.Label(pad, text="Качване от телефона", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(pad, textvariable=self.status, font=("Segoe UI", 12)).pack(anchor="w", pady=(8, 4))
        ttk.Label(pad, textvariable=self.address, font=("Segoe UI", 12)).pack(anchor="w")
        ttk.Label(pad, textvariable=self.folder).pack(anchor="w", pady=(4, 10))
        ttk.Checkbutton(
            pad,
            text="Само на този компютър (телефонът няма да може да се върже)",
            variable=self.local,
            command=self.restart,
        ).pack(anchor="w", pady=(0, 8))

        buttons = ttk.Frame(pad)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="Старт", command=self.start).pack(side="left")
        ttk.Button(buttons, text="Спри", command=self.stop).pack(side="left", padx=6)
        ttk.Button(buttons, text="Копирай адреса", command=self.copy_address).pack(side="left", padx=6)
        ttk.Button(buttons, text="Отвори папката", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(buttons, text="Отвори страницата", command=self.open_page).pack(side="left", padx=6)

        self.log = tk.Text(pad, height=12, wrap="word", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start()
        self.root.after(800, self.refresh)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._error = ""
        local = self.local.get()

        def target() -> None:
            try:
                import atexit

                from app import _remove_runtime_files, build_server
                from network import resolve_host
                from settings import settings

                host = resolve_host("127.0.0.1" if local else settings.host)
                self._host = host
                self._server = build_server(host)
                atexit.register(_remove_runtime_files)
                self._server.run()
            except BaseException as exc:
                self._error = str(exc)

        self._thread = threading.Thread(target=target, name="phoneuploads-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=8)
        self._server = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def copy_address(self) -> None:
        value = self.address.get().strip()
        if not value.startswith("http"):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def open_folder(self) -> None:
        folder = self.folder.get().replace("Файлове: ", "").strip()
        if folder and Path(folder).exists():
            os.startfile(folder)  # type: ignore[attr-defined]

    def open_page(self) -> None:
        value = self.address.get().strip()
        if value.startswith("http"):
            webbrowser.open(value)

    def refresh(self) -> None:
        from settings import settings

        self.folder.set(f"Файлове: {settings.storage_dir}")
        alive = self._thread is not None and self._thread.is_alive()
        if self._error:
            self.status.set("Спрян е.")
            self.address.set(self._error)
        elif alive:
            url = self._url()
            if self._host in {"127.0.0.1", "::1"}:
                self.status.set("Работи само на този компютър.")
            else:
                self.status.set("Работи. Отвори този адрес на телефона, докато Tailscale е включен.")
            self.address.set(url or "Чака се адрес...")
        else:
            self.status.set("Спрян е.")
        self._show_log()
        self.root.after(1000, self.refresh)

    def _url(self) -> str:
        from settings import settings

        if settings.listen_file.is_file():
            try:
                import json

                payload = json.loads(settings.listen_file.read_text(encoding="ascii"))
                return str(payload.get("url") or "")
            except (OSError, json.JSONDecodeError):
                return ""
        if self._host:
            return f"http://{self._host}:{settings.port}/"
        return ""

    def _show_log(self) -> None:
        from settings import settings

        path = settings.log_dir / "server.log"
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
        except OSError:
            return
        self.log.delete("1.0", "end")
        self.log.insert("1.0", "\n".join(lines))

    def on_close(self) -> None:
        from tkinter import messagebox

        if messagebox.askokcancel(
            "Спиране",
            "Ако затвориш прозореца, качването от телефона спира.\n\nДа се спре ли?",
            parent=self.root,
        ):
            self.stop()
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _check_bundle() -> None:
    from settings import RESOURCE_DIR

    marker = Path(os.environ.get("TEMP", ".")) / "phoneuploads-check.txt"
    template = RESOURCE_DIR / "templates" / "login.html"
    marker.write_text(f"ok\n{RESOURCE_DIR}\n{template.is_file()}\n", encoding="utf-8")


def _ensure_stdio() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _smoke() -> None:
    import time
    import urllib.request

    from app import build_server
    from settings import settings

    server = build_server("127.0.0.1")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    body = b""
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{settings.port}/health", timeout=1) as response:
                body = response.read()
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.25)
    server.should_exit = True
    thread.join(timeout=8)
    marker = Path(os.environ.get("TEMP", ".")) / "phoneuploads-smoke.txt"
    ok = b"ok" in body.lower()
    marker.write_text("ok\n" if ok else f"fail\n{body!r}\n", encoding="utf-8")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    _ensure_stdio()
    if "--check" in sys.argv:
        _check_bundle()
        return
    if "--smoke" in sys.argv:
        _smoke()
        return
    _copy_into_app_folder()
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    if not _configured(home):
        if not _run_wizard():
            return
        _relaunch()
    if not _single_instance():
        _message("Програмата вече е отворена. Виж я в лентата на задачите.")
        return
    ControlWindow().run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _report_error(exc)
        raise SystemExit(1) from exc
