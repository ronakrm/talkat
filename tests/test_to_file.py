"""Tests for `talkat listen --to-file` — long-form note taking on the one route.

DictationSession is replaced by a fake that replays scripted segment results
through ``on_result``; the session's own capture, idle timeout, and length
limit are covered in test_session.py. What we pin down here: pieces go
straight to the transcript file as they land, nothing is typed, untranscribed
pieces leave a marker and trip the consecutive-failure breaker, and
end-of-session AIPP runs once on the whole transcript.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from talkat.session import SegmentResult, SessionOutcome

MARKER = "[untranscribed audio: talkat file /tmp/untranscribed/x_000.wav]"


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
    return SegmentResult(index=0, text=text, seconds=5.0, cut="pause", failed=failed)


class _FakeSession:
    """Replays ``state["script"]`` (SegmentResults, strings, or callables) through on_result."""

    def __init__(self, state: dict[str, Any], client: object, **kwargs: Any) -> None:
        self.state = state
        self.kwargs = kwargs
        self.idle_timeout: float | None = None
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
        results = []
        for item in self.state["script"]:
            if callable(item):
                item(self)
                continue
            result = piece(item) if isinstance(item, str) else item
            results.append(result)
            on_result(result)
        return SessionOutcome(results, "stop_requested", audio_error=self.state["audio_error"])


class _FakeClient:
    def __init__(self, config: dict) -> None:
        self.config = config

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def to_file_env(monkeypatch: pytest.MonkeyPatch, restore_signal_handlers) -> Iterator[dict]:
    from talkat import main as main_mod

    state: dict[str, Any] = {
        "script": [],
        "audio_error": None,
        "sessions": [],
        "notes": [],
        "copied": [],
        "subprocess_calls": [],
        "signal_events": [],
    }
    monkeypatch.setattr(
        main_mod, "DictationSession", lambda client, **kw: _FakeSession(state, client, **kw)
    )
    monkeypatch.setattr(main_mod, "TranscriptionClient", _FakeClient)
    monkeypatch.setattr(main_mod, "_notify", state["notes"].append)
    monkeypatch.setattr(
        main_mod, "copy_to_clipboard", lambda text: state["copied"].append(text) or True
    )
    monkeypatch.setattr(
        main_mod,
        "_set_stop_event_on_signal",
        lambda stop, abort: state["signal_events"].append((stop, abort)),
    )
    monkeypatch.setattr(main_mod, "_fetch_server_info", lambda _socket: (None, None))

    def fake_run(command: list[str], **_kwargs: object) -> object:
        state["subprocess_calls"].append(list(command))

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(main_mod, "safe_subprocess_run", fake_run)
    yield state


def _run(tmp_path: Path, **kwargs: Any) -> tuple[int, Path]:
    from talkat.main import run_dictation

    output = tmp_path / "transcript.txt"
    rc = run_dictation(output_file=str(output), to_file=True, **kwargs)
    return rc, output


# ---------------------------------------------------------------------------
# Transcript file
# ---------------------------------------------------------------------------


def test_each_piece_is_appended_to_the_transcript_as_it_arrives(
    clean_pid_files, to_file_env, tmp_path
):
    output = tmp_path / "transcript.txt"
    seen_on_disk: list[str] = []
    to_file_env["script"] = [
        "hello",
        lambda _s: seen_on_disk.append(output.read_text(encoding="utf-8")),
        "this is talkat",
        "goodbye",
    ]

    rc, _ = _run(tmp_path)

    assert rc == 0
    assert seen_on_disk == ["hello "], "pieces must hit the disk immediately, not at session end"
    assert output.read_text(encoding="utf-8") == "hello this is talkat goodbye "


def test_silent_pieces_are_not_written(clean_pid_files, to_file_env, tmp_path):
    to_file_env["script"] = ["", "hello", "", "world", ""]

    rc, output = _run(tmp_path)

    assert rc == 0
    assert output.read_text(encoding="utf-8").split() == ["hello", "world"]


def test_untranscribed_piece_leaves_a_marker_and_fails_the_run(
    clean_pid_files, to_file_env, tmp_path
):
    to_file_env["script"] = ["before", piece(MARKER, failed=True), "after"]

    rc, output = _run(tmp_path)

    assert rc == 1
    assert output.read_text(encoding="utf-8") == f"before {MARKER} after "
    assert any("couldn't be transcribed" in n for n in to_file_env["notes"])


def test_breaker_stops_the_session_after_consecutive_failures(
    clean_pid_files, to_file_env, tmp_path
):
    stop_seen: list[bool] = []
    to_file_env["script"] = [
        piece(MARKER, failed=True),
        piece(MARKER, failed=True),
        lambda _s: stop_seen.append(to_file_env["signal_events"][0][0].is_set()),
        piece(MARKER, failed=True),
        lambda _s: stop_seen.append(to_file_env["signal_events"][0][0].is_set()),
    ]

    rc, _ = _run(tmp_path, config_overrides={"max_consecutive_errors": 3})

    assert rc == 1
    assert stop_seen == [False, True]
    assert any("Dictation stopped" in n for n in to_file_env["notes"])


def test_a_success_resets_the_breaker(clean_pid_files, to_file_env, tmp_path):
    failed = piece(MARKER, failed=True)
    to_file_env["script"] = [failed, failed, "ok", failed, failed]

    _run(tmp_path, config_overrides={"max_consecutive_errors": 3})

    stop_event = to_file_env["signal_events"][0][0]
    assert not stop_event.is_set()


def test_nothing_is_typed(clean_pid_files, to_file_env, tmp_path):
    to_file_env["script"] = ["hello", "world"]

    _run(tmp_path)

    assert [c for c in to_file_env["subprocess_calls"] if c and c[0] == "ydotool"] == []


def test_session_limits_come_from_config(clean_pid_files, to_file_env, tmp_path):
    to_file_env["script"] = ["hi"]

    _run(
        tmp_path,
        config_overrides={"idle_timeout": 45.0, "max_recording_duration": 900.0},
    )

    (session,) = to_file_env["sessions"]
    assert session.kwargs["max_duration"] == 900.0
    assert session.idle_timeout == 45.0
    stop, abort = to_file_env["signal_events"][0]
    assert session.kwargs["stop_event"] is stop and session.kwargs["abort_event"] is abort


def test_microphone_error_fails_the_run(clean_pid_files, to_file_env, tmp_path):
    from talkat.record import AudioSessionError

    to_file_env["audio_error"] = AudioSessionError("No microphone found")

    rc, _ = _run(tmp_path)

    assert rc == 1
    assert "Audio error: No microphone found" in to_file_env["notes"]


def test_no_speech_says_so(clean_pid_files, to_file_env, tmp_path):
    rc, _ = _run(tmp_path)

    assert rc == 0
    assert "Stopped. No speech detected." in to_file_env["notes"]


def test_full_transcript_is_copied_to_clipboard_at_the_end(clean_pid_files, to_file_env, tmp_path):
    to_file_env["script"] = ["hello", "world"]

    rc, _ = _run(tmp_path)

    assert rc == 0
    assert to_file_env["copied"] == ["hello world"]
    assert to_file_env["notes"][-1] == "Stopped. 2 words copied to clipboard."


# ---------------------------------------------------------------------------
# §5a postprocess — end-of-session AIPP
# ---------------------------------------------------------------------------


def test_postprocess_runs_once_on_the_full_transcript(
    clean_pid_files, to_file_env, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """Per-piece AIPP would lose cross-piece context and multiply LLM cost."""
    to_file_env["script"] = ["hello", "this is talkat", "goodbye"]
    aipp_calls: list[tuple[str, str]] = []

    def fake_postprocess(text: str, profile_name: str, *, config: dict | None = None) -> str:
        aipp_calls.append((text, profile_name))
        return "POLISHED: " + text.strip()

    monkeypatch.setattr("talkat.postprocess.postprocess_text", fake_postprocess)

    rc, output = _run(tmp_path, postprocess="tidy")

    assert rc == 0
    assert aipp_calls == [("hello this is talkat goodbye", "tidy")]
    processed = output.with_suffix(".processed.txt")
    assert processed.read_text(encoding="utf-8") == "POLISHED: hello this is talkat goodbye"
    assert "hello" in output.read_text(encoding="utf-8"), "raw transcript must remain intact"


def test_postprocess_not_invoked_when_arg_omitted(
    clean_pid_files, to_file_env, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    called: list[bool] = []
    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text", lambda *_a, **_kw: called.append(True) or ""
    )
    to_file_env["script"] = ["hello"]

    _run(tmp_path)
    assert called == []


def test_postprocess_failopen_keeps_raw_clipboard(
    clean_pid_files, to_file_env, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """No .processed.txt when AIPP returned its input unchanged."""
    monkeypatch.setattr("talkat.postprocess.postprocess_text", lambda text, _name, **_kw: text)
    to_file_env["script"] = ["hello world"]

    rc, output = _run(tmp_path, postprocess="tidy")

    assert rc == 0
    assert not output.with_suffix(".processed.txt").exists()
    assert to_file_env["copied"] == ["hello world"]


def test_postprocess_skipped_when_part_was_not_transcribed(
    clean_pid_files, to_file_env, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """The LLM would rewrite the saved-audio marker."""
    called: list[bool] = []
    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text", lambda *_a, **_kw: called.append(True) or ""
    )
    to_file_env["script"] = ["hello", piece(MARKER, failed=True)]

    _run(tmp_path, postprocess="tidy")

    assert called == []
    assert to_file_env["copied"] == [f"hello {MARKER}"]
