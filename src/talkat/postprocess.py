"""AI post-processing (AIPP) — pipe transcribed text through an LLM.

Transport: OpenAI-compatible ``/v1/chat/completions`` only. This single
endpoint shape transparently covers Ollama (``http://localhost:11434/v1``),
llama.cpp server, LM Studio, OpenRouter, vLLM, and OpenAI itself — Ollama
has shipped OpenAI compatibility since early 2024, so we don't need a
provider abstraction or a second transport for v1.0.0.

Design:
    - Profiles live in ``config["postprocess_profiles"]`` (see
      ``security.validate_postprocess_profile`` for the schema).
    - API keys are referenced via environment variable names
      (``api_key_env``), never stored in the config file directly.
    - All failures are fail-open: log + notify + return the raw transcript
      unchanged. AIPP must never lose the user's dictation.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import httpx

from .logging_config import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0


def _notify(message: str) -> None:
    """Send a desktop notification; ignore failure (notify-send may be missing)."""
    # Local import to avoid a hard dep cycle (security → subprocess → logging).
    from .security import safe_subprocess_run

    with contextlib.suppress(FileNotFoundError):
        safe_subprocess_run(["notify-send", "Talkat", message], check=False)


def postprocess_text(
    text: str,
    profile_name: str,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    """Send ``text`` through the named AIPP profile; return the cleaned text.

    Fail-open contract: on profile-not-found, network error, HTTP error,
    timeout, or malformed response, log the issue, emit a notification, and
    return the original ``text`` unchanged. Callers can therefore call this
    unconditionally without guarding against AIPP being misconfigured.

    Empty input short-circuits (returns ``""``) so we don't waste a round
    trip on a no-speech result.

    Args:
        text: The transcribed text to post-process.
        profile_name: Name of a profile in ``config["postprocess_profiles"]``.
        config: Pre-loaded config dict (avoids re-reading the file for callers
            that already merged overrides). Defaults to ``load_app_config()``.

    Returns:
        Post-processed text, or ``text`` unchanged if anything went wrong.
    """
    if not text:
        return text

    if config is None:
        from .config import load_app_config

        config = load_app_config()

    profiles = config.get("postprocess_profiles", {}) or {}
    profile = profiles.get(profile_name)
    if profile is None:
        available = sorted(profiles)
        logger.error(
            f"Postprocess profile {profile_name!r} not found. "
            f"Available: {available or '(none configured)'}. "
            "Typing raw transcript."
        )
        _notify(f"AIPP profile {profile_name!r} not found — using raw transcript.")
        return text

    try:
        return _call_openai_compat(text, profile)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error(f"AIPP profile {profile_name!r} failed: {e}. Using raw transcript.")
        _notify(f"AIPP {profile_name!r} failed — using raw transcript.")
        return text


def _call_openai_compat(text: str, profile: dict[str, Any]) -> str:
    """Single ``/v1/chat/completions`` POST. Raises on any failure path.

    Caller (``postprocess_text``) catches and falls back to the raw transcript.
    Splitting this out keeps the exception-funnel obvious — anything raised
    here is treated as "AIPP didn't run" and triggers fail-open.
    """
    base_url = str(profile["base_url"]).rstrip("/")
    model = str(profile["model"])
    system_prompt = str(profile["system_prompt"])
    timeout = float(profile.get("timeout", _DEFAULT_TIMEOUT))

    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key_env = profile.get("api_key_env")
    if api_key_env:
        api_key = os.environ.get(str(api_key_env))
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            # Not fatal — some local servers ignore the header entirely. Log
            # so the user notices if their hosted endpoint then 401s.
            logger.warning(
                f"AIPP api_key_env {api_key_env!r} is unset; sending request without Authorization."
            )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }

    url = f"{base_url}/chat/completions"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"unexpected response shape: {data!r}") from e
    if not isinstance(content, str):
        raise ValueError(f"response content is not a string: {type(content).__name__}")
    return content.strip()
