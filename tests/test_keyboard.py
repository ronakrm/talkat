"""Tests for talkat.keyboard — the modifier guard's view of the physical keyboard.

No real input devices are touched: devices are plain files under tmp_path,
sysfs capability bitmaps are copied from a real ThinkPad, and the key-state
ioctl is faked per device path.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from talkat import keyboard as keyboard_mod

KEY_A = 30
KEY_LEFTMETA = 125

# /sys/class/input/eventN/device/capabilities/key, verbatim from a ThinkPad
# X1 Carbon: the kernel prints hex unsigned longs, most-significant first,
# skipping leading all-zero words.
AT_KEYBOARD = "402000007 ff803078f800d001 feffffdfffcfffff fffffffffffffffe"
YDOTOOLD_VIRTUAL = (
    "ffffffffff 0 ffffff0003007f 1000f7fffffff 7fe001fffff000f 7ffffffffffffff "
    "ffffffff0003fdff 7fff8fff00ff03ff 1ffffffffffff07 ffffffffffffffff "
    "ffffffffffefffff fffffffffffffffe"
)
TOUCHPAD = "e520 10000 0 0 0 0"
THINKPAD_EXTRA_BUTTONS = (
    "400000000010040 40000 18040000 f000000000000000 50010000000000 0 "
    "101701b02102c04 280051195000 10e000000000000 0"
)


def test_eviocgkey_matches_the_kernel_macro():
    # EVIOCGKEY(len) = _IOC(_IOC_READ, 'E', 0x18, len) with len = KEY_MAX/8 + 1 = 96.
    assert keyboard_mod._EVIOCGKEY == 0x80604518


@pytest.mark.parametrize(
    ("bitmap", "expected"),
    [
        (AT_KEYBOARD, True),
        (YDOTOOLD_VIRTUAL, True),
        (TOUCHPAD, False),
        (THINKPAD_EXTRA_BUTTONS, False),
        ("0", False),
    ],
)
def test_has_modifier_keys_reads_sysfs_bitmaps(bitmap: str, expected: bool):
    assert keyboard_mod.has_modifier_keys(bitmap) is expected


# ---------------------------------------------------------------------------
# ModifierWatch over fake devices
# ---------------------------------------------------------------------------


class FakeInput:
    """Fake /dev/input + /sys/class/input trees with scriptable key state."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dev = root / "dev"
        self.sysfs = root / "sys"
        self.dev.mkdir()
        self.sysfs.mkdir()
        # device path -> callable returning the keycodes currently down
        self.pressed: dict[str, Callable[[], set[int]]] = {}
        self.ioctl_calls = 0
        monkeypatch.setattr(keyboard_mod.fcntl, "ioctl", self._ioctl)

    def add(self, name: str, capabilities: str, pressed: set[int] | None = None) -> Path:
        path = self.dev / name
        path.write_bytes(b"")
        caps = self.sysfs / name / "device" / "capabilities"
        caps.mkdir(parents=True)
        (caps / "key").write_text(capabilities + "\n")
        down = pressed or set()
        self.pressed[str(path)] = lambda: down
        return path

    def watch(self) -> keyboard_mod.ModifierWatch:
        return keyboard_mod.ModifierWatch(
            device_glob=str(self.dev / "event*"), sysfs_dir=str(self.sysfs)
        )

    def _ioctl(self, fd: int, request: int, buf: bytearray) -> int:
        assert request == keyboard_mod._EVIOCGKEY
        self.ioctl_calls += 1
        for code in self.pressed[os.readlink(f"/proc/self/fd/{fd}")]():
            buf[code // 8] |= 1 << (code % 8)
        return 0


@pytest.fixture
def fake_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeInput:
    return FakeInput(tmp_path, monkeypatch)


def test_watch_opens_only_devices_with_modifier_keys(fake_input: FakeInput):
    fake_input.add("event3", AT_KEYBOARD)
    fake_input.add("event9", TOUCHPAD)
    fake_input.add("event15", YDOTOOLD_VIRTUAL)

    with fake_input.watch() as watch:
        assert watch.keyboard_count == 2
        assert watch.held() is False


def test_held_sees_a_modifier_down_on_any_keyboard(fake_input: FakeInput):
    fake_input.add("event3", AT_KEYBOARD, pressed={KEY_A})
    fake_input.add("event15", YDOTOOLD_VIRTUAL, pressed={KEY_LEFTMETA})

    with fake_input.watch() as watch:
        assert watch.held() is True


def test_held_ignores_ordinary_keys(fake_input: FakeInput):
    fake_input.add("event3", AT_KEYBOARD, pressed={KEY_A})

    with fake_input.watch() as watch:
        assert watch.held() is False


def test_no_readable_keyboard_means_guard_off(fake_input: FakeInput):
    """Can't tell ≠ modifier held: None lets typing proceed unguarded."""
    fake_input.add("event9", TOUCHPAD)
    unreadable = fake_input.add("event3", AT_KEYBOARD, pressed={KEY_LEFTMETA})
    unreadable.chmod(0)
    if os.access(unreadable, os.R_OK):
        pytest.skip("running as root: chmod can't make the fake device unreadable")

    with fake_input.watch() as watch:
        assert watch.keyboard_count == 0
        assert watch.held() is None
        assert watch.wait_released(timeout=0) is True


def test_unplugged_keyboard_is_skipped(fake_input: FakeInput, monkeypatch: pytest.MonkeyPatch):
    fake_input.add("event3", AT_KEYBOARD, pressed={KEY_LEFTMETA})

    def gone(fd: int, request: int, buf: bytearray) -> int:
        raise OSError(19, "No such device")

    with fake_input.watch() as watch:
        monkeypatch.setattr(keyboard_mod.fcntl, "ioctl", gone)
        assert watch.held() is None


def test_wait_released_returns_once_the_modifier_is_released(fake_input: FakeInput):
    reads = iter([{KEY_LEFTMETA}, {KEY_LEFTMETA}, set()])
    path = fake_input.add("event3", AT_KEYBOARD)
    fake_input.pressed[str(path)] = lambda: next(reads, set())

    with fake_input.watch() as watch:
        assert watch.wait_released(timeout=5.0) is True
    assert fake_input.ioctl_calls == 3


def test_wait_released_gives_up_after_timeout(fake_input: FakeInput):
    fake_input.add("event3", AT_KEYBOARD, pressed={KEY_LEFTMETA})

    with fake_input.watch() as watch:
        assert watch.wait_released(timeout=0.05) is False


def test_exit_closes_every_device(fake_input: FakeInput):
    fake_input.add("event3", AT_KEYBOARD)
    fake_input.add("event15", YDOTOOLD_VIRTUAL)

    with fake_input.watch() as watch:
        fds = list(watch._fds)
        assert len(fds) == 2
    assert watch.keyboard_count == 0
    for fd in fds:
        with pytest.raises(OSError):
            fcntl.fcntl(fd, fcntl.F_GETFD)
