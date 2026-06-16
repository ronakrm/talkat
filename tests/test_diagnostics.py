"""Tests for talkat.diagnostics — record assembly and file write."""

from __future__ import annotations

import json

import pytest

from talkat.diagnostics import build_record


def test_build_record_basic_fields():
    record = build_record(
        mode="listen",
        audio_duration=4.2,
        asr_seconds=1.05,
        applied_gain_db=3.5,
        model_type="faster-whisper",
        model_name="small.en",
        transcript_chars=42,
        transcript_words=8,
    )
    assert record["mode"] == "listen"
    assert record["audio_duration_seconds"] == 4.2
    assert record["asr_seconds"] == 1.05
    assert record["model_type"] == "faster-whisper"
    assert record["model_name"] == "small.en"
    assert record["transcript_chars"] == 42
    assert record["transcript_words"] == 8
    assert record["postprocess_profile"] is None
    assert record["errors"] == []
    # RTF is wall-clock / audio_duration. 1.05 / 4.2 ≈ 0.25.
    assert abs(record["realtime_factor"] - 0.25) < 1e-6


def test_realtime_factor_is_none_when_audio_duration_zero():
    """No audio → no RTF; we report None rather than dividing by zero."""
    record = build_record(
        mode="listen",
        audio_duration=0.0,
        asr_seconds=0.5,
        applied_gain_db=0.0,
        model_type="faster-whisper",
        model_name="small.en",
        transcript_chars=0,
        transcript_words=0,
    )
    assert record["realtime_factor"] is None


def test_build_record_includes_extras_and_errors():
    record = build_record(
        mode="long",
        audio_duration=120.0,
        asr_seconds=30.0,
        applied_gain_db=2.0,
        model_type="faster-whisper",
        model_name="small.en",
        transcript_chars=2400,
        transcript_words=400,
        postprocess_profile="ollama-cleanup",
        errors=["server_error: 500 once", "audio: input overflow"],
        extra={"session_seconds": 130.4, "utterances_with_gain_boost": 7},
    )
    assert record["errors"] == ["server_error: 500 once", "audio: input overflow"]
    assert record["postprocess_profile"] == "ollama-cleanup"
    assert record["extra"] == {"session_seconds": 130.4, "utterances_with_gain_boost": 7}


def test_write_record_creates_latest_and_timestamped_files(tmp_path, monkeypatch):
    """Each write must drop both ``diagnostics.latest.json`` and a timestamped copy."""
    # Redirect the module-level DIAGNOSTICS_DIR at the diagnostics module since
    # that's what write_record reaches for. Reload-style patching keeps the
    # rest of the test process untouched.
    from talkat import diagnostics as diag

    monkeypatch.setattr(diag, "DIAGNOSTICS_DIR", tmp_path)

    record = build_record(
        mode="listen",
        audio_duration=2.0,
        asr_seconds=0.5,
        applied_gain_db=0.0,
        model_type="faster-whisper",
        model_name="small.en",
        transcript_chars=10,
        transcript_words=2,
    )
    path = diag.write_record(record)
    assert path is not None
    latest = tmp_path / "diagnostics.latest.json"
    assert latest.exists()
    timestamped = list(tmp_path.glob("diagnostics_*.json"))
    assert len(timestamped) == 1
    # Both files have identical contents.
    assert latest.read_text() == timestamped[0].read_text()
    # And the contents are valid JSON that round-trips to the record.
    loaded = json.loads(latest.read_text())
    assert loaded["mode"] == "listen"
    assert loaded["audio_duration_seconds"] == 2.0


def test_write_record_is_advisory_on_failure(tmp_path, monkeypatch):
    """A non-writable diagnostics dir must not raise — diagnostics are advisory."""
    from talkat import diagnostics as diag

    # Point at a path that can't be created (an existing file masquerading
    # as a directory). The function should log and return None, not crash.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    monkeypatch.setattr(diag, "DIAGNOSTICS_DIR", blocker / "subpath")

    record = build_record(
        mode="file",
        audio_duration=1.0,
        asr_seconds=0.5,
        applied_gain_db=0.0,
        model_type="faster-whisper",
        model_name="small.en",
        transcript_chars=0,
        transcript_words=0,
    )
    # No exception should propagate.
    result = diag.write_record(record)
    assert result is None


@pytest.mark.parametrize(
    "mode,extra",
    [
        ("listen", None),
        ("long", {"session_seconds": 60.0}),
        ("file", {"input_file": "/tmp/x.wav"}),
    ],
)
def test_record_round_trips_through_json(mode: str, extra: dict | None):
    """The whole record must JSON-encode cleanly with stdlib json."""
    record = build_record(
        mode=mode,
        audio_duration=5.0,
        asr_seconds=1.0,
        applied_gain_db=0.0,
        model_type="faster-whisper",
        model_name="small.en",
        transcript_chars=20,
        transcript_words=4,
        extra=extra,
    )
    s = json.dumps(record)
    back = json.loads(s)
    assert back["mode"] == mode
    if extra:
        assert back["extra"] == extra
    else:
        assert "extra" not in back
