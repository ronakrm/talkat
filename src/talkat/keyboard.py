"""Physically held modifier keys, read straight from the kernel's evdev nodes.

Keystrokes typed through ydotoold's virtual keyboard share the seat's modifier
state with every real keyboard (niri keeps one modifier state per seat). If
Super is down while talkat types "the tool", the compositor sees Super+T,
Super+H, Super+E, ... and runs those shortcuts — spawning terminals, moving
focus — instead of typing. The typing loop in ``main.py`` checks
:class:`ModifierWatch` before every keystroke.

Reading ``/dev/input/event*`` needs the ``input`` group, which ydotoold's own
``/dev/uinput`` access usually comes with. Everything here is best-effort:
when no keyboard is readable the watch is inactive, :meth:`ModifierWatch.held`
returns ``None``, and typing proceeds unguarded — exactly as it did before the
guard existed.
"""

import contextlib
import fcntl
import glob
import os
import struct
import time
from types import TracebackType

from .logging_config import get_logger

logger = get_logger(__name__)

# Left/right Ctrl, Shift, Alt and Meta (Super), from linux/input-event-codes.h.
MODIFIER_KEYCODES = (29, 97, 42, 54, 56, 100, 125, 126)

_KEY_MAX = 0x2FF
_KEY_STATE_BYTES = _KEY_MAX // 8 + 1
# EVIOCGKEY(len) = _IOC(_IOC_READ, 'E', 0x18, len): bitmap of the keys currently down.
_EVIOCGKEY = (2 << 30) | (_KEY_STATE_BYTES << 16) | (ord("E") << 8) | 0x18
# sysfs prints capability bitmaps as space-separated hex unsigned longs.
_BITS_PER_LONG = struct.calcsize("L") * 8
_POLL_INTERVAL_S = 0.02


def has_modifier_keys(capabilities_key: str) -> bool:
    """Whether a sysfs ``capabilities/key`` bitmap includes any modifier key."""
    bits = 0
    for word in capabilities_key.split():  # most-significant word first
        bits = (bits << _BITS_PER_LONG) | int(word, 16)
    return any(bits >> code & 1 for code in MODIFIER_KEYCODES)


class ModifierWatch:
    """Keeps every modifier-capable keyboard open for cheap, repeated key-state reads.

    Use as a context manager around one typing run. A key-state ioctl costs
    well under a microsecond, but closing an evdev node costs milliseconds
    (the kernel waits out an RCU grace period) — so devices are opened once
    per run, never per check.
    """

    def __init__(
        self,
        device_glob: str = "/dev/input/event*",
        sysfs_dir: str = "/sys/class/input",
    ) -> None:
        self._device_glob = device_glob
        self._sysfs_dir = sysfs_dir
        self._fds: list[int] = []

    def __enter__(self) -> "ModifierWatch":
        for path in sorted(glob.glob(self._device_glob)):
            capabilities = os.path.join(
                self._sysfs_dir, os.path.basename(path), "device", "capabilities", "key"
            )
            try:
                with open(capabilities) as f:
                    if not has_modifier_keys(f.read()):
                        continue  # touchpads, lid switch, audio jacks, ...
                self._fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except (OSError, ValueError) as e:
                logger.debug(f"Modifier guard skipping {path}: {e}")
        if self._fds:
            logger.debug(f"Modifier guard watching {len(self._fds)} keyboard(s)")
        else:
            logger.debug("Modifier guard inactive: no readable keyboard under /dev/input")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for fd in self._fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds = []

    @property
    def keyboard_count(self) -> int:
        return len(self._fds)

    def held(self) -> bool | None:
        """True if a modifier is down on any watched keyboard; None if none is readable."""
        readable = False
        for fd in self._fds:
            state = bytearray(_KEY_STATE_BYTES)
            try:
                fcntl.ioctl(fd, _EVIOCGKEY, state)
            except OSError:
                continue  # unplugged mid-run
            readable = True
            if any(state[code // 8] >> (code % 8) & 1 for code in MODIFIER_KEYCODES):
                return True
        return False if readable else None

    def wait_released(self, timeout: float) -> bool:
        """Block until no modifier is held; False if one is still held after ``timeout``."""
        deadline = time.monotonic() + timeout
        while self.held():
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_INTERVAL_S)
        return True
