"""Live-backend tests for the §5a AIPP module.

These hit a real OpenAI-compatible server (Ollama by default) and are
**skipped unless ``--aipp-live`` is passed**. They cover the parts of
the contract that the mocked unit tests can't:
    - Our request shape matches what a real OpenAI-compat server accepts.
    - Our response parsing handles real (non-canonical) JSON.
    - Fail-open works against a backend that genuinely rejected a request
      (not just one we mocked to fail).

Configuration (defaults match a fresh Ollama install):
    OLLAMA_BASE_URL — base URL ending in /v1 (default: http://localhost:11434/v1)
    OLLAMA_MODEL    — model to use (default: qwen2.5:0.5b — ~400 MB, CPU-fast)

A session-scoped fixture pings the backend up front. If it isn't
reachable the tests skip with a helpful message instead of producing
N timeout failures.

Local usage:
    ollama pull qwen2.5:0.5b
    uv run pytest --aipp-live -k aipp_live -v
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")


# ---------------------------------------------------------------------------
# Test setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reasoning as test_postprocess.py — fail-open paths fire notify-send."""
    monkeypatch.setattr("talkat.postprocess._notify", lambda _msg: None)


@pytest.fixture(scope="session")
def live_backend_url() -> str:
    """Pre-flight: ping the backend's /models endpoint once for the whole session.

    Skips immediately with a friendly message if the backend isn't up, so a
    forgotten ``ollama serve`` produces one skip per test rather than N
    confusing timeout failures.
    """
    probe_url = f"{OLLAMA_BASE_URL.rstrip('/')}/models"
    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.get(probe_url)
    except httpx.HTTPError as e:
        pytest.skip(
            f"Live AIPP backend not reachable at {OLLAMA_BASE_URL}: {e}. "
            "Start one with `ollama serve` (and `ollama pull qwen2.5:0.5b`)."
        )
    if r.status_code >= 500:
        pytest.skip(f"Backend at {OLLAMA_BASE_URL} returned {r.status_code}")
    return OLLAMA_BASE_URL


@pytest.fixture
def live_profile(live_backend_url: str) -> dict[str, Any]:
    """Build a default AIPP profile pointing at the live backend."""
    return {
        "base_url": live_backend_url,
        "model": OLLAMA_MODEL,
        "system_prompt": "Reply concisely. No commentary.",
        "timeout": 60,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.aipp_live
def test_live_round_trip_returns_non_empty(live_profile: dict[str, Any]) -> None:
    """Minimum bar: send text in, get non-empty text back.

    LLM outputs are stochastic — we don't gate on specific content. This
    proves request shape + response parsing match a real OpenAI-compatible
    server end-to-end.
    """
    from talkat.postprocess import postprocess_text

    config = {"postprocess_profiles": {"live": live_profile}}
    out = postprocess_text("hello world", "live", config=config)
    assert isinstance(out, str)
    assert out, f"expected non-empty response, got: {out!r}"


@pytest.mark.aipp_live
def test_live_cleanup_realistic_transcript(live_profile: dict[str, Any]) -> None:
    """Multi-sentence dictation-style input must round-trip without truncation.

    We don't assert on cleanup quality (depends on the model). We just check
    the request/response shape survives non-trivial input and that the
    response isn't an echo of our system prompt.
    """
    from talkat.postprocess import postprocess_text

    profile = {
        **live_profile,
        "system_prompt": (
            "Clean up grammar and punctuation. Keep the meaning identical. "
            "Return only the cleaned text."
        ),
    }
    config = {"postprocess_profiles": {"live": profile}}
    transcript = (
        "um so i was thinking about the project and like "
        "we should probably refactor the auth layer next quarter "
        "because the current setup is getting hard to test"
    )
    out = postprocess_text(transcript, "live", config=config)
    assert isinstance(out, str)
    assert out, "expected non-empty cleanup output"
    # Sanity: the model should respond to the user message, not echo our setup.
    assert "Return only the cleaned text" not in out


@pytest.mark.aipp_live
def test_live_bad_model_fails_open_to_raw_transcript(
    live_profile: dict[str, Any],
) -> None:
    """Real backend rejection (unknown model) must trip fail-open.

    Complements the mocked fail-open tests by proving the path works against
    a backend that genuinely returned an error (not one we constructed).
    """
    from talkat.postprocess import postprocess_text

    profile = {**live_profile, "model": "definitely-does-not-exist-zzz"}
    config = {"postprocess_profiles": {"live": profile}}

    out = postprocess_text("preserve me", "live", config=config)
    assert (
        out == "preserve me"
    ), "fail-open should return the raw transcript when the backend rejects the request"
