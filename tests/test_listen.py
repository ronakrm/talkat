"""Tests for talkat.main.run_dictation — what happens to the transcript.

DictationSession is replaced by a fake that replays scripted segment results
through ``on_result`` (the real session has its own tests in
test_session.py), and safe_subprocess_run is patched to capture ydotool and
notify-send invocations. What we pin down: pieces are typed as they arrive,
whatever can't be typed goes to the clipboard with a notification that says
so, and the typing guards (modifier keys, focus, stop signals) hold.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from talkat.session import SegmentResult, SessionOutcome

MARKER = "[untranscribed audio: talkat file /tmp/untranscribed/x_001.wav]"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    saved = {sig: signal.getsignal(sig) for sig in sigs}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            try:
                signal.signal(sig, handler)
            except (TypeError, ValueError):
                pass


def piece(text: str, failed: bool = False) -> SegmentResult:
    return SegmentResult(index=0, text=text, seconds=1.0, cut="pause", failed=failed)


class _FakeSession:
    """Stand-in for DictationSession: replays ``state["script"]`` through on_result.

    Script items are SegmentResults, plain strings (successful pieces), or
    callables run between deliveries with the session (to change focus,
    abort, assert on what's been typed so far, ...).
    """

    def __init__(self, state: dict[str, Any], client: object, **kwargs: Any) -> None:
        self.state = state
        self.kwargs = kwargs
        state["sessions"].append(self)

    def run(
        self,
        on_result: Callable[[SegmentResult], None],
        idle_timeout: float | None = None,
        idle_notify_interval: float | None = None,
        on_idle: Callable[[float], None] | None = None,
    ) -> SessionOutcome:
        self.idle_timeout = idle_timeout
        self.idle_notify_interval = idle_notify_interval
        for idle_seconds in self.state["idle_reminders"]:
            if on_idle is not None:
                on_idle(idle_seconds)
        if self.state["audio_error"] is not None:
            return SessionOutcome([], None, audio_error=self.state["audio_error"])
        if self.kwargs.get("on_recording_started"):
            self.kwargs["on_recording_started"]()
        results = []
        for item in self.state["script"]:
            if callable(item):
                item(self)
                continue
            result = piece(item) if isinstance(item, str) else item
            results.append(result)
            on_result(result)
        if self.kwargs.get("on_recording_stopped"):
            self.kwargs["on_recording_stopped"](self.state["stop_reason"])
        return SessionOutcome(
            results,
            self.state["stop_reason"],
            aborted=self.kwargs["abort_event"].is_set(),
            idle_stopped=self.state["idle_stopped"],
        )


class _FakeClient:
    def __init__(self, config: dict) -> None:
        self.config = config

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeModifierWatch:
    """Stand-in for keyboard.ModifierWatch, scripted through a shared dict.

    ``held`` is consumed one value per ``held()`` call (``False`` once empty);
    ``release`` is what ``wait_released`` reports; ``on_wait`` (optional) runs
    when typing starts waiting.
    """

    def __init__(self, state: dict) -> None:
        self.state = state

    def __enter__(self) -> _FakeModifierWatch:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def held(self) -> bool | None:
        script = self.state["held"]
        return script.pop(0) if script else False

    def wait_released(self, timeout: float) -> bool:
        self.state["waits"] += 1
        if self.state.get("on_wait"):
            self.state["on_wait"]()
        return bool(self.state["release"])


@pytest.fixture
def listen_env(monkeypatch: pytest.MonkeyPatch, restore_signal_handlers) -> Iterator[dict]:
    """Patch run_dictation's side effects; tests fill in ``script`` and read the captures."""
    from talkat import main as main_mod

    state: dict[str, Any] = {
        "main_mod": main_mod,
        "script": [],
        "stop_reason": "stop_requested",
        "audio_error": None,
        "idle_reminders": [],
        "idle_stopped": False,
        "sessions": [],
        "subprocess_calls": [],
        "notes": [],
        "copied": [],
        "signal_events": [],
        "modifiers": {"held": [], "release": True, "waits": 0},
    }

    def fake_run(command: list[str], **kwargs: object) -> object:
        state["subprocess_calls"].append(list(command))
        if state.get("after_call"):
            state["after_call"](command)

        class _Done:
            returncode = 0
            stdout = b""
            stderr = b""

        return _Done()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)
    monkeypatch.setattr(
        main_mod, "DictationSession", lambda client, **kw: _FakeSession(state, client, **kw)
    )
    monkeypatch.setattr(main_mod, "TranscriptionClient", _FakeClient)
    monkeypatch.setattr(main_mod, "_notify", state["notes"].append)
    monkeypatch.setattr(
        main_mod, "copy_to_clipboard", lambda text: state["copied"].append(text) or True
    )
    state["real_set_stop_event_on_signal"] = main_mod._set_stop_event_on_signal
    monkeypatch.setattr(
        main_mod,
        "_set_stop_event_on_signal",
        lambda stop, abort: state["signal_events"].append((stop, abort)),
    )
    monkeypatch.setattr(main_mod, "_fetch_server_info", lambda _socket: (None, None))
    # Focus queries must never hit the real compositor from tests (the test
    # process may well be running inside one). None disables the guard.
    monkeypatch.setattr(main_mod, "get_focused_window", lambda: None)
    # Nor may the modifier guard read the real keyboard: a key held while the
    # suite runs would change what gets typed.
    monkeypatch.setattr(main_mod, "ModifierWatch", lambda: _FakeModifierWatch(state["modifiers"]))
    yield state


