"""Tests for talkat.segmenter — where live audio is cut into transcription segments.

Pure: synthetic 30 ms int16 chunks of "speech" (a constant level well above
the threshold) and silence. What we pin down: cuts land inside pauses, never
before MIN_SEGMENT_S; continuous speech is force-cut at MAX_SEGMENT_S at its
quietest moment; silence alone is never cut at a "pause"; and the segments
always concatenate back to exactly the audio fed in.
"""

from __future__ import annotations

import numpy as np
import pytest

from talkat import segmenter as seg_mod
from talkat.segmenter import Segment, Segmenter

CHUNK_SAMPLES = 480  # 30 ms at 16 kHz
THRESHOLD = 200.0


def _chunks(seconds: float, level: int) -> list[bytes]:
    n = round(seconds / 0.03)
    return [np.full(CHUNK_SAMPLES, level, dtype=np.int16).tobytes()] * n


def speech(seconds: float, level: int = 5000) -> list[bytes]:
    return _chunks(seconds, level)


def silence(seconds: float) -> list[bytes]:
    return _chunks(seconds, 0)


def _feed(segmenter: Segmenter, chunks: list[bytes]) -> list[Segment]:
    segments = []
    for chunk in chunks:
        segment = segmenter.feed(chunk)
        if segment is not None:
            segments.append(segment)
    return segments


def _run(chunks: list[bytes]) -> list[Segment]:
    """Feed everything, then flush the tail."""
    segmenter = Segmenter(THRESHOLD)
    segments = _feed(segmenter, chunks)
    tail = segmenter.flush()
    return segments + ([tail] if tail is not None else [])


def test_segments_concatenate_back_to_the_input():
    """Segmentation decides where to cut, never what to send."""
    rng = np.random.default_rng(0)
    chunks = []
    for _ in range(40):
        seconds = float(rng.uniform(0.1, 6.0))
        chunks += speech(seconds, level=int(rng.integers(50, 8000))) + silence(
            float(rng.uniform(0.0, 1.5))
        )
    segments = _run(chunks)

    assert len(segments) > 1
    assert b"".join(s.pcm for s in segments) == b"".join(chunks)


def test_cuts_in_the_middle_of_a_pause():
    audio = speech(5.0) + silence(1.0) + speech(2.0)
    segments = _run(audio)

    assert [s.cut for s in segments] == ["pause", "tail"]
    # The cut lands inside the pause, leaving no speech on the wrong side.
    assert 5.0 < segments[0].seconds < 6.0
    first_after_cut = np.frombuffer(segments[1].pcm[: CHUNK_SAMPLES * 2], dtype=np.int16)
    assert not first_after_cut.any()


def test_no_pause_cut_before_the_minimum_length():
    """A pause early in a segment doesn't cut; the next one, past MIN_SEGMENT_S, does."""
    early = seg_mod.MIN_SEGMENT_S / 2
    audio = speech(early) + silence(0.8) + speech(2.0) + silence(1.0) + speech(1.0)
    segments = _run(audio)

    assert [s.cut for s in segments] == ["pause", "tail"]
    assert segments[0].seconds > early + 0.8 + 2.0


def test_gaps_between_words_are_not_pauses():
    audio = (speech(0.6) + silence(0.3)) * 15  # 13.5 s of choppy speech
    segmenter = Segmenter(THRESHOLD)

    assert _feed(segmenter, audio) == []


def test_silence_alone_is_never_cut_at_a_pause():
    """Nothing to transcribe yet — only the length cap may end a silent segment."""
    segmenter = Segmenter(THRESHOLD)

    assert _feed(segmenter, silence(seg_mod.MAX_SEGMENT_S - 1)) == []


def test_continuous_speech_is_cut_at_its_quietest_moment():
    """No pause by MAX_SEGMENT_S: cut inside the quietest stretch of the last
    HARD_CUT_SEARCH_S — here a softer (still above-threshold) 0.3 s dip."""
    audio = speech(24.0) + speech(0.3, level=1500) + speech(10.0)
    segmenter = Segmenter(THRESHOLD)
    segments = _feed(segmenter, audio)

    assert len(segments) == 1
    assert segments[0].cut == "max"
    assert 24.0 <= segments[0].seconds <= 24.3


def test_noise_above_the_threshold_is_still_cut_at_the_cap():
    """The calibrated threshold can't see pauses in a loud room; the cap still bounds segments."""
    audio = speech(65.0, level=400)  # noise-level "speech" above THRESHOLD, no pauses
    segments = _run(audio)

    assert all(s.seconds <= seg_mod.MAX_SEGMENT_S for s in segments)
    assert [s.cut for s in segments][:2] == ["max", "max"]


def test_flush_returns_the_tail_once():
    segmenter = Segmenter(THRESHOLD)
    _feed(segmenter, speech(1.0))

    tail = segmenter.flush()
    assert tail is not None and tail.cut == "tail"
    assert tail.seconds == pytest.approx(0.99, abs=0.03)
    assert segmenter.flush() is None


def test_pause_counting_continues_across_a_cut():
    """The half of a pause carried into the next segment must not hide the
    speech that follows it from the next pause cut."""
    audio = speech(5.0) + silence(1.0) + speech(4.0) + silence(1.0) + speech(1.0)
    segments = _run(audio)

    assert [s.cut for s in segments] == ["pause", "pause", "tail"]
