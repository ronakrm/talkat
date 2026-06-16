"""Tests for talkat.audio_utils.segment_long_audio.

The contract:
  * Short audio passes through as a single segment.
  * Long audio splits into chunks each ≤ ``max_segment_seconds``.
  * Concatenating the returned segments reproduces the input exactly
    (boundary samples are not lost or duplicated).
  * The splitter prefers low-energy frames when one is available in the
    search window.
"""

from __future__ import annotations

import math

import numpy as np

from talkat.audio_utils import segment_long_audio

SR = 16000


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def _tone(seconds: float, amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def test_short_audio_returns_single_segment():
    audio = _tone(5.0)
    pieces = segment_long_audio(audio, sample_rate=SR, max_segment_seconds=10.0)
    assert len(pieces) == 1
    assert np.array_equal(pieces[0], audio)


def test_audio_at_exact_limit_is_one_piece():
    audio = _tone(10.0)
    pieces = segment_long_audio(audio, sample_rate=SR, max_segment_seconds=10.0)
    assert len(pieces) == 1


def test_long_audio_is_split_to_bounded_segments():
    # 30 seconds, max 10s/segment → expect ~3 pieces.
    audio = _tone(30.0)
    pieces = segment_long_audio(
        audio, sample_rate=SR, max_segment_seconds=10.0, search_window_seconds=4.0
    )
    assert len(pieces) >= 3
    # Every segment must be at or under (limit + half-window) — the window
    # is centered on the target, so the last frame in the window sits at
    # target + window/2.
    max_allowed = (10.0 + 4.0 / 2.0) * SR + SR  # +1s slack
    for seg in pieces:
        assert seg.size <= max_allowed


def test_concatenated_segments_reproduce_input():
    audio = _tone(25.0)
    pieces = segment_long_audio(audio, sample_rate=SR, max_segment_seconds=8.0)
    rejoined = np.concatenate(pieces)
    assert rejoined.size == audio.size
    assert np.array_equal(rejoined, audio)


def test_split_prefers_silence_when_available():
    """A clear silent gap inside the search window should attract the split."""
    # 20s of tone, 2s of silence, 20s of tone. Limit 18s → first split target
    # lands around t=18s. The silence is at t=20–22s; the search window
    # (default 30s/2 = ±15s) easily covers it.
    audio = np.concatenate([_tone(20.0), _silence(2.0), _tone(20.0)])
    pieces = segment_long_audio(
        audio, sample_rate=SR, max_segment_seconds=18.0, search_window_seconds=30.0
    )
    # The first split should land *inside* the silent gap (samples
    # [20*SR, 22*SR]). Find where segment 0 ends.
    boundary = pieces[0].size
    assert 20 * SR <= boundary <= 22 * SR, (
        f"split landed at sample {boundary}, expected within the silent gap "
        f"[{20 * SR}, {22 * SR}]"
    )


def test_empty_audio_returns_empty_list_or_single_empty():
    audio = np.zeros(0, dtype=np.float32)
    pieces = segment_long_audio(audio, sample_rate=SR, max_segment_seconds=10.0)
    # Empty in → either empty list or [empty]; either is fine, just must not crash.
    assert all(p.size == 0 for p in pieces) or pieces == []


def test_no_silence_anywhere_still_produces_bounded_segments():
    """Pathological all-tone audio still gets split, just without nice boundaries."""
    audio = _tone(30.0)
    pieces = segment_long_audio(
        audio, sample_rate=SR, max_segment_seconds=10.0, search_window_seconds=2.0
    )
    # No clean silence → splits land at-or-near the target. We just need
    # bounded pieces and a complete reconstruction.
    rejoined = np.concatenate(pieces)
    assert np.array_equal(rejoined, audio)
    # Each piece ≤ target + window/2 + a small margin.
    for seg in pieces[:-1]:  # last piece can be whatever's left
        assert seg.size <= (10.0 + 2.0 / 2.0) * SR + SR
