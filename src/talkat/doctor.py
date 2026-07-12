"""Environment self-check: ``talkat doctor``.

Answers "which talkat am I actually running, and is everything it needs
alive?" — the questions that come up whenever an install goes stale or two
installs shadow each other (uv tool vs distro package, user systemd unit vs
packaged unit).

Output is plain prints, not logging — the report IS the product. Exit code
is 1 if any check fails outright (✗), 0 otherwise; warnings (!) don't fail.
"""

import importlib.metadata
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from .config import CODE_DEFAULTS, load_app_config
from .focus import compositor_name
from .paths import CONFIG_FILE, RUNTIME_DIR, SYSTEM_CONFIG_FILE

OK = "✓"
BAD = "✗"
WARN = "!"

PACKAGED_UNIT = Path("/usr/lib/systemd/user/talkat.service")


class _Report:
    """Collects check lines and remembers whether anything failed."""

    def __init__(self) -> None:
        self.failed = False

    def line(self, mark: str, label: str, detail: str = "") -> None:
        if mark == BAD:
            self.failed = True
        print(f" {mark} {label}" + (f": {detail}" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n{title}")


def _client_version() -> str:
    try:
        return importlib.metadata.version("talkat")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _install_origin() -> str:
    path = str(Path(__file__).resolve())
    if "/uv/tools/" in path:
        return "uv tool install"
    if path.startswith("/usr/"):
        return "system package"
    if "/site-packages/" in path:
        return "virtualenv"
    return "source checkout"


def _talkat_binaries_on_path() -> list[str]:
    """Every distinct `talkat` executable reachable via PATH, in PATH order."""
    found: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = Path(d) / "talkat"
        if cand.is_file() and os.access(cand, os.X_OK) and str(cand) not in found:
            found.append(str(cand))
    return found


def _systemd_unit_info() -> tuple[str | None, str | None]:
    """(FragmentPath, ActiveState) of the user talkat.service, best-effort."""
    if not shutil.which("systemctl"):
        return None, None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "talkat", "--property=FragmentPath,ActiveState"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value
    return props.get("FragmentPath") or None, props.get("ActiveState") or None


def _socket_exists(socket_path: str) -> bool:
    return Path(socket_path).is_socket()


def _server_health(socket_path: str) -> dict[str, Any] | None:
    try:
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, timeout=2.0) as client:
            response = client.get("http://talkat/health")
            if response.status_code != 200:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return None


def _check_install(report: _Report) -> None:
    report.section("Install")
    version = _client_version()
    report.line(OK, "talkat", f"{version} ({_install_origin()}, {Path(__file__).parent})")

    binaries = _talkat_binaries_on_path()
    if len(binaries) > 1:
        report.line(
            WARN,
            "multiple `talkat` executables on PATH",
            f"{binaries[0]} wins; shadowed: {', '.join(binaries[1:])}",
        )
        report.line(
            WARN,
            "hint",
            "if the shadowing copy is a stale dev install, remove it "
            "(`uv tool uninstall talkat`) and use ./dev.sh for development",
        )
    elif binaries:
        report.line(OK, "PATH resolves talkat", binaries[0])
    else:
        report.line(WARN, "talkat not on PATH", "running via `python -m talkat.cli`?")


def _check_service(report: _Report) -> None:
    report.section("Model server")
    fragment, active = _systemd_unit_info()
    if fragment is None:
        report.line(WARN, "systemd user unit", "not found (server must be run manually)")
    else:
        mark = OK if active == "active" else BAD
        report.line(mark, "talkat.service", f"{active} ({fragment})")
        if PACKAGED_UNIT.exists() and fragment != str(PACKAGED_UNIT):
            report.line(
                WARN,
                "unit shadowing",
                f"{fragment} overrides the packaged unit at {PACKAGED_UNIT}; "
                "remove it (`talkat uninstall-service`) to use the packaged service",
            )

    config = load_app_config()
    socket_path = str(config.get("server_socket", CODE_DEFAULTS["server_socket"]))
    if _socket_exists(socket_path):
        report.line(OK, "socket", socket_path)
    else:
        report.line(BAD, "socket", f"{socket_path} missing — is the service running?")

    health = _server_health(socket_path)
    if health is None:
        report.line(BAD, "health", "server not responding")
        return
    server_version = str(health.get("version", "pre-1.1"))
    report.line(
        OK,
        "health",
        f"{health.get('model_type')}/{health.get('model_name')} (server {server_version})",
    )
    client_version = _client_version()
    if server_version != client_version:
        report.line(
            WARN,
            "version skew",
            f"client {client_version} vs server {server_version} — "
            "restart the service after upgrades (`systemctl --user restart talkat`)",
        )


def _check_desktop_tools(report: _Report) -> None:
    report.section("Desktop integration")
    if shutil.which("ydotool"):
        ydotool_socket = os.environ.get(
            "YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket"
        )
        if Path(ydotool_socket).exists():
            report.line(OK, "ydotool", f"daemon socket {ydotool_socket}")
        else:
            report.line(
                BAD,
                "ydotoold not running",
                f"no socket at {ydotool_socket} — typed output will fall back to clipboard",
            )
    else:
        report.line(BAD, "ydotool missing", "typed output will fall back to clipboard")

    if shutil.which("wl-copy") or shutil.which("xclip"):
        report.line(OK, "clipboard", "wl-copy" if shutil.which("wl-copy") else "xclip")
    else:
        report.line(WARN, "clipboard", "neither wl-copy nor xclip found")

    if shutil.which("notify-send"):
        report.line(OK, "notifications", "notify-send")
    else:
        report.line(WARN, "notifications", "notify-send missing (status toasts disabled)")

    compositor = compositor_name()
    config = load_app_config()
    if not config.get("focus_guard", CODE_DEFAULTS["focus_guard"]):
        report.line(WARN, "focus guard", "disabled in config")
    elif compositor:
        report.line(OK, "focus guard", f"active ({compositor})")
    else:
        report.line(WARN, "focus guard", "inactive (no supported compositor IPC detected)")


def _check_audio(report: _Report) -> None:
    report.section("Audio")
    # Imported lazily — PortAudio enumeration is the slowest part of doctor.
    import pyaudio

    from .devices import input_devices
    from .record import _suppress_native_stderr

    try:
        with _suppress_native_stderr():
            p = pyaudio.PyAudio()
            try:
                devices = input_devices(p)
                try:
                    default = p.get_default_input_device_info()
                    default_name = str(default.get("name", "?"))
                except OSError:
                    default_name = None
            finally:
                p.terminate()
    except Exception as e:
        report.line(BAD, "audio subsystem", f"PortAudio failed: {e}")
        return

    if not devices:
        report.line(BAD, "input devices", "none found")
        return
    report.line(OK, "input devices", str(len(devices)))
    if default_name:
        report.line(OK, "default input", default_name)
    else:
        report.line(WARN, "default input", "none — set `input_device_name` in config")
    config = load_app_config()
    pinned = config.get("input_device_name")
    if pinned:
        matched = any(pinned.lower() in name.lower() for _, name in devices)
        mark = OK if matched else WARN
        detail = f"pinned to {pinned!r}" + ("" if matched else " — no current device matches")
        report.line(mark, "input_device_name", detail)


def _check_config(report: _Report) -> None:
    report.section("Config & paths")
    if SYSTEM_CONFIG_FILE.exists():
        report.line(OK, "system config", str(SYSTEM_CONFIG_FILE))
    if CONFIG_FILE.exists():
        report.line(OK, "user config", str(CONFIG_FILE))
    else:
        report.line(OK, "user config", "none (using defaults; run `talkat calibrate` to create)")
    report.line(OK, "runtime dir", str(RUNTIME_DIR))
    if os.environ.get("TALKAT_RUNTIME_DIR"):
        report.line(
            WARN,
            "TALKAT_RUNTIME_DIR override active",
            "this process is isolated from the installed service (dev mode)",
        )


def run_doctor() -> int:
    """Run all checks; returns 0 when healthy, 1 if anything failed."""
    report = _Report()
    print("talkat doctor")
    _check_install(report)
    _check_service(report)
    _check_desktop_tools(report)
    _check_audio(report)
    _check_config(report)
    print()
    if report.failed:
        print("Result: problems found (✗ above)")
        return 1
    print("Result: healthy")
    return 0
