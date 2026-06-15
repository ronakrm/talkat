"""Clipboard helper — wl-copy (Wayland) → xclip (X11) fallback chain.

Single source of truth for clipboard writes. Both ``main.listen_continuous``
and ``file_processor.process_audio_file_command`` route through here so we
don't duplicate the fallback logic in two places.
"""

import subprocess

from .logging_config import get_logger
from .security import safe_subprocess_run, sanitize_text_for_clipboard

logger = get_logger(__name__)


def copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the system clipboard.

    Tries ``wl-copy`` first (Wayland-native), falls back to ``xclip`` for
    legacy X11 sessions / XWayland setups. Input is sanitized via
    ``sanitize_text_for_clipboard`` before either tool sees it.

    Returns True on success, False if neither tool is installed or both
    failed. Callers decide how loud to be about the failure — for short
    dictation the typed text is the primary output, clipboard is a bonus;
    for long dictation the file on disk is the primary output.
    """
    text = sanitize_text_for_clipboard(text)

    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            safe_subprocess_run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    return False
