"""Per-run diagnostics — a small JSON blob written next to transcripts.

Each completed dictation or file-transcription run drops two files in
``$XDG_DATA_HOME/talkat/diagnostics/``:

  * ``diagnostics.latest.json`` — overwritten each run; the easy-to-find one
  * ``diagnostics_YYYYMMDD_HHMMSS.json`` — kept around for trending

The schema is intentionally a plain dict (not a dataclass with __slots__
or a pydantic model) so adding a field is a one-liner at the call site
and parsers downstream — `jq`, a follow-up `talkat diag` command, a
support ticket — see whatever was there at write time without a schema
migration. Borrowed from speech-note's ``diagnostics.latest.json``
convention.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .paths import DIAGNOSTICS_DIR

logger = get_logger(__name__)

# Retention cap for timestamped records. Every dictation writes one, so an
# active user produces dozens a day — without pruning the directory grows
# forever. 200 records ≈ several days of heavy use, plenty for trending.
MAX_TIMESTAMPED_RECORDS = 200


def _safe_rtf(audio_duration: float, asr_seconds: float) -> float | None:
    """Realtime factor = wall-clock ASR time ÷ audio duration; None if undefined."""
    if audio_duration <= 0:
        return None
    return asr_seconds / audio_duration


def build_record(
    *,
    mode: str,
    audio_duration: float,
    asr_seconds: float,
    applied_gain_db: float,
    model_type: str | None,
    model_name: str | None,
    transcript_chars: int,
    transcript_words: int,
    postprocess_profile: str | None = None,
    errors: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a diagnostics record. Pure — no I/O — for testability."""
    record: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "audio_duration_seconds": round(audio_duration, 3),
        "asr_seconds": round(asr_seconds, 3),
        "realtime_factor": _safe_rtf(audio_duration, asr_seconds),
        "applied_gain_db": round(applied_gain_db, 2),
        "model_type": model_type,
        "model_name": model_name,
        "transcript_chars": transcript_chars,
        "transcript_words": transcript_words,
        "postprocess_profile": postprocess_profile,
        "errors": list(errors) if errors else [],
    }
    if extra:
        record["extra"] = extra
    return record


def write_record(record: dict[str, Any]) -> Path | None:
    """Write the record to ``diagnostics.latest.json`` and a timestamped copy.

    Returns the path of the timestamped file on success, ``None`` on
    failure. Diagnostics are advisory — a write failure must not break
    the user's dictation.
    """
    try:
        DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"Could not create diagnostics dir: {e}")
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped = DIAGNOSTICS_DIR / f"diagnostics_{stamp}.json"
    latest = DIAGNOSTICS_DIR / "diagnostics.latest.json"

    payload = json.dumps(record, indent=2, sort_keys=True)
    try:
        timestamped.write_text(payload, encoding="utf-8")
        latest.write_text(payload, encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not write diagnostics: {e}")
        return None

    _prune_old_records()
    return timestamped


def _prune_old_records() -> None:
    """Delete the oldest timestamped records beyond the retention cap.

    Filename timestamps sort chronologically, so a name sort is an age sort.
    Best-effort like everything else here.
    """
    try:
        records = sorted(DIAGNOSTICS_DIR.glob("diagnostics_*.json"))
        excess = len(records) - MAX_TIMESTAMPED_RECORDS
        for stale in records[:excess] if excess > 0 else []:
            stale.unlink(missing_ok=True)
    except OSError as e:
        logger.debug(f"Diagnostics pruning skipped: {e}")
