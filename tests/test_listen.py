"""Tests for talkat.main.listen_once — the listen-mode typing path.

These complement the long-mode tests by covering the single-utterance flow:
how a successful transcription becomes a ydotool typing call, how empty/no-text
results short-circuit, and how the --output-file path bypasses typing entirely.

TranscriptionClient is stubbed (we never touch a real mic or HTTP socket),
and safe_subprocess_run is patched to capture invocations instead of actually
shelling out to ydotool/notify-send.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

import pytest

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


class _StubTranscriber:
    """Drop-in for TranscriptionClient.

    ``text`` is returned (or an Exception is raised if ``raises`` is set) by
    every transcribe_one_utterance call.
    """

    def __init__(self, config: dict, text: str = "", raises: BaseException | None = None) -> None:
        self.config = config
        self.text = text
        self.raises = raises
        self.calls = 0

    def __enter__(self) -> _StubTranscriber:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    def transcribe_one_utterance(
        self,
        stop_event: threading.Event | None = None,
        max_duration: float | None = None,
        debug: bool = False,
        on_recording_started: object | None = None,
    ) -> str:
        self.calls += 1
        if callable(on_recording_started):
            on_recording_started()
        if self.raises is not None:
            raise self.raises
        return self.text


@pytest.fixture
def listen_env(monkeypatch: pytest.MonkeyPatch, restore_signal_handlers) -> Iterator[dict]:
    """Patch the side-effect-y parts of listen_once and capture subprocess calls."""
    from talkat import main as main_mod

    subprocess_calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        subprocess_calls.append(list(command))

        class _Done:
            returncode = 0
            stdout = b""
            stderr = b""

        return _Done()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)
    monkeypatch.setattr(main_mod, "_notify", lambda _msg: None)
    monkeypatch.setattr(main_mod, "_set_stop_event_on_signal", lambda _ev: None)
    # Focus queries must never hit the real compositor from tests (the test
    # process may well be running inside one). None disables the guard.
    monkeypatch.setattr(main_mod, "get_focused_window", lambda: None)

    state: dict = {"subprocess_calls": subprocess_calls, "main_mod": main_mod}
    yield state


def _install_stub_transcriber(
    main_mod, *, text: str = "", raises: BaseException | None = None
) -> None:
    """Replace TranscriptionClient with a stub that returns ``text``/raises."""

    def factory(config: dict) -> _StubTranscriber:
        return _StubTranscriber(config, text=text, raises=raises)

    main_mod.TranscriptionClient = factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_listen_once_types_recognized_text_via_ydotool(clean_pid_files, listen_env):
    """A successful transcription must reach ydotool with the recognized text."""
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="hello world")

    rc = listen_once()

    assert rc == 0
    calls = listen_env["subprocess_calls"]
    ydotool_calls = [c for c in calls if c and c[0] == "ydotool"]
    assert len(ydotool_calls) == 1, f"expected one ydotool call, got: {calls}"

    cmd = ydotool_calls[0]
    assert cmd[0:3] == ["ydotool", "type", "--key-delay=1"]
    # Final arg is the text to type.
    assert cmd[3] == "hello world"


def test_listen_once_empty_transcription_skips_typing(clean_pid_files, listen_env):
    """If the model returns empty text, we must NOT call ydotool."""
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="")

    rc = listen_once()

    assert rc == 0
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert ydotool_calls == [], "ydotool was called for empty text"


def test_listen_once_with_output_file_writes_file_and_skips_typing(
    clean_pid_files, listen_env, tmp_path: Path
):
    """When --output is provided, the text goes to disk and ydotool is skipped."""
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="output me")

    out = tmp_path / "result.txt"
    rc = listen_once(output_file=str(out))

    assert rc == 0
    assert out.exists()
    assert out.read_text(encoding="utf-8") == "output me"
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert ydotool_calls == [], "ydotool was called despite --output being set"


def test_listen_once_unreachable_server_returns_one(clean_pid_files, listen_env):
    """A TranscriptionUnreachable must surface as exit code 1 and skip ydotool."""
    from talkat.main import TranscriptionUnreachable, listen_once

    _install_stub_transcriber(
        listen_env["main_mod"], raises=TranscriptionUnreachable("server down")
    )

    rc = listen_once()
    assert rc == 1
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert ydotool_calls == []


def test_listen_once_server_error_returns_one(clean_pid_files, listen_env):
    """A TranscriptionServerError must surface as exit code 1 and skip ydotool."""
    from talkat.main import TranscriptionServerError, listen_once

    _install_stub_transcriber(listen_env["main_mod"], raises=TranscriptionServerError("500"))

    rc = listen_once()
    assert rc == 1


def test_listen_once_sanitizes_text_before_typing(clean_pid_files, listen_env):
    """Control characters / nulls in transcribed text must not reach ydotool literally.

    sanitize_text_for_typing strips NUL and other control chars; the typed
    argument should be the sanitized form.
    """
    from talkat.main import listen_once

    # NUL byte must be stripped from the typed payload.
    _install_stub_transcriber(listen_env["main_mod"], text="hi\x00there")

    rc = listen_once()
    assert rc == 0

    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert len(ydotool_calls) == 1
    typed_arg = ydotool_calls[0][3]
    assert "\x00" not in typed_arg
    # "hi" and "there" should still be present.
    assert "hi" in typed_arg
    assert "there" in typed_arg


def test_listen_once_types_text_with_shell_metacharacters(clean_pid_files, listen_env, monkeypatch):
    """Transcripts containing $ ( ) ; etc. must be typed verbatim.

    Regression test: validate_command used to reject shell metacharacters in
    *arguments*, so dictating "$20 (roughly)" crashed the typing path and
    lost the text. With shell=False these characters are inert data. The REAL
    safe_subprocess_run validation runs here; only the spawn is stubbed.
    """
    import subprocess as subprocess_module

    from talkat.main import listen_once
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

    _install_stub_transcriber(listen_env["main_mod"], text="$20 (roughly); done & dusted")
    rc = listen_once()

    assert rc == 0
    ydotool_calls = [c for c in spawned if c and c[0] == "ydotool"]
    assert len(ydotool_calls) == 1
    assert ydotool_calls[0][3] == "$20 (roughly); done & dusted"


def test_listen_once_focus_change_diverts_to_clipboard(clean_pid_files, listen_env, monkeypatch):
    """If the focused window changed during dictation, don't type — clipboard it."""
    from talkat.main import listen_once

    main_mod = listen_env["main_mod"]
    _install_stub_transcriber(main_mod, text="wrong window")

    focus_values = iter(["niri:1", "niri:2"])  # capture-time, then check-time
    monkeypatch.setattr(main_mod, "get_focused_window", lambda: next(focus_values))

    copied: list[str] = []
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: copied.append(text) or True)

    rc = listen_once()

    assert rc == 0
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert ydotool_calls == [], "typed despite focus change"
    assert copied == ["wrong window"]


