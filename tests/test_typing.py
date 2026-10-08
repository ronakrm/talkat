"""Typing pace — typing_key_hold_ms / typing_key_delay_ms — and how keystrokes go out.

Nothing is ever typed: safe_subprocess_run is patched to record each ydotool
call, time.sleep to record each pause, and the modifier watch is a stub that
logs its checks — so tests can assert the exact order of keys, pauses and
guard checks.
"""

from __future__ import annotations

import subprocess
import threading
from typing import Any

import pytest

from talkat import main as main_mod
from talkat.config import CODE_DEFAULTS
from talkat.security import validate_json_config


class _Modifiers:
    """Stub ModifierWatch: ``held()`` pops from ``script`` (False once empty)."""

    def __init__(self, log: list[tuple[Any, ...]], script: list[bool]) -> None:
        self.log = log
        self.script = script

    def __enter__(self) -> _Modifiers:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def held(self) -> bool:
        self.log.append(("held",))
        return self.script.pop(0) if self.script else False

    def wait_released(self, timeout: float) -> bool:
        self.log.append(("wait",))
        return True


@pytest.fixture
def typing_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch every way typing could reach the desktop; record what it does, in order."""
    env: dict[str, Any] = {"log": [], "calls": [], "held": []}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        env["calls"].append((list(command), kwargs))
        env["log"].append(("key", command[-1]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)
    monkeypatch.setattr(main_mod, "ModifierWatch", lambda: _Modifiers(env["log"], env["held"]))
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: env["log"].append(("sleep", s)))
    monkeypatch.setattr(main_mod, "get_focused_window", lambda: None)
    monkeypatch.setattr(main_mod, "_notify", lambda message: None)
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: True)
    return env


def _type(text: str, hold: int = 5, delay: int = 0) -> tuple[int, str | None]:
    pace = main_mod._TypingPace(key_hold_ms=hold, key_delay_ms=delay)
    return main_mod._type_text(text, None, threading.Event(), pace)


def _keys_and_pauses(env: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [event for event in env["log"] if event[0] in ("key", "sleep")]


def test_defaults_hold_each_key_5_ms_with_no_pause() -> None:
    assert CODE_DEFAULTS["typing_key_hold_ms"] == 5
    assert CODE_DEFAULTS["typing_key_delay_ms"] == 0
    assert main_mod._TypingPace.from_config({}) == main_mod._TypingPace(5, 0)


def test_each_key_is_one_ydotool_call_held_for_the_configured_time(typing_env) -> None:
    assert _type("Hi!", hold=7) == (3, None)

    assert [argv for argv, _ in typing_env["calls"]] == [
        ["ydotool", "type", "--key-hold=7", "--key-delay=0", "--escape=0", "--", char]
        for char in "Hi!"
    ]


def test_ydotool_exit_is_awaited_on_a_pipe(typing_env) -> None:
    """With a piped stdout, subprocess.run's timeout wait wakes the moment
    ydotool exits; inherited, it polls with doubling sleeps and rounds every
    keystroke up (20 ms took 31)."""
    _type("ab")

    for _, kwargs in typing_env["calls"]:
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["check"] is True


def test_no_pause_by_default(typing_env) -> None:
    _type("abc")

    assert _keys_and_pauses(typing_env) == [("key", "a"), ("key", "b"), ("key", "c")]


def test_the_pause_falls_between_keystrokes_only(typing_env) -> None:
    _type("abc", delay=8)

    assert _keys_and_pauses(typing_env) == [
        ("key", "a"),
        ("sleep", 0.008),
        ("key", "b"),
        ("sleep", 0.008),
        ("key", "c"),
    ]


def test_skipped_characters_add_no_pause(typing_env) -> None:
    _type("a—b", delay=8)  # non-ASCII never reaches ydotool

    assert _keys_and_pauses(typing_env) == [("key", "a"), ("sleep", 0.008), ("key", "b")]


def test_a_modifier_pressed_during_the_pause_is_caught_before_the_next_key(typing_env) -> None:
    typing_env["held"].extend([False, True])

    _type("ab", delay=8)

    assert typing_env["log"] == [
        ("held",),
        ("key", "a"),
        ("sleep", 0.008),
        ("held",),
        ("wait",),
        ("key", "b"),
    ]


def test_config_sets_the_pace(typing_env) -> None:
    config = {"typing_key_hold_ms": 12, "typing_key_delay_ms": 3}

    main_mod._deliver_text("ok", config, focus_before=None)

    assert [argv[2] for argv, _ in typing_env["calls"]] == ["--key-hold=12", "--key-hold=12"]
    assert _keys_and_pauses(typing_env) == [("key", "o"), ("sleep", 0.003), ("key", "k")]


def test_fractional_settings_round_down_to_whole_ms() -> None:
    config = {"typing_key_hold_ms": 2.9, "typing_key_delay_ms": 1.5}

    assert main_mod._TypingPace.from_config(config) == main_mod._TypingPace(2, 1)


@pytest.mark.parametrize("key", ["typing_key_hold_ms", "typing_key_delay_ms"])
@pytest.mark.parametrize("value", [0, 5, 100])
def test_pace_settings_accept_0_to_100_ms(key: str, value: int) -> None:
    assert validate_json_config({key: value}) == {key: value}


@pytest.mark.parametrize("key", ["typing_key_hold_ms", "typing_key_delay_ms"])
@pytest.mark.parametrize("value", [-1, 101, "fast", None])
def test_pace_settings_reject_out_of_range_or_non_numbers(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        validate_json_config({key: value})
