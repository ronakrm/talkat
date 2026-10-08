"""Cut a live 16 kHz mono int16 stream into transcription segments at natural pauses.

Pure and synchronous: feed it the capture loop's ~30 ms chunks and collect the
segments it completes. A segment ends:

* at the first pause of ``PAUSE_S`` once it holds ``MIN_SEGMENT_S`` of audio
  with some speech in it — cut in the middle of the pause, so no word is split;
* otherwise at ``MAX_SEGMENT_S`` (continuous speech, or noise the calibrated
  threshold can't tell from speech), at the quietest moment of the last
  ``HARD_CUT_SEARCH_S`` — a spot that needs no threshold to find;
* at stop, via :meth:`Segmenter.flush`.

Segmentation decides where to cut, never what to send: the segments
concatenate back to exactly the bytes fed in. Silence reaches the server,
whose VAD filter trims it — dropping "quiet" audio client-side is how quiet
speakers' words get lost.
"""

import collections
from dataclasses import dataclass
from typing import Literal

import numpy as np

SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2

# Shortest segment a pause may end, so pieces stay sentence-sized rather than
# a few words each; the tail at stop can be shorter.
MIN_SEGMENT_S = 3.0
# How long the level must stay below the threshold to count as a pause. In
# fluent read speech the pauses measured 0.36 s at the median and rarely
# reached 0.7 s; at 0.4 s every cut in a 3-minute sample fell between
# sentences (median piece 9 s), and with the previous text as context the
# result matched whole-file transcription to 0.2% of words.
PAUSE_S = 0.4
# Longest segment: Whisper's native window, so a segment is decoded in one pass.
MAX_SEGMENT_S = 30.0
# Where a forced cut looks for the quietest moment, and how wide that moment is.
HARD_CUT_SEARCH_S = 10.0
QUIET_WINDOW_S = 0.3
# Level smoothing, matching AudioSession's silence detection.
_SMOOTHING_CHUNKS = 3

CutReason = Literal["pause", "max", "tail"]


@dataclass(frozen=True)
class Segment:
    pcm: bytes
    cut: CutReason

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (_BYTES_PER_SAMPLE * SAMPLE_RATE)


class Segmenter:
    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self._chunks: list[bytes] = []
        self._rms: list[float] = []
        self._loud: list[bool] = []
        self._samples = 0
        self._has_speech = False
        # Length of the quiet run at the end of the current segment.
        self._quiet_chunks = 0
        self._quiet_samples = 0
        self._recent_rms: collections.deque[float] = collections.deque(maxlen=_SMOOTHING_CHUNKS)

    @property
    def quiet_seconds(self) -> float:
        """How long the level has been below the threshold, from the audio itself.

        Time-free and noise-honest: it says whether anyone is talking right
        now, which is what a "still recording?" reminder needs.
        """
        return self._quiet_samples / SAMPLE_RATE

    def feed(self, chunk: bytes) -> Segment | None:
        """Add one chunk; return a segment if this chunk completed one."""
        samples = len(chunk) // _BYTES_PER_SAMPLE
        if samples == 0:
            return None
        audio = np.frombuffer(chunk[: samples * _BYTES_PER_SAMPLE], dtype=np.int16)
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        self._recent_rms.append(rms)
        loud = float(np.mean(self._recent_rms)) > self.threshold

        self._chunks.append(chunk)
        self._rms.append(rms)
        self._loud.append(loud)
        self._samples += samples
        if loud:
            self._has_speech = True
            self._quiet_chunks = 0
            self._quiet_samples = 0
        else:
            self._quiet_chunks += 1
            self._quiet_samples += samples

        seconds = self._samples / SAMPLE_RATE
        if (
            self._has_speech
            and seconds >= MIN_SEGMENT_S
            and self._quiet_samples >= PAUSE_S * SAMPLE_RATE
        ):
            return self._cut(len(self._chunks) - self._quiet_chunks // 2, "pause")
        if seconds >= MAX_SEGMENT_S:
            return self._cut(self._quietest_cut_index(), "max")
        return None

    def flush(self) -> Segment | None:
        """Return whatever is buffered as the final segment."""
        if not self._chunks:
            return None
        return self._cut(len(self._chunks), "tail")

    def _quietest_cut_index(self) -> int:
        """Chunk index at the middle of the quietest window near the segment's end."""
        chunk_s = self._samples / len(self._chunks) / SAMPLE_RATE
        window = max(1, round(QUIET_WINDOW_S / chunk_s))
        start = max(1, len(self._rms) - round(HARD_CUT_SEARCH_S / chunk_s))
        region = np.asarray(self._rms[start:], dtype=np.float64)
        if region.size <= window:
            return len(self._chunks)
        loudness = np.convolve(region, np.ones(window), mode="valid")
        # The latest of equally quiet windows: in silence, carry over as little as possible.
        quietest = len(loudness) - 1 - int(np.argmin(loudness[::-1]))
        return start + quietest + window // 2

    def _cut(self, index: int, reason: CutReason) -> Segment:
        segment = Segment(b"".join(self._chunks[:index]), reason)
        self._chunks = self._chunks[index:]
        self._rms = self._rms[index:]
        self._loud = self._loud[index:]
        self._samples = sum(len(c) // _BYTES_PER_SAMPLE for c in self._chunks)
        self._has_speech = any(self._loud)
        # The carried-over audio may end in part of a pause: keep counting it.
        self._quiet_chunks = 0
        self._quiet_samples = 0
        for chunk, loud in zip(reversed(self._chunks), reversed(self._loud), strict=True):
            if loud:
                break
            self._quiet_chunks += 1
            self._quiet_samples += len(chunk) // _BYTES_PER_SAMPLE
        return segment
