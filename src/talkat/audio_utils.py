"""Audio preprocessing helpers — applied server-side before ASR.

These transforms operate on the float32 mono 16 kHz buffers that
``backends.py`` accepts. They are deliberately lightweight (RMS-based,
no FFT, no external deps beyond numpy) so the server can apply them
inline per request without measurable latency.
"""

from __future__ import annotations

import math

import numpy as np

# A signal whose RMS sits below this is treated as silence / background
# noise and left alone — amplifying it would just amplify hiss.
SILENCE_RMS_DBFS = -60.0

# Default target RMS for normalized speech. Most clean-speech corpora sit
# in the -18 to -23 dBFS range; -20 keeps headroom for transients without
# squashing dynamics.
DEFAULT_TARGET_RMS_DBFS = -20.0

# Cap on how much we'll boost a quiet signal. Without a cap, a near-silent
# clip would get +60 dB and become unintelligible noise.
DEFAULT_MAX_GAIN_DB = 20.0

# Post-gain peak ceiling — gain is scaled back further if the loudest
# sample would exceed this. 0.95 (≈ -0.45 dBFS) leaves a margin for
# downstream resampling / quantization to not introduce clipping.
DEFAULT_PEAK_CEILING = 0.95


def _rms(audio: np.ndarray) -> float:
    """Root-mean-square of a float32 audio buffer, as a linear amplitude."""
    if audio.size == 0:
        return 0.0
    # float64 accumulator keeps the mean accurate on long buffers where
    # float32 summation can drift.
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))


def _amp_to_dbfs(amp: float) -> float:
    """Convert a linear amplitude (0-1) to dBFS. -inf for true silence."""
    if amp <= 0.0:
        return -math.inf
    return 20.0 * math.log10(amp)


def normalize_gain(
    audio: np.ndarray,
    target_rms_dbfs: float = DEFAULT_TARGET_RMS_DBFS,
    max_gain_db: float = DEFAULT_MAX_GAIN_DB,
    silence_rms_dbfs: float = SILENCE_RMS_DBFS,
    peak_ceiling: float = DEFAULT_PEAK_CEILING,
) -> tuple[np.ndarray, float]:
    """Scale ``audio`` toward ``target_rms_dbfs``, returning (audio, applied_db).

    The function is *defensive*: it never amplifies pure silence, never
    boosts beyond ``max_gain_db``, and never lets the post-gain peak
    exceed ``peak_ceiling``. ``applied_db`` is what was actually applied
    after all three caps — usable as a diagnostic.

    The input is not mutated; callers receive a new array when gain is
    non-zero. Empty / too-quiet input is returned unchanged with 0 dB.
    """
    if audio.size == 0:
        return audio, 0.0

    rms = _rms(audio)
    rms_dbfs = _amp_to_dbfs(rms)

    # Too quiet to be speech — leave it alone.
    if rms_dbfs < silence_rms_dbfs:
        return audio, 0.0

    desired_gain_db = target_rms_dbfs - rms_dbfs
    # Only boost; if the signal is already louder than the target, leave
    # it (lowering gain would lose information for no real benefit, and
    # would risk overshooting the next ASR call's expected range).
    if desired_gain_db <= 0.0:
        return audio, 0.0

    applied_db = min(desired_gain_db, max_gain_db)
    gain_factor = 10.0 ** (applied_db / 20.0)

    # Peak protection: if scaling would push samples above the ceiling,
    # pull the gain back so the loudest sample lands exactly at the
    # ceiling. This costs us some target-RMS accuracy but keeps the
    # buffer numerically clean.
    peak = float(np.max(np.abs(audio)))
    if peak * gain_factor > peak_ceiling:
        gain_factor = peak_ceiling / peak if peak > 0.0 else 1.0
        applied_db = 20.0 * math.log10(gain_factor) if gain_factor > 0.0 else 0.0

    if applied_db <= 0.0:
        return audio, 0.0

    return (audio * gain_factor).astype(np.float32), applied_db


