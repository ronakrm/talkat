"""Standard paths for Talkat following FHS and XDG specifications."""

import os
from pathlib import Path


def get_xdg_dir(env_var: str, default: str) -> Path:
    """Get XDG directory from environment or use default."""
    return Path(os.environ.get(env_var, os.path.expanduser(default)))


# XDG Base Directory Specification
XDG_CONFIG_HOME = get_xdg_dir("XDG_CONFIG_HOME", "~/.config")
XDG_CACHE_HOME = get_xdg_dir("XDG_CACHE_HOME", "~/.cache")
XDG_DATA_HOME = get_xdg_dir("XDG_DATA_HOME", "~/.local/share")
XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))

# Fall back to a cache subdir if XDG_RUNTIME_DIR doesn't exist on this system.
# Directory creation happens lazily in ensure_user_directories() — not at import time.
if not XDG_RUNTIME_DIR.exists():
    XDG_RUNTIME_DIR = XDG_CACHE_HOME / "talkat" / "runtime"

# Application-specific directories
APP_NAME = "talkat"

# Configuration files
CONFIG_DIR = XDG_CONFIG_HOME / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
DICTIONARY_FILE = CONFIG_DIR / "dictionary.txt"

# Cache directories
CACHE_DIR = XDG_CACHE_HOME / APP_NAME
MODEL_CACHE_DIR = CACHE_DIR / "models"
FASTER_WHISPER_CACHE_DIR = MODEL_CACHE_DIR / "faster-whisper"
VOSK_CACHE_DIR = MODEL_CACHE_DIR / "vosk"

# Data directories
DATA_DIR = XDG_DATA_HOME / APP_NAME
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
LOG_DIR = DATA_DIR / "logs"
DIAGNOSTICS_DIR = DATA_DIR / "diagnostics"

# Runtime directories
RUNTIME_DIR = XDG_RUNTIME_DIR / APP_NAME
PID_DIR = RUNTIME_DIR
LOCK_DIR = RUNTIME_DIR
SOCKET_FILE = RUNTIME_DIR / "server.sock"

# System-wide paths (when installed system-wide)
SYSTEM_CONFIG_DIR = Path("/etc") / APP_NAME
SYSTEM_CONFIG_FILE = SYSTEM_CONFIG_DIR / "config.json"
SYSTEM_DATA_DIR = Path("/usr/share") / APP_NAME
SYSTEM_LIB_DIR = Path("/usr/lib") / APP_NAME


def ensure_user_directories() -> None:
    """Create all necessary user directories."""
    dirs = [
        CONFIG_DIR,
        CACHE_DIR,
        MODEL_CACHE_DIR,
        FASTER_WHISPER_CACHE_DIR,
        VOSK_CACHE_DIR,
        DATA_DIR,
        TRANSCRIPT_DIR,
        LOG_DIR,
        DIAGNOSTICS_DIR,
        RUNTIME_DIR,
        PID_DIR,
        LOCK_DIR,
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)


def get_config_file() -> Path:
    """Return the writable user config file path.

    This is the path ``save_app_config`` writes to and the one users edit
    by hand. For loading the effective config, callers want
    :func:`get_config_files` — it returns the full merge chain instead of
    a single file.
    """
    return CONFIG_FILE


def get_config_files() -> list[Path]:
    """Return the config files to merge, in low-to-high precedence order.

    Order is ``[/etc/talkat/config.json, ~/.config/talkat/config.json]``.
    Files that don't exist are omitted. Callers (i.e. ``load_app_config``)
    walk this list and apply each layer over the previous so that:

    * a sysadmin-shipped ``/etc`` default (delivered by future .deb/.rpm
      packages) sets organization-wide values
    * the user's ``~/.config`` partially overrides those values

    Today's behavior was wholesale-shadow: any user config made the
    system config invisible. Switching to merge means a user can override
    just one field without restating every key.
    """
    files: list[Path] = []
    if SYSTEM_CONFIG_FILE.exists():
        files.append(SYSTEM_CONFIG_FILE)
    if CONFIG_FILE.exists():
        files.append(CONFIG_FILE)
    return files
