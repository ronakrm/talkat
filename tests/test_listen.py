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
    ) -> str:
        self.calls += 1
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
