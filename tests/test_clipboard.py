"""Tests for talkat.clipboard.copy_to_clipboard — wl-copy then xclip fallback.

These tests moved here from test_main_helpers.py when the clipboard helper
was extracted into its own module (so main.py and file_processor.py share
one implementation instead of duplicating it).
"""

from __future__ import annotations

import subprocess

import pytest


class _Completed:
    """Stand-in for subprocess.CompletedProcess — just needs the attribute names."""

    returncode = 0
    stdout = b""
    stderr = b""


def test_copy_to_clipboard_prefers_wl_copy(monkeypatch: pytest.MonkeyPatch):
    """First-choice tool is wl-copy; xclip is never reached when wl-copy succeeds."""
    from talkat import clipboard as clip_mod

    attempts: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _Completed:
        attempts.append(cmd[0])
        return _Completed()

    monkeypatch.setattr(clip_mod, "safe_subprocess_run", fake_run)

    assert clip_mod.copy_to_clipboard("hello") is True
    assert attempts == ["wl-copy"]


def test_copy_to_clipboard_falls_back_to_xclip_when_wl_copy_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """FileNotFoundError on wl-copy must trigger the xclip fallback."""
    from talkat import clipboard as clip_mod

    attempts: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _Completed:
        attempts.append(cmd[0])
        if cmd[0] == "wl-copy":
            raise FileNotFoundError("wl-copy not on PATH")
        return _Completed()

    monkeypatch.setattr(clip_mod, "safe_subprocess_run", fake_run)

    assert clip_mod.copy_to_clipboard("hello") is True
    assert attempts == ["wl-copy", "xclip"]


def test_copy_to_clipboard_returns_false_when_neither_tool_available(
    monkeypatch: pytest.MonkeyPatch,
):
    """Both tools missing → False, no exception."""
    from talkat import clipboard as clip_mod

    def fake_run(cmd: list[str], **_kwargs: object) -> _Completed:
        raise FileNotFoundError(f"{cmd[0]} not installed")

    monkeypatch.setattr(clip_mod, "safe_subprocess_run", fake_run)

    assert clip_mod.copy_to_clipboard("hello") is False


def test_copy_to_clipboard_returns_false_when_both_tools_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """CalledProcessError from each tool must be treated as failure, not raised."""
    from talkat import clipboard as clip_mod

    def fake_run(cmd: list[str], **_kwargs: object) -> _Completed:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(clip_mod, "safe_subprocess_run", fake_run)

    assert clip_mod.copy_to_clipboard("hello") is False


def test_copy_to_clipboard_sanitizes_text_before_sending(
    monkeypatch: pytest.MonkeyPatch,
):
    """The text reaching the subprocess must have been through sanitize_text_for_clipboard.

    Specifically, null bytes are stripped — that's the most user-visible
    invariant the sanitizer enforces.
    """
    from talkat import clipboard as clip_mod

    received: dict[str, bytes] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> _Completed:
        received["payload"] = kwargs["input"]  # type: ignore[assignment]
        return _Completed()

    monkeypatch.setattr(clip_mod, "safe_subprocess_run", fake_run)

    clip_mod.copy_to_clipboard("hello\x00world")
    assert received["payload"] == b"helloworld"