# ---------------------------------------------------------------------------
# Long-form segmentation
# ---------------------------------------------------------------------------
#
# ASR engines have practical length limits — Whisper's position embeddings
# top out around the ~30s its encoder was trained on (with overlap-stitching
# extending that to ~400s before breakdown), and a single Vosk
# ``AcceptWaveform`` over a multi-minute buffer is the classic 500-error
# trigger in this project. Even when an engine "supports" long-form, a
# single in-memory pass over 30+ minutes of float32 audio is wasteful.
#
# The fix borrowed from speech-note: split long inputs at energy minima
# (best-effort, no external VAD) so each pass sees a manageable chunk. We
# never split inside speech if we can help it — the search picks the
# quietest frame within a wide window around each target boundary.


def _frame_rms(audio: np.ndarray, frame_samples: int) -> np.ndarray:
    """Per-frame RMS over fixed-size windows. Last partial frame is dropped."""
    n_frames = audio.size // frame_samples
    if n_frames == 0:
        return np.zeros(0, dtype=np.float32)
    trimmed = audio[: n_frames * frame_samples].reshape(n_frames, frame_samples)
    rms: np.ndarray = np.sqrt(np.mean(trimmed.astype(np.float64) ** 2, axis=1)).astype(np.float32)
    return rms


def segment_long_audio(
    audio: np.ndarray,
    sample_rate: int = 16000,
    max_segment_seconds: float = 480.0,
    search_window_seconds: float = 30.0,
    frame_ms: int = 100,
) -> list[np.ndarray]:
    """Split ``audio`` into ≤ ``max_segment_seconds`` chunks at energy minima.

    Returns a list of contiguous slices that, concatenated, reproduce the
    input (with at most one boundary-frame's worth of jitter from how the
    split index lands). If the input is already short enough, the return
    is just ``[audio]`` so callers don't need a length check.

    The split picker looks at a window of ``search_window_seconds`` centered
    on each target boundary and picks the quietest frame in that window.
    With no clean silence, the boundary lands at the target — a bit awkward,
    but bounded segment length matters more than perfect alignment for the
    failure mode this is solving (OOM / position-embedding cliffs).
    """
    duration = audio.size / float(sample_rate)
    if duration <= max_segment_seconds:
        return [audio]

    frame_samples = max(1, int(sample_rate * frame_ms / 1000))
    rms = _frame_rms(audio, frame_samples)
    if rms.size == 0:
        # Pathological input; just hard-split.
        rms = np.zeros(1, dtype=np.float32)

    target_step_frames = int(max_segment_seconds * 1000 / frame_ms)
    window_half_frames = int(search_window_seconds * 1000 / frame_ms / 2)

    boundaries: list[int] = []  # in samples
    cursor = 0
    while True:
        target_frame = (cursor // frame_samples) + target_step_frames
        if target_frame >= rms.size:
            break

        lo = max(target_frame - window_half_frames, cursor // frame_samples + 1)
        hi = min(target_frame + window_half_frames, rms.size)
        if lo >= hi:
            split_frame = target_frame
        else:
            # Quietest frame in the window — that's the natural break.
            split_frame = lo + int(np.argmin(rms[lo:hi]))

        split_sample = split_frame * frame_samples
        if split_sample <= cursor:
            # Defensive: never go backwards. Force forward progress.
            split_sample = cursor + target_step_frames * frame_samples
        if split_sample >= audio.size:
            break

        boundaries.append(split_sample)
        cursor = split_sample

    if not boundaries:
        return [audio]

    segments: list[np.ndarray] = []
    prev = 0
    for b in boundaries:
        segments.append(audio[prev:b])
        prev = b
    segments.append(audio[prev:])
    # Drop any zero-length tail that can appear if the last boundary
    # landed exactly at the end.
    return [s for s in segments if s.size > 0]
