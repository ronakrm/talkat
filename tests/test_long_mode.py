"""Tests for talkat.main.listen_continuous (long-mode dictation).

We stub out TranscriptionClient with a programmable fake so we can drive
the loop through specific code paths — server-unreachable exit, the §3
consecutive-error circuit breaker, and the on-disk transcript invariant
(no in-memory accumulation of segments).
"""

import signal
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    """Save/restore SIGINT/SIGTERM/SIGHUP — listen_continuous installs its own.

    Pytest installs its own SIGINT handler; replacing it during a test is fine
    so long as we put pytest's back before the next test runs.
    """
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


class FakeTranscriber:
    """Programmable stub used in place of TranscriptionClient.

    Each ``transcribe_one_utterance`` call pops the next entry from ``events``:

      - ``str`` → returned as the transcribed text (empty string allowed)
      - ``Exception`` instance → raised

    When ``events`` is exhausted the fake sets ``stop_event`` so the loop in
    ``listen_continuous`` exits cleanly without depending on real-time silence
    timers.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.events: list = []
        self.calls: list[dict] = []

    def __enter__(self) -> "FakeTranscriber":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def transcribe_one_utterance(
        self,
        stop_event: threading.Event | None = None,
        max_duration: float | None = None,
        debug: bool = False,
    ) -> str:
        self.calls.append({"stop_event": stop_event, "max_duration": max_duration, "debug": debug})
        if not self.events:
            if stop_event is not None:
                stop_event.set()
            return ""
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


@pytest.fixture
def patched_long_mode(
    monkeypatch: pytest.MonkeyPatch, restore_signal_handlers
) -> Iterator[FakeTranscriber]:
    """Wire up a FakeTranscriber in place of TranscriptionClient.

    Also silences notify-send and clipboard side effects so the test process
    doesn't fire desktop notifications or hit wl-copy.
    """
    from talkat import main as main_mod

    fake_container: dict[str, FakeTranscriber] = {}

    def factory(config: dict) -> FakeTranscriber:
        fake_container["fake"] = FakeTranscriber(config)
        return fake_container["fake"]

    monkeypatch.setattr(main_mod, "TranscriptionClient", factory)
    monkeypatch.setattr(main_mod, "_notify", lambda _msg: None)
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda _text: False)
    # Avoid stomping on pytest's signal handlers (restore_signal_handlers
    # also runs as a safety net).
    monkeypatch.setattr(main_mod, "_set_stop_event_on_signal", lambda _ev: None)

    yield fake_container  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unreachable_server_exits_with_one(clean_pid_files, patched_long_mode, tmp_path: Path):
    """A single TranscriptionUnreachable must bail out the loop with rc=1."""
    from talkat import main as main_mod
    from talkat.main import TranscriptionUnreachable, listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events.append(TranscriptionUnreachable("server down"))
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    rc = listen_continuous(
        output_file=str(tmp_path / "transcript.txt"),
        background=False,
        clipboard=False,
    )

    assert rc == 1
    fake = patched_long_mode["fake"]
    assert len(fake.calls) == 1, "loop should have aborted after the first failure"


def test_circuit_breaker_trips_at_max_consecutive_errors(
    clean_pid_files, patched_long_mode, tmp_path: Path
):
    """N consecutive TranscriptionServerError must trip the circuit breaker.

    We set max_consecutive_errors=3 via config_overrides, then feed 3 server
    errors in a row. The loop must exit with rc=1 after exactly 3 attempts.
    """
    from talkat import main as main_mod
    from talkat.main import TranscriptionServerError, listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = [
            TranscriptionServerError("500-1"),
            TranscriptionServerError("500-2"),
            TranscriptionServerError("500-3"),
            # An extra event that should NEVER be consumed (loop must abort
            # before reaching it).
            "should not be reached",
        ]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
        config_overrides={"long_mode_max_consecutive_errors": 3},
    )

    assert rc == 1
    fake = patched_long_mode["fake"]
    # Exactly 3 attempts, then trip.
    assert len(fake.calls) == 3
    # The "should not be reached" event must still be queued.
    assert fake.events == ["should not be reached"]


def test_circuit_breaker_resets_on_successful_transcribe(
    clean_pid_files, patched_long_mode, tmp_path: Path
):
    """A successful transcribe must reset the consecutive-error counter.

    Sequence with max=3:
      err, err, success, err, err, <auto-stop>
    The two pre-success errors should NOT count against the post-success
    errors. The loop must NOT trip, and should exit cleanly with rc=0.
    """
    from talkat import main as main_mod
    from talkat.main import TranscriptionServerError, listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = [
            TranscriptionServerError("500-a"),
            TranscriptionServerError("500-b"),
            "hello world",
            TranscriptionServerError("500-c"),
            TranscriptionServerError("500-d"),
            # After this, events is empty → fake sets stop_event → loop exits rc=0.
        ]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
        config_overrides={"long_mode_max_consecutive_errors": 3},
    )

    assert rc == 0, "circuit breaker tripped — counter did not reset on success"
    fake = patched_long_mode["fake"]
    # 5 attempted events + 1 final call that triggers stop_event on the empty queue.
    assert len(fake.calls) == 6


def test_transcript_is_appended_to_disk_each_segment(
    clean_pid_files, patched_long_mode, tmp_path: Path
):
    """Each non-empty utterance must be appended to the transcript file.

    The §3 memory-cap fix replaced the in-memory ``full_transcript`` list with
    direct file appends; the file is the source of truth for the final clipboard
    copy. This test verifies the on-disk path captures every segment.
    """
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["hello", "this is talkat", "goodbye"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
    )

    assert rc == 0
    assert output.exists()
    contents = output.read_text(encoding="utf-8")
    # Each segment is written with a trailing space.
    assert "hello " in contents
    assert "this is talkat " in contents
    assert "goodbye " in contents
    # Order must match the event order.
    assert contents.index("hello") < contents.index("this is talkat") < contents.index("goodbye")


def test_empty_transcribe_results_are_not_written_to_disk(
    clean_pid_files, patched_long_mode, tmp_path: Path
):
    """An empty transcription (silence) must NOT be written to the transcript file."""
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        # Two real utterances bracketed by empty results.
        fake.events = ["", "hello", "", "world", ""]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
    )

    assert rc == 0
    contents = output.read_text(encoding="utf-8")
    # Only the non-empty utterances should appear.
    assert "hello " in contents
    assert "world " in contents
    # And nothing extra — the empty ones added no characters.
    assert contents.strip().split() == ["hello", "world"]


def test_transcribe_call_uses_silence_timeout_as_max_duration(
    clean_pid_files, patched_long_mode, tmp_path: Path
):
    """Each transcribe call must be capped at the configured silence timeout.

    This is what keeps the loop responsive to session/silence limits — without
    it, a single long blocking call would prevent the limit checks from firing.
    """
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["one"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
        config_overrides={"long_mode_silence_timeout": 7.5},
    )

    fake = patched_long_mode["fake"]
    assert fake.calls
    for call in fake.calls:
        assert call["max_duration"] == 7.5
