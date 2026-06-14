"""Tests for talkat.file_processor — format_output and batch processing.

The on-the-wire HTTP path is covered by tests/test_integration_file_processor.py.
This file is for pure-function format_output and the batch orchestration
around it (no real server needed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talkat.file_processor import (
    batch_process_files,
    format_output,
    process_audio_file_command,
)

# ---------------------------------------------------------------------------
# format_output
# ---------------------------------------------------------------------------


def test_format_output_text_is_passthrough():
    assert format_output("hello world", 1.5, "text") == "hello world"


def test_format_output_text_is_default_format():
    assert format_output("hello", 1.0, "unknown-format") == "hello"


def test_format_output_json_includes_text_and_words():
    out = format_output("hello world from talkat", 3.25, "json")
    obj = json.loads(out)
    assert obj["text"] == "hello world from talkat"
    assert obj["duration"] == 3.25
    assert obj["words"] == ["hello", "world", "from", "talkat"]


def test_format_output_srt_has_subtitle_index_and_timestamps():
    out = format_output("hello", 1.5, "srt")
    assert out.startswith("1\n")
    assert "00:00:00,000 -->" in out
    assert "hello" in out


def test_format_output_vtt_has_webvtt_header():
    out = format_output("hello", 1.5, "vtt")
    assert out.startswith("WEBVTT")
    assert "00:00:00.000 -->" in out
    assert "hello" in out


def test_format_output_srt_uses_comma_decimal_separator_for_timestamps():
    """SRT spec requires a comma; VTT requires a dot. Don't mix them up."""
    srt = format_output("x", 1.5, "srt")
    vtt = format_output("x", 1.5, "vtt")
    # SRT line has "00:00:00,000 -->"; VTT line has "00:00:00.000 -->".
    assert "00:00:00,000 -->" in srt
    assert "00:00:00.000 -->" in vtt


# ---------------------------------------------------------------------------
# process_audio_file_command — orchestration around transcribe + format
# ---------------------------------------------------------------------------


def test_process_audio_file_command_writes_to_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When output_file is given, transcription is written to that path."""
    from talkat import file_processor as fp

    monkeypatch.setattr(fp, "transcribe_audio_file", lambda *_args, **_kwargs: ("hello there", 2.0))

    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00\x00")  # contents are irrelevant — transcribe is stubbed
    dst = tmp_path / "out.txt"

    rc = process_audio_file_command(str(src), output_file=str(dst), output_format="text")

    assert rc == 0
    assert dst.read_text() == "hello there"


def test_process_audio_file_command_returns_one_when_no_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty transcription must surface as exit code 1."""
    from talkat import file_processor as fp

    monkeypatch.setattr(fp, "transcribe_audio_file", lambda *_a, **_k: ("", 1.0))

    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00\x00")

    rc = process_audio_file_command(str(src), output_format="text")
    assert rc == 1


def test_process_audio_file_command_writes_json_format_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from talkat import file_processor as fp

    monkeypatch.setattr(fp, "transcribe_audio_file", lambda *_a, **_k: ("ok then", 1.0))

    src = tmp_path / "in.wav"
    src.write_bytes(b"\x00\x00")
    dst = tmp_path / "out.json"

    rc = process_audio_file_command(str(src), output_file=str(dst), output_format="json")
    assert rc == 0

    payload = json.loads(dst.read_text())
    assert payload["text"] == "ok then"
    assert payload["words"] == ["ok", "then"]


# ---------------------------------------------------------------------------
# batch_process_files
# ---------------------------------------------------------------------------


def test_batch_process_files_writes_each_to_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each input file produces one output file under the output dir."""
    from talkat import file_processor as fp

    # Stub transcribe to return a per-filename transcript.
    def fake_transcribe(path: str, *_a, **_k) -> tuple[str, float]:
        name = Path(path).stem
        return (f"transcript of {name}", 1.0)

    monkeypatch.setattr(fp, "transcribe_audio_file", fake_transcribe)

    inputs = []
    for name in ("a", "b", "c"):
        f = tmp_path / f"{name}.wav"
        f.write_bytes(b"\x00\x00")
        inputs.append(str(f))

    outdir = tmp_path / "out"
    rc = batch_process_files(inputs, output_dir=str(outdir), output_format="text")

    assert rc == 0
    assert (outdir / "a.txt").read_text() == "transcript of a"
    assert (outdir / "b.txt").read_text() == "transcript of b"
    assert (outdir / "c.txt").read_text() == "transcript of c"


def test_batch_process_files_returns_one_when_any_file_has_no_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Any failure (empty result or exception) flips the batch return to 1."""
    from talkat import file_processor as fp

    results = iter([("good", 1.0), ("", 1.0)])
    monkeypatch.setattr(fp, "transcribe_audio_file", lambda *_a, **_k: next(results))

    files = []
    for name in ("good", "bad"):
        f = tmp_path / f"{name}.wav"
        f.write_bytes(b"\x00\x00")
        files.append(str(f))

    outdir = tmp_path / "out"
    rc = batch_process_files(files, output_dir=str(outdir), output_format="text")
    assert rc == 1
    # The good file should still be written.
    assert (outdir / "good.txt").read_text() == "good"
    assert not (outdir / "bad.txt").exists()


def test_batch_process_files_chooses_extension_by_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Output extension must match the requested format."""
    from talkat import file_processor as fp

    monkeypatch.setattr(fp, "transcribe_audio_file", lambda *_a, **_k: ("x", 1.0))

    src = tmp_path / "f.wav"
    src.write_bytes(b"\x00\x00")
    outdir = tmp_path / "out"

    for fmt, ext in (("text", ".txt"), ("json", ".json"), ("srt", ".srt"), ("vtt", ".vtt")):
        # Re-stub on each iter to reset.
        rc = batch_process_files([str(src)], output_dir=str(outdir), output_format=fmt)
        assert rc == 0
        assert (outdir / f"f{ext}").exists()
