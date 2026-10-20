"""Decide which address the server is allowed to listen on.

Binding to a Tailscale address is the control that keeps the port off the
public internet and off the home LAN. A firewall rule is a second layer, not
a substitute: rules can be disabled, named after the wrong adapter, or
overridden. This module therefore refuses 0.0.0.0, IPv6 any-address, and
ordinary LAN addresses.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from pathlib import Path

TAILSCALE_NET = ipaddress.IPv4Network("100.64.0.0/10")
_CREATE_NO_WINDOW = 0x08000000


def is_tailscale_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(value.strip())
    except (ValueError, AttributeError):
        return False
    return ip in TAILSCALE_NET


def validate_bind_host(host: str) -> str:
    host = (host or "").strip()
    if host in {"127.0.0.1", "::1"}:
        return host
    if is_tailscale_ipv4(host):
        return host
    raise SystemExit(
        f"Refusing to listen on {host!r}. This server only binds to a Tailscale "
        "IPv4 address (100.64.0.0/10) or to 127.0.0.1 for local testing. "
        "It will not bind to 0.0.0.0, '::', or a LAN address."
    )


def bind_mode(host: str) -> str:
    if host in {"127.0.0.1", "::1"}:
        return "local"
    if is_tailscale_ipv4(host):
        return "tailscale"
    return "rejected"


def parse_tailscale_ip_output(text: str) -> str | None:
    for line in (text or "").splitlines():
        candidate = line.strip()
        if is_tailscale_ipv4(candidate):
            return candidate
    return None


def choose_tailscale_ip(addresses: list[tuple[str, str]]) -> str | None:
    """Pick a Tailscale address from (ip, interface_alias) pairs.

    Prefer an interface whose name contains "Tailscale" so a stray CGNAT
    address on another adapter is not treated as the tailnet.
    """
    tagged = [(ip.strip(), alias or "") for ip, alias in addresses if is_tailscale_ipv4(ip)]
    preferred = [ip for ip, alias in tagged if "tailscale" in alias.lower()]
    if preferred:
        return preferred[0]
    if len(tagged) == 1:
        return tagged[0][0]
    return None


def tailscale_executable() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Tailscale\tailscale.exe"),
        Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Tailscale" / "tailscale.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        return subprocess.run(args, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None


def tailscale_ip_from_cli() -> str | None:
    exe = tailscale_executable()
    if not exe:
        return None
    completed = _run([exe, "ip", "-4"], timeout=15)
    if completed is None:
        return None
    return parse_tailscale_ip_output(completed.stdout)


def windows_ipv4_addresses() -> list[tuple[str, str]]:
    if os.name != "nt":
        return []
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress"
    )
    completed = _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=40,
    )
    if completed is None or not completed.stdout.strip():
        return []
    raw = completed.stdout.strip().lstrip("\ufeff")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    found: list[tuple[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("IPAddress") or "").strip()
        alias = str(item.get("InterfaceAlias") or "")
        if ip:
            found.append((ip, alias))
    return found


def detect_tailscale_ipv4() -> str | None:
    cli_ip = tailscale_ip_from_cli()
    if cli_ip:
        return cli_ip
    return choose_tailscale_ip(windows_ipv4_addresses())


def resolve_host(setting: str) -> str:
    setting = (setting or "").strip()
    if setting.lower() == "auto":
        ip = detect_tailscale_ipv4()
        if not ip:
            raise SystemExit(
                "Tailscale IPv4 address not found. Connect Tailscale on this PC, "
                "or run .\\start.ps1 -Local to test only from this computer."
            )
        return validate_bind_host(ip)
    return validate_bind_host(setting)
