"""Tests for talkat.audio_utils — gain normalization invariants.

These are pure-numpy tests, no audio I/O. We verify the contract of
``normalize_gain``:

  * silence (or near-silence) is returned untouched
  * already-loud audio is returned untouched (we only boost)
  * a quiet signal is scaled toward the target RMS
  * the gain cap is honored
  * peak-ceiling protection kicks in to avoid clipping
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from talkat.audio_utils import (
    DEFAULT_MAX_GAIN_DB,
    DEFAULT_TARGET_RMS_DBFS,
    _amp_to_dbfs,
    _rms,
    normalize_gain,
)


def _sine(duration_s: float, freq_hz: float = 440.0, amp: float = 0.1) -> np.ndarray:
    """Generate a float32 sine wave at 16 kHz, RMS = amp / sqrt(2)."""
    n = int(duration_s * 16000)
    t = np.arange(n, dtype=np.float32) / 16000.0
    return (amp * np.sin(2 * math.pi * freq_hz * t)).astype(np.float32)


def test_empty_audio_returns_unchanged():
    audio = np.zeros(0, dtype=np.float32)
    out, gain = normalize_gain(audio)
    assert out.size == 0
    assert gain == 0.0


def test_pure_silence_is_left_alone():
    """RMS below the silence floor must not be amplified — it's just noise."""
    audio = np.zeros(16000, dtype=np.float32)
    out, gain = normalize_gain(audio)
    assert gain == 0.0
    assert np.array_equal(out, audio)


def test_already_loud_audio_is_left_alone():
    """We only boost — squashing loud audio is the wrong call for ASR."""
    # RMS around -10 dBFS (louder than the -20 default target)
    audio = _sine(1.0, amp=0.45)  # RMS ≈ 0.318 ≈ -10 dBFS
    rms_in_dbfs = _amp_to_dbfs(_rms(audio))
    assert rms_in_dbfs > DEFAULT_TARGET_RMS_DBFS  # sanity check
    out, gain = normalize_gain(audio)
    assert gain == 0.0
    assert np.array_equal(out, audio)


def test_quiet_audio_is_boosted_toward_target():
    """A signal at -40 dBFS should get ~+20 dB of gain (subject to the cap)."""
    # RMS ≈ 0.01 → -40 dBFS
    audio = _sine(1.0, amp=0.01 * math.sqrt(2))
    rms_in_dbfs = _amp_to_dbfs(_rms(audio))
    assert -41.0 < rms_in_dbfs < -39.0  # sanity

    out, gain = normalize_gain(audio, target_rms_dbfs=-20.0, max_gain_db=30.0)

    rms_out_dbfs = _amp_to_dbfs(_rms(out))
    # The boost lands at the target within rounding tolerance.
    assert abs(rms_out_dbfs - (-20.0)) < 0.5
    assert abs(gain - 20.0) < 0.5


def test_gain_cap_is_honored():
    """Quiet (but not silent) signals must not be boosted past max_gain_db."""
    # Pick amplitude so RMS lands at ~-50 dBFS — well below the -20 target,
    # well above the -60 silence floor. With max_gain_db=10, only 10 dB
    # should be applied (not the 30 dB the target alone would imply).
    audio = _sine(1.0, amp=(10 ** (-50 / 20.0)) * math.sqrt(2))
    rms_in_dbfs = _amp_to_dbfs(_rms(audio))
    assert -52.0 < rms_in_dbfs < -48.0  # sanity

    out, gain = normalize_gain(audio, target_rms_dbfs=-20.0, max_gain_db=10.0)

    assert abs(gain - 10.0) < 0.01
    rms_out_dbfs = _amp_to_dbfs(_rms(out))
    assert rms_out_dbfs < -20.0


def test_peak_ceiling_prevents_clipping():
    """If the gain would push the peak above 1.0, we scale back."""
    # Audio with peak close to 1.0 but RMS still below the target.
    # A short loud transient surrounded by silence.
    audio = np.zeros(16000, dtype=np.float32)
    audio[8000:8100] = 0.9  # peak = 0.9, RMS tiny → desired_gain is huge
    rms_in_dbfs = _amp_to_dbfs(_rms(audio))
    assert rms_in_dbfs < -20.0  # well below target

    out, gain = normalize_gain(audio, target_rms_dbfs=-10.0, max_gain_db=40.0, peak_ceiling=0.95)
    # Post-gain peak must not exceed the ceiling.
    assert float(np.max(np.abs(out))) <= 0.95 + 1e-6
    # And the applied gain must be smaller than what the RMS target alone would dictate.
    assert gain < 40.0


def test_output_is_float32():
    """ASR backends expect float32 — multiplication can promote to float64."""
    audio = _sine(0.5, amp=0.01)
    out, _ = normalize_gain(audio)
    assert out.dtype == np.float32


def test_input_is_not_mutated():
    """The caller should be able to reuse their buffer afterward."""
    audio = _sine(0.5, amp=0.01)
    snapshot = audio.copy()
    normalize_gain(audio)
    assert np.array_equal(audio, snapshot)


@pytest.mark.parametrize("rms_dbfs", [-50.0, -40.0, -30.0, -25.0])
def test_normalized_rms_does_not_overshoot_target(rms_dbfs: float):
    """Across a sweep of quiet inputs, post-normalization RMS sits at-or-below target."""
    amp = (10 ** (rms_dbfs / 20.0)) * math.sqrt(2)
    audio = _sine(1.0, amp=amp)
    out, _ = normalize_gain(audio, target_rms_dbfs=-20.0, max_gain_db=DEFAULT_MAX_GAIN_DB)
    rms_out_dbfs = _amp_to_dbfs(_rms(out))
    # Allow a small headroom for the peak-ceiling pullback.
    assert rms_out_dbfs <= -20.0 + 0.5