def _ydotool_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c and c[0] == "ydotool"]


def _typed_text(calls: list[list[str]]) -> str:
    """Reassemble what was typed from the per-keystroke ydotool calls."""
    ydotool_calls = _ydotool_calls(calls)
    for cmd in ydotool_calls:
        assert cmd[:4] == ["ydotool", "type", "--escape=0", "--"], cmd
        assert len(cmd) == 5 and len(cmd[4]) == 1 and cmd[4].isascii(), cmd
    return "".join(cmd[4] for cmd in ydotool_calls)


# ---------------------------------------------------------------------------
# Typing as you talk
# ---------------------------------------------------------------------------


def test_transcript_is_typed_one_keystroke_per_ydotool_call(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["hello world"]

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "hello world"
    assert listen_env["notes"][-1] == "Typed: hello world"
    assert listen_env["copied"] == []


def test_each_piece_is_typed_as_soon_as_it_arrives(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    calls = listen_env["subprocess_calls"]
    typed_between: list[str] = []
    listen_env["script"] = [
        "First sentence.",
        lambda _session: typed_between.append(_typed_text(calls)),
        "Second one.",
    ]

    assert run_dictation() == 0
    assert typed_between == ["First sentence."]
    assert _typed_text(calls) == "First sentence. Second one."


def test_silent_pieces_add_no_text(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["", "hello", "", "world", ""]

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "hello world"


def test_no_speech_types_nothing(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["", ""]

    assert run_dictation() == 0
    assert _ydotool_calls(listen_env["subprocess_calls"]) == []
    assert "No text recognized" in listen_env["notes"]


def test_transcript_is_sanitized_before_typing(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["hi\x00there"]

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "hithere"


def test_non_ascii_characters_never_reach_ydotool(clean_pid_files, listen_env):
    """ydotool 1.0.x reads outside its keymap for non-ASCII bytes; they're skipped."""
    from talkat.main import run_dictation

    listen_env["script"] = ["café — ok"]

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "caf  ok"


def test_shell_metacharacters_are_typed_verbatim(clean_pid_files, listen_env, monkeypatch):
    """Regression: validate_command once rejected $ ( ) ; in *arguments*, so
    dictating "$20 (roughly)" crashed typing. The REAL safe_subprocess_run
    validation runs here; only the spawn is stubbed."""
    import subprocess as subprocess_module

    from talkat.main import run_dictation
    from talkat.security import safe_subprocess_run as real_safe_run

    spawned: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(listen_env["main_mod"], "safe_subprocess_run", real_safe_run)
    monkeypatch.setattr(
        subprocess_module, "run", lambda command, **kwargs: spawned.append(list(command)) or _Done()
    )
    listen_env["script"] = ["$20 (roughly); done & dusted"]

    assert run_dictation() == 0
    assert _typed_text(spawned) == "$20 (roughly); done & dusted"


# ---------------------------------------------------------------------------
# Whatever can't be typed goes to the clipboard — and the notification says so
# ---------------------------------------------------------------------------


def test_focus_changed_before_typing_sends_everything_to_clipboard(
    clean_pid_files, listen_env, monkeypatch
):
    from talkat.main import run_dictation

    focus_values = iter(["niri:1", "niri:2"])  # recording start, first keystroke
    monkeypatch.setattr(listen_env["main_mod"], "get_focused_window", lambda: next(focus_values))
    listen_env["script"] = ["wrong window"]

    assert run_dictation() == 0
    assert _ydotool_calls(listen_env["subprocess_calls"]) == []
    assert listen_env["copied"] == ["wrong window"]
    assert listen_env["notes"][-1] == "Focus changed — the transcript copied to clipboard."


def test_focus_moving_mid_typing_sends_the_rest_to_clipboard(
    clean_pid_files, listen_env, monkeypatch
):
    """Focus is re-checked at word boundaries; once it moves, typing stops."""
    from talkat.main import run_dictation

    # recording start, then before "hello", "big", "world", "again"
    focus_values = iter(["niri:1", "niri:1", "niri:1", "niri:9"])
    monkeypatch.setattr(listen_env["main_mod"], "get_focused_window", lambda: next(focus_values))
    listen_env["script"] = ["hello big world", "again"]

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "hello big "
    assert listen_env["copied"] == ["world again"]
    assert listen_env["notes"][-1] == "Focus changed — the rest copied to clipboard."


def test_rest_keeps_its_leading_space_to_paste_after_typed_text(
    clean_pid_files, listen_env, monkeypatch
):
    from talkat.main import run_dictation

    focus = {"now": "niri:1"}
    monkeypatch.setattr(listen_env["main_mod"], "get_focused_window", lambda: focus["now"])
    listen_env["script"] = ["hello", lambda _s: focus.update(now="niri:2"), "world"]

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "hello"
    assert listen_env["copied"] == [" world"]


def test_untranscribed_piece_stops_typing_and_goes_to_clipboard_with_its_marker(
    clean_pid_files, listen_env
):
    """Typing on past a gap would leave text with a hole in it."""
    from talkat.main import run_dictation

    listen_env["script"] = ["Before the gap.", piece(MARKER, failed=True), "After."]

    assert run_dictation() == 1
    assert _typed_text(listen_env["subprocess_calls"]) == "Before the gap."
    assert listen_env["copied"] == [f" {MARKER} After."]
    assert listen_env["notes"][-1] == (
        "Part of the recording couldn't be transcribed (audio saved) — "
        "the rest copied to clipboard."
    )
    # The user hears about it while still talking, not only at the end.
    assert any("will go to the clipboard" in n for n in listen_env["notes"][:-1])


def test_untranscribed_first_piece_means_nothing_is_typed(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = [piece(MARKER, failed=True), "After."]

    assert run_dictation() == 1
    assert _ydotool_calls(listen_env["subprocess_calls"]) == []
    assert listen_env["copied"] == [f"{MARKER} After."]
    assert listen_env["notes"][-1].endswith("the transcript copied to clipboard.")


def test_typing_failure_sends_transcript_to_clipboard(clean_pid_files, listen_env, monkeypatch):
    from talkat.main import run_dictation

    def failing_run(command: list[str], **kwargs: object) -> object:
        if command and command[0] == "ydotool":
            raise FileNotFoundError("ydotool not installed")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(listen_env["main_mod"], "safe_subprocess_run", failing_run)
    listen_env["script"] = ["precious words"]

    assert run_dictation() == 0
    assert listen_env["copied"] == ["precious words"]


def test_output_mode_clipboard_never_types(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["clipboard", "me"]

    assert run_dictation(config_overrides={"output_mode": "clipboard"}) == 0
    assert _ydotool_calls(listen_env["subprocess_calls"]) == []
    assert listen_env["copied"] == ["clipboard me"]


def test_output_file_is_written_and_nothing_typed(clean_pid_files, listen_env, tmp_path: Path):
    from talkat.main import run_dictation

    listen_env["script"] = ["output", "me"]
    out = tmp_path / "result.txt"

    assert run_dictation(output_file=str(out)) == 0
    assert out.read_text(encoding="utf-8") == "output me"
    assert _ydotool_calls(listen_env["subprocess_calls"]) == []


# ---------------------------------------------------------------------------
# Typing guards — held modifiers, stop signals
# ---------------------------------------------------------------------------


def test_typing_pauses_while_a_modifier_is_held(clean_pid_files, listen_env):
    """No keystroke may go out while a modifier is down; typing resumes after."""
    from talkat.main import run_dictation

    calls = listen_env["subprocess_calls"]
    listen_env["script"] = ["the tool"]
    listen_env["modifiers"]["held"] = [False, False, False, True]  # Super down before " "
    typed_when_paused: list[str] = []
    listen_env["modifiers"]["on_wait"] = lambda: typed_when_paused.append(_typed_text(calls))

    assert run_dictation() == 0
    assert typed_when_paused == ["the"]
    assert _typed_text(calls) == "the tool"


def test_modifier_held_past_timeout_sends_the_rest_to_clipboard(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["hello world"]
    listen_env["modifiers"]["held"] = [False] * 6 + [True]
    listen_env["modifiers"]["release"] = False

    assert run_dictation() == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "hello "
    assert listen_env["copied"] == ["world"]
    assert listen_env["notes"][-1] == "A modifier key stayed held — the rest copied to clipboard."


def test_abort_mid_typing_stops_between_keystrokes(clean_pid_files, listen_env):
    """stop_process's escalation (the second signal) ends typing cleanly — never
    mid-keystroke — with the rest in the clipboard."""
    from talkat.main import run_dictation

    calls = listen_env["subprocess_calls"]
    events = listen_env["signal_events"]

    def abort_after_two_keystrokes(_command: list[str]) -> None:
        if len(_ydotool_calls(calls)) == 2:
            events[0][1].set()  # what the handler does on signal #2

    listen_env["after_call"] = abort_after_two_keystrokes
    listen_env["script"] = ["abcdef", "ghi"]

    assert run_dictation() == 0
    assert _typed_text(calls) == "ab"
    assert listen_env["copied"] == ["cdef ghi"]
    assert listen_env["notes"][-1] == "Stopped — the rest copied to clipboard."


def test_real_signals_while_typing_never_raise(clean_pid_files, listen_env, monkeypatch):
    """Real SIGTERMs mid-typing with the real handlers: the first only marks the
    stop, the second stops typing between keystrokes — no KeyboardInterrupt."""
    import os

    from talkat.main import run_dictation

    main_mod = listen_env["main_mod"]
    monkeypatch.setattr(
        main_mod, "_set_stop_event_on_signal", listen_env["real_set_stop_event_on_signal"]
    )
    calls = listen_env["subprocess_calls"]

    def signal_after_keystrokes_two_and_three(_command: list[str]) -> None:
        if len(_ydotool_calls(calls)) in (2, 3):
            # Without a Python handler SIGTERM would kill the test runner.
            assert callable(signal.getsignal(signal.SIGTERM)), "stop handler missing"
            os.kill(os.getpid(), signal.SIGTERM)

    listen_env["after_call"] = signal_after_keystrokes_two_and_three
    listen_env["script"] = ["abcdef"]

    assert run_dictation() == 0
    assert _typed_text(calls) == "abc"
    assert listen_env["copied"] == ["def"]


# ---------------------------------------------------------------------------
# Recording limits and notices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("max_duration", "Recording hit the 10 min limit — transcribing."),
        ("read_error", "Microphone error — transcribing what was recorded."),
        ("stop_requested", None),
    ],
)
def test_recording_that_ends_on_its_own_says_so(clean_pid_files, listen_env, stop_reason, expected):
    """The 30 s cap used to end recordings silently; the user kept talking and
    reached for the stop hotkey while the transcript was being typed."""
    from talkat.main import run_dictation

    listen_env["script"] = ["long thought"]
    listen_env["stop_reason"] = stop_reason

    assert run_dictation() == 0
    notes = listen_env["notes"]
    if expected is None:
        assert not any("transcribing" in n for n in notes)
    else:
        assert expected in notes


def test_limit_notice_reads_well_for_an_odd_cap(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["hi"]
    listen_env["stop_reason"] = "max_duration"

    assert run_dictation(config_overrides={"max_recording_duration": 75.0}) == 0
    assert "Recording hit the 75s limit — transcribing." in listen_env["notes"]


def test_session_gets_the_recording_limits_including_overrides(clean_pid_files, listen_env):
    """--max-recording used to be dropped: capture re-read the config files."""
    from talkat.config import CODE_DEFAULTS
    from talkat.main import run_dictation

    listen_env["script"] = ["hi"]
    run_dictation()
    run_dictation(config_overrides={"max_recording_duration": 45.0, "idle_timeout": 120.0})

    default_session, overridden = listen_env["sessions"]
    assert default_session.kwargs["max_duration"] == CODE_DEFAULTS["max_recording_duration"]
    assert default_session.idle_timeout == CODE_DEFAULTS["idle_timeout"]
    assert default_session.idle_notify_interval == CODE_DEFAULTS["idle_notify_interval"]
    assert overridden.kwargs["max_duration"] == 45.0
    assert overridden.idle_timeout == 120.0


def test_a_quiet_session_says_it_is_still_recording(clean_pid_files, listen_env):
    """Silence is not a stop — but a session left open must not look dead."""
    from talkat.main import run_dictation

    listen_env["script"] = ["hello"]
    listen_env["idle_reminders"] = [30.0, 90.0]

    assert run_dictation() == 0
    assert listen_env["notes"][:2] == [
        'No speech for 30s — still dictating. Run "talkat listen" again to stop.',
        'No speech for 90s — still dictating. Run "talkat listen" again to stop.',
    ]


def test_idle_stop_is_announced(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["hello"]
    listen_env["idle_stopped"] = True

    assert run_dictation() == 0
    assert "Stopped: no speech for 1 min." in listen_env["notes"]


def test_microphone_error_is_reported(clean_pid_files, listen_env):
    from talkat.main import run_dictation
    from talkat.record import AudioSessionError

    listen_env["audio_error"] = AudioSessionError("No microphone found")

    assert run_dictation() == 1
    assert "Audio error: No microphone found" in listen_env["notes"]


def test_transcript_is_saved_with_markers(clean_pid_files, listen_env, monkeypatch):
    from talkat.main import run_dictation

    saved: list[str] = []
    monkeypatch.setattr(
        listen_env["main_mod"], "save_transcript", lambda text: saved.append(text) or Path()
    )
    listen_env["script"] = ["one", piece(MARKER, failed=True), "two"]

    run_dictation()
    assert saved == [f"one {MARKER} two"]


# ---------------------------------------------------------------------------
# §5a postprocess — delivered once, at the end
# ---------------------------------------------------------------------------


def test_postprocess_types_the_processed_output_at_the_end(
    clean_pid_files, listen_env, monkeypatch
):
    from talkat.main import run_dictation

    calls = listen_env["subprocess_calls"]
    typed_between: list[str] = []
    listen_env["script"] = ["hello", lambda _s: typed_between.append(_typed_text(calls)), "world"]
    captured: list[tuple[str, str]] = []

    def fake_postprocess(text: str, profile_name: str, *, config: dict | None = None) -> str:
        captured.append((text, profile_name))
        return "Hello, world."

    monkeypatch.setattr("talkat.postprocess.postprocess_text", fake_postprocess)

    assert run_dictation(postprocess="tidy") == 0
    assert typed_between == [""], "AIPP needs the whole transcript: nothing typed mid-session"
    assert captured == [("hello world", "tidy")]
    assert _typed_text(calls) == "Hello, world."


def test_postprocess_failopen_still_types_raw(clean_pid_files, listen_env, monkeypatch):
    from talkat.main import run_dictation

    listen_env["script"] = ["raw text"]
    monkeypatch.setattr("talkat.postprocess.postprocess_text", lambda text, _name, **_kw: text)

    assert run_dictation(postprocess="broken") == 0
    assert _typed_text(listen_env["subprocess_calls"]) == "raw text"


def test_no_postprocess_arg_skips_aipp(clean_pid_files, listen_env, monkeypatch):
    from talkat.main import run_dictation

    called: list[bool] = []
    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text", lambda *_a, **_kw: called.append(True) or ""
    )
    listen_env["script"] = ["hi"]

    assert run_dictation() == 0
    assert called == []


def test_postprocess_skipped_on_empty_transcription(clean_pid_files, listen_env, monkeypatch):
    from talkat.main import run_dictation

    called: list[bool] = []
    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text", lambda *_a, **_kw: called.append(True) or ""
    )
    listen_env["script"] = [""]

    assert run_dictation(postprocess="tidy") == 0
    assert called == []


def test_postprocess_skipped_when_part_was_not_transcribed(
    clean_pid_files, listen_env, monkeypatch
):
    """The LLM would rewrite the saved-audio marker; the raw text goes to the clipboard."""
    from talkat.main import run_dictation

    called: list[bool] = []
    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text", lambda *_a, **_kw: called.append(True) or ""
    )
    listen_env["script"] = ["hello", piece(MARKER, failed=True)]

    assert run_dictation(postprocess="tidy") == 1
    assert called == []
    assert _ydotool_calls(listen_env["subprocess_calls"]) == []
    assert listen_env["copied"] == [f"hello {MARKER}"]
    assert listen_env["notes"][-1].endswith("copied to clipboard.")


def test_stop_and_abort_events_reach_the_session(clean_pid_files, listen_env):
    from talkat.main import run_dictation

    listen_env["script"] = ["hi"]
    run_dictation()

    (stop, abort), session = listen_env["signal_events"][0], listen_env["sessions"][0]
    assert session.kwargs["stop_event"] is stop
    assert session.kwargs["abort_event"] is abort
    assert isinstance(stop, threading.Event)
