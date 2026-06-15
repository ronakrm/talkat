"""Tests for talkat.main.listen_continuous (long-mode dictation).

We stub out TranscriptionClient with a programmable fake so we can drive
the loop through specific code paths — server-unreachable exit, the §3
consecutive-error circuit breaker, and the on-disk transcript invariant
(no in-memory accumulation of segments).
"""

import signal
import threading
import time as time_mod
from collections.abc import Iterator
from itertools import chain, repeat
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


# ---------------------------------------------------------------------------
# Time-limit branches — safety invariants
# ---------------------------------------------------------------------------
#
# These two tests pin down the "session can't run unbounded" guarantee.
# listen_continuous reads two ceilings from config:
#
#   long_mode_max_session_duration — hard wall-clock cap per session
#   long_mode_silence_timeout      — auto-stop if no speech for N seconds
#
# Both are checked at the top of each loop iteration. If either trips the
# loop breaks cleanly with rc=0. We drive time.monotonic through a fake
# clock so we can assert the branches actually fire without waiting in
# real time. Without these tests, a regression that swaps a "<" for a ">"
# (or drops a check entirely) would silently let dictation run forever.


def _fake_clock(values: list[float]):
    """Build a monotonic() stub that walks ``values`` then repeats the last one.

    Returning the last value forever (instead of StopIteration) is defensive:
    if listen_continuous adds another time.monotonic call later, the test
    won't crash — it just won't assert on the new call. The branch under
    test is still exercised via the values we did supply.
    """
    it = chain(iter(values), repeat(values[-1]))
    return lambda: next(it)


def test_max_session_duration_break_stops_the_loop(
    clean_pid_files, patched_long_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Crossing long_mode_max_session_duration must break the loop cleanly.

    Sequence of time.monotonic() calls in listen_continuous:
      1. session_start             (before loop)
      2. now, iter 1               (top of loop)
      3. last_speech_at, iter 1    (after non-empty transcribe)
      4. now, iter 2               (top of loop — MUST trip max here)

    With max=5 and silence=1000, iter 1 sees now=1 and proceeds. After
    "hello" lands, last_speech_at=2. Iter 2 sees now=999 — 999 > 5, so the
    max-session branch breaks before transcribe is even attempted. The
    queued "should not be reached" event must remain un-consumed.
    """
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    monkeypatch.setattr(time_mod, "monotonic", _fake_clock([0.0, 1.0, 2.0, 999.0]))

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["hello", "should not be reached"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
        config_overrides={
            "long_mode_max_session_duration": 5.0,
            "long_mode_silence_timeout": 1000.0,
        },
    )

    assert rc == 0, "max_session_duration break is a clean exit, not an error"
    fake = patched_long_mode["fake"]
    assert len(fake.calls) == 1, "loop must break on iter 2 before transcribing again"
    assert fake.events == [
        "should not be reached"
    ], "queued event must remain un-consumed when max-session break fires"


# ---------------------------------------------------------------------------
# §5a postprocess — end-of-session AIPP for listen_continuous
# ---------------------------------------------------------------------------


def test_long_mode_postprocess_runs_once_on_full_transcript(
    clean_pid_files, patched_long_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """AIPP must run exactly once at session end with the concatenated transcript.

    Per-utterance AIPP would lose cross-utterance context and multiply LLM
    cost N times — see listen_continuous docstring. This test pins the
    "once at the end on the full text" semantics.
    """
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["hello", "this is talkat", "goodbye"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    aipp_calls: list[tuple[str, str]] = []

    def fake_postprocess(text: str, profile_name: str, *, config: dict | None = None) -> str:
        aipp_calls.append((text, profile_name))
        return "POLISHED: " + text.strip()

    monkeypatch.setattr("talkat.postprocess.postprocess_text", fake_postprocess)

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
        postprocess="tidy",
    )

    assert rc == 0
    assert len(aipp_calls) == 1, "AIPP should run exactly once for the whole session"
    text, profile = aipp_calls[0]
    assert profile == "tidy"
    assert "hello" in text and "this is talkat" in text and "goodbye" in text

    # Side-by-side .processed.txt should hold the AIPP output; raw transcript
    # remains as the source of truth.
    processed = output.with_suffix(".processed.txt")
    assert processed.exists(), "expected .processed.txt next to the raw transcript"
    assert processed.read_text(encoding="utf-8").startswith("POLISHED: ")
    assert "hello" in output.read_text(encoding="utf-8"), "raw transcript must remain intact"


def test_long_mode_postprocess_not_invoked_when_arg_omitted(
    clean_pid_files, patched_long_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["hello"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    called: list[bool] = []

    def boom(*_a, **_kw) -> str:
        called.append(True)
        return ""

    monkeypatch.setattr("talkat.postprocess.postprocess_text", boom)

    output = tmp_path / "transcript.txt"
    listen_continuous(output_file=str(output), background=False, clipboard=False)
    assert called == []


def test_long_mode_postprocess_failopen_keeps_raw_clipboard(
    clean_pid_files, patched_long_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """When AIPP returns the input unchanged, no .processed.txt is written.

    The side-by-side file only exists when AIPP actually changed the text.
    This avoids polluting the transcripts dir with no-op duplicates and
    makes the file's presence a signal that AIPP ran successfully.
    """
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["hello world"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    # fail-open shape: returns input unchanged
    monkeypatch.setattr(
        "talkat.postprocess.postprocess_text",
        lambda text, _name, **_kw: text,
    )

    clipboard_calls: list[str] = []
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda t: clipboard_calls.append(t) or True)

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=True,
        postprocess="tidy",
    )

    assert rc == 0
    processed = output.with_suffix(".processed.txt")
    assert not processed.exists(), "no .processed.txt expected when AIPP was a no-op"
    assert clipboard_calls, "clipboard should still be fed the raw transcript"
    assert "hello world" in clipboard_calls[0]


def test_silence_timeout_break_stops_the_loop(
    clean_pid_files, patched_long_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No speech for silence_timeout seconds must break the loop cleanly.

    With silence_timeout=5 and max=1000:
      1. session_start = 0
      2. now (iter 1) = 1                   → 1 < 5 silence, 1 < 1000 max → proceed
      3. transcribe returns "" (silence)    → last_speech_at NOT updated
      4. now (iter 2) = 10                  → 10-0 = 10 > 5 silence → BREAK

    Empty results don't update last_speech_at (only ``if text:`` does), so
    the silence countdown is from session_start. This branch is what auto-
    stops dictation when the user walks away.
    """
    from talkat import main as main_mod
    from talkat.main import listen_continuous

    monkeypatch.setattr(time_mod, "monotonic", _fake_clock([0.0, 1.0, 10.0]))

    def factory(config: dict) -> FakeTranscriber:
        fake = FakeTranscriber(config)
        fake.events = ["", "should not be reached"]
        patched_long_mode["fake"] = fake
        return fake

    main_mod.TranscriptionClient = factory  # type: ignore[attr-defined]

    output = tmp_path / "transcript.txt"
    rc = listen_continuous(
        output_file=str(output),
        background=False,
        clipboard=False,
        config_overrides={
            "long_mode_max_session_duration": 1000.0,
            "long_mode_silence_timeout": 5.0,
        },
    )

    assert rc == 0, "silence_timeout break is a clean exit, not an error"
    fake = patched_long_mode["fake"]
    assert len(fake.calls) == 1, "loop must break on iter 2 before transcribing again"
    assert fake.events == [
        "should not be reached"
    ], "queued event must remain un-consumed when silence-timeout break fires"
