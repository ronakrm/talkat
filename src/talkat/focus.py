"""Focused-window tracking via compositor IPC (niri, Hyprland, sway).

Used by the focus guard in listen mode: the focused window is captured when
recording starts (that's the window the user intends to dictate into), and
re-checked just before typing. If focus moved in between, the transcript is
delivered via the clipboard instead of being typed into whatever window
happens to be focused now.

Everything here is best-effort: on an unsupported compositor or any IPC
failure the functions return ``None`` and the caller behaves exactly as it
did before the guard existed (always type).
"""

import json
import os
from typing import Any

from .logging_config import get_logger
from .security import safe_subprocess_run

logger = get_logger(__name__)

# Query timeout — compositor IPC answers in single-digit milliseconds; if it
# takes longer than this something is wrong and we'd rather skip the guard
# than stall dictation.
_IPC_TIMEOUT_S = 2


def _run_json(cmd: list[str]) -> Any:
    """Run a compositor IPC query and parse its JSON output; None on failure."""
    try:
        result = safe_subprocess_run(cmd, capture_output=True, text=True, timeout=_IPC_TIMEOUT_S)
        if result.returncode != 0:
            logger.debug(f"Focus query {cmd[0]} exited {result.returncode}")
            return None
        return json.loads(result.stdout)
    except Exception as e:
        logger.debug(f"Focus query failed ({cmd[0]}): {e}")
        return None


def _sway_find_focused(node: dict[str, Any]) -> dict[str, Any] | None:
    if node.get("focused"):
        return node
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        found = _sway_find_focused(child)
        if found is not None:
            return found
    return None


def compositor_name() -> str | None:
    """Which supported compositor's IPC is reachable from this session, if any."""
    if os.environ.get("NIRI_SOCKET"):
        return "niri"
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if os.environ.get("SWAYSOCK"):
        return "sway"
    return None


def get_focused_window() -> str | None:
    """Return an opaque identifier for the currently focused window.

    ``None`` means "unknown" — no supported compositor, IPC failure, or
    genuinely no focused window. Callers must treat ``None`` as "skip the
    guard", never as "focus changed".
    """
    compositor = compositor_name()

    if compositor == "niri":
        data = _run_json(["niri", "msg", "--json", "focused-window"])
        if isinstance(data, dict) and data.get("id") is not None:
            return f"niri:{data['id']}"
        return None

    if compositor == "hyprland":
        data = _run_json(["hyprctl", "-j", "activewindow"])
        if isinstance(data, dict) and data.get("address"):
            return f"hyprland:{data['address']}"
        return None

    if compositor == "sway":
        data = _run_json(["swaymsg", "-t", "get_tree"])
        if isinstance(data, dict):
            node = _sway_find_focused(data)
            if node is not None and node.get("id") is not None:
                return f"sway:{node['id']}"
        return None

    return None
