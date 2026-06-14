"""Install/uninstall the talkat user systemd service."""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from .logging_config import get_logger

logger = get_logger(__name__)


SERVICE_NAME = "talkat.service"
SERVICE_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
SERVICE_FILE = SERVICE_DIR / SERVICE_NAME


def _service_unit(python_exe: str) -> str:
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=Talkat Voice Dictation Server
        Documentation=https://github.com/ronakrm/talkat
        After=sound.target

        [Service]
        Type=simple
        ExecStart={python_exe} -m talkat.cli server
        Restart=on-failure
        RestartSec=5
        StandardOutput=journal
        StandardError=journal

        # Sandboxing — defence-in-depth for a per-user service.
        # ReadWritePaths punches holes through ProtectHome=read-only for the
        # talkat-owned XDG directories. XDG_RUNTIME_DIR (/run/user/$UID) is
        # outside $HOME so it stays writable without an explicit entry.
        NoNewPrivileges=yes
        PrivateTmp=yes
        ProtectSystem=strict
        ProtectHome=read-only
        ReadWritePaths=%h/.cache/talkat %h/.local/share/talkat %h/.config/talkat
        ProtectKernelTunables=yes
        ProtectKernelModules=yes
        ProtectControlGroups=yes
        RestrictNamespaces=yes
        RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
        LockPersonality=yes
        RestrictRealtime=yes
        RestrictSUIDSGID=yes
        # PrivateDevices stays off — we need /dev/snd for microphone capture.
        PrivateDevices=no

        [Install]
        WantedBy=default.target
        """
    )


def _systemctl(*args: str, check: bool = False) -> int:
    """Run ``systemctl --user <args>`` and return its exit code."""
    cmd = ["systemctl", "--user", *args]
    result = subprocess.run(cmd, check=check)
    return result.returncode


def install_service() -> int:
    """Write the service file, then daemon-reload, enable, and (re)start it."""
    if not shutil.which("systemctl"):
        logger.error("systemctl not found — this command requires systemd.")
        return 1

    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_FILE.write_text(_service_unit(sys.executable))
    logger.info(f"Wrote {SERVICE_FILE}")

    if _systemctl("daemon-reload") != 0:
        logger.error("systemctl --user daemon-reload failed")
        return 1
    if _systemctl("enable", SERVICE_NAME) != 0:
        logger.error(f"Failed to enable {SERVICE_NAME}")
        return 1
    if _systemctl("restart", SERVICE_NAME) != 0:
        logger.error(f"Failed to start {SERVICE_NAME}")
        return 1

    logger.info("Talkat service installed and running.")
    logger.info(f"  Status: systemctl --user status {SERVICE_NAME}")
    logger.info(f"  Logs:   journalctl --user -u {SERVICE_NAME} -f")
    return 0


def uninstall_service() -> int:
    """Stop, disable, and remove the user service file (idempotent)."""
    if not shutil.which("systemctl"):
        logger.error("systemctl not found — this command requires systemd.")
        return 1

    # Stop and disable are intentionally non-fatal — they may legitimately fail
    # if the service is already stopped or was never enabled.
    _systemctl("stop", SERVICE_NAME)
    _systemctl("disable", SERVICE_NAME)

    if SERVICE_FILE.exists():
        SERVICE_FILE.unlink()
        logger.info(f"Removed {SERVICE_FILE}")
    else:
        logger.info(f"No service file at {SERVICE_FILE}")

    _systemctl("daemon-reload")
    logger.info("Talkat service uninstalled.")
    return 0