def test_listen_once_stable_focus_still_types(clean_pid_files, listen_env, monkeypatch):
    """Unchanged focus must not divert anything — the normal typing path runs."""
    from talkat.main import listen_once

    main_mod = listen_env["main_mod"]
    _install_stub_transcriber(main_mod, text="right window")
    monkeypatch.setattr(main_mod, "get_focused_window", lambda: "niri:7")

    rc = listen_once()

    assert rc == 0
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert len(ydotool_calls) == 1
    assert ydotool_calls[0][3] == "right window"


def test_listen_once_typing_failure_falls_back_to_clipboard(
    clean_pid_files, listen_env, monkeypatch
):
    """A ydotool failure must never lose the transcript — clipboard fallback."""
    from talkat.main import listen_once

    main_mod = listen_env["main_mod"]
    _install_stub_transcriber(main_mod, text="precious words")

    def failing_run(command: list[str], **kwargs: object) -> object:
        if command and command[0] == "ydotool":
            raise FileNotFoundError("ydotool not installed")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", failing_run)

    copied: list[str] = []
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: copied.append(text) or True)

    rc = listen_once()

    assert rc == 0
    assert copied == ["precious words"]


def test_listen_once_output_mode_clipboard_never_types(clean_pid_files, listen_env, monkeypatch):
    """output_mode=clipboard sends the transcript to the clipboard, not ydotool."""
    from talkat.main import listen_once

    main_mod = listen_env["main_mod"]
    _install_stub_transcriber(main_mod, text="clipboard me")

    copied: list[str] = []
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: copied.append(text) or True)

    rc = listen_once(config_overrides={"output_mode": "clipboard"})

    assert rc == 0
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert ydotool_calls == []
    assert copied == ["clipboard me"]


# ---------------------------------------------------------------------------
# §5a postprocess wiring — listen_once with --postprocess
# ---------------------------------------------------------------------------


def test_listen_once_postprocess_typed_text_is_the_processed_output(
    clean_pid_files, listen_env, monkeypatch: pytest.MonkeyPatch
):
    """`--postprocess tidy` must type the LLM output, not the raw transcript."""
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="hello world")

    captured: list[tuple[str, str]] = []

    def fake_postprocess(text: str, profile_name: str, *, config: dict | None = None) -> str:
        captured.append((text, profile_name))
        return "Hello, world."

    monkeypatch.setattr("talkat.postprocess.postprocess_text", fake_postprocess)

    rc = listen_once(postprocess="tidy")

    assert rc == 0
    assert captured == [("hello world", "tidy")]
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert len(ydotool_calls) == 1
    assert ydotool_calls[0][3] == "Hello, world."


def test_listen_once_postprocess_failopen_still_types_raw(
    clean_pid_files, listen_env, monkeypatch: pytest.MonkeyPatch
):
    """When AIPP returns the raw text (its fail-open contract), ydotool still fires.

    We don't exercise the failure path here — we just confirm that returning
    the input verbatim from postprocess_text yields the same ydotool call as
    not using --postprocess at all.
    """
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="raw text")

    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text",
        lambda text, _name, **_kw: text,  # fail-open shape
    )

    rc = listen_once(postprocess="broken")
    assert rc == 0
    ydotool_calls = [c for c in listen_env["subprocess_calls"] if c and c[0] == "ydotool"]
    assert len(ydotool_calls) == 1
    assert ydotool_calls[0][3] == "raw text"


def test_listen_once_no_postprocess_arg_skips_aipp(
    clean_pid_files, listen_env, monkeypatch: pytest.MonkeyPatch
):
    """When --postprocess isn't passed, postprocess_text must not be called."""
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="hi")

    called: list[bool] = []

    def boom(*_a, **_kw) -> str:
        called.append(True)
        return ""

    monkeypatch.setattr("talkat.postprocess.postprocess_text", boom)

    rc = listen_once()
    assert rc == 0
    assert called == [], "postprocess_text must not be invoked without --postprocess"


def test_listen_once_postprocess_skipped_on_empty_transcription(
    clean_pid_files, listen_env, monkeypatch: pytest.MonkeyPatch
):
    """No speech → no AIPP call (we already short-circuit on empty)."""
    from talkat.main import listen_once

    _install_stub_transcriber(listen_env["main_mod"], text="")

    called: list[bool] = []

    def fake(*_a, **_kw) -> str:
        called.append(True)
        return ""

    monkeypatch.setattr("talkat.postprocess.postprocess_text", fake)

    rc = listen_once(postprocess="tidy")
    assert rc == 0
    assert called == [], "AIPP must not run on empty transcription"
