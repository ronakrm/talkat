"""Tests for talkat.main side-effect helpers — copy_to_clipboard, _notify,
save_transcript, run_calibrate.

These are pure-ish wrappers around subprocess calls and disk I/O; we stub the
subprocess layer with monkeypatch and verify the command dispatch / fallback
chain / config persistence.
"""

from __future__ import annotations

import subprocess

import pytest


class _Completed:
    """Stand-in for subprocess.CompletedProcess — just needs the attribute names."""

    returncode = 0
    stdout = b""
    stderr = b""


# ---------------------------------------------------------------------------
# copy_to_clipboard — wl-copy preferred, xclip fallback, both-missing → False
# ---------------------------------------------------------------------------


def test_copy_to_clipboard_prefers_wl_copy(monkeypatch: pytest.MonkeyPatch):
    from talkat import main as main_mod

    attempts: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        attempts.append(cmd[0])
        return _Completed()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)

    assert main_mod.copy_to_clipboard("hello") is True
    # wl-copy succeeded, so xclip should never be attempted.
    assert attempts == ["wl-copy"]


def test_copy_to_clipboard_falls_back_to_xclip_when_wl_copy_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import main as main_mod

    attempts: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        attempts.append(cmd[0])
        if cmd[0] == "wl-copy":
            raise FileNotFoundError("wl-copy not on PATH")
        return _Completed()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)

    assert main_mod.copy_to_clipboard("hello") is True
    assert attempts == ["wl-copy", "xclip"]


def test_copy_to_clipboard_returns_false_when_neither_tool_available(
    monkeypatch: pytest.MonkeyPatch,
):
    from talkat import main as main_mod

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        raise FileNotFoundError(f"{cmd[0]} not installed")

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)

    assert main_mod.copy_to_clipboard("hello") is False


def test_copy_to_clipboard_returns_false_when_both_tools_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """CalledProcessError from each tool must be treated as failure, not raised."""
    from talkat import main as main_mod

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)

    assert main_mod.copy_to_clipboard("hello") is False


# ---------------------------------------------------------------------------
# _notify — suppress FileNotFoundError if notify-send missing
# ---------------------------------------------------------------------------


def test_notify_suppresses_missing_notify_send(monkeypatch: pytest.MonkeyPatch):
    from talkat import main as main_mod

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        raise FileNotFoundError("notify-send not installed")

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)

    # Must not raise.
    main_mod._notify("hello, world")


def test_notify_dispatches_to_notify_send(monkeypatch: pytest.MonkeyPatch):
    from talkat import main as main_mod

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        calls.append(list(cmd))
        return _Completed()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)

    main_mod._notify("hello there")
    assert len(calls) == 1
    assert calls[0][0] == "notify-send"
    assert calls[0][1] == "Talkat"
    assert calls[0][2] == "hello there"


# ---------------------------------------------------------------------------
# save_transcript — appends to a timestamped file under the transcript dir
# ---------------------------------------------------------------------------


def test_save_transcript_writes_text_and_returns_path():
    from talkat.main import save_transcript

    path = save_transcript("hello from talkat", mode="short")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "hello from talkat" in content
    # Filenames are stamped with mode.
    assert path.name.endswith("_short.txt")


def test_save_transcript_appends_on_repeated_calls():
    from talkat.main import save_transcript

    p1 = save_transcript("first line", mode="long")
    # We can't guarantee the timestamp differs between calls within the same
    # second, so call save_transcript a second time and verify the second call's
    # output is present in some transcript file.
    p2 = save_transcript("second line", mode="long")
    # If they end up in the same file (same-second timestamp), it should
    # contain both lines.
    if p1 == p2:
        content = p1.read_text(encoding="utf-8")
        assert "first line" in content
        assert "second line" in content
    else:
        assert "first line" in p1.read_text(encoding="utf-8")
        assert "second line" in p2.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# run_calibrate — wires calibrate_microphone into config
# ---------------------------------------------------------------------------


def test_run_calibrate_persists_threshold_into_config(
    clean_config_file, monkeypatch: pytest.MonkeyPatch
):
    from talkat import main as main_mod

    monkeypatch.setattr(main_mod, "calibrate_microphone", lambda: 412.5)
    monkeypatch.setattr(main_mod, "_notify", lambda _m: None)

    rc = main_mod.run_calibrate()
    assert rc == 0

    from talkat.config import load_app_config

    cfg = load_app_config()
    assert cfg["silence_threshold"] == 412.5
