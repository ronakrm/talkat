"""Tests for the §5a AIPP (AI Post-Processing) module.

postprocess_text is the only public function. Its contract:
    - Empty input → empty output (no network).
    - Missing/unknown profile → return raw text + log + notify (fail-open).
    - Any LLM failure (network / HTTP / malformed response) → fail-open.
    - Happy path → return the LLM's choices[0].message.content (stripped).

The fail-open contract is the most important part — AIPP must never lose
the user's dictation. Each failure path gets a dedicated test.

Implementation tests use a FakeClient that captures requests + returns
canned responses. One integration test spins a real Flask+waitress
server on a random TCP port to verify the end-to-end shape.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from flask import Flask, jsonify, request
from waitress.server import create_server

# ---------------------------------------------------------------------------
# FakeClient — drop-in replacement for httpx.Client for unit tests
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Subset of httpx.Response sufficient for postprocess._call_openai_compat."""

    def __init__(self, status_code: int, json_data: Any) -> None:
        self.status_code = status_code
        self._json = json_data
        # Real httpx response has a non-null request; the only place we use
        # this is in HTTPStatusError construction, which accepts None.
        self.request: Any = None

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # Mirror httpx's real error; postprocess catches httpx.HTTPError.
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )


class _FakeClient:
    """Records every call + returns a programmed response or raises."""

    def __init__(
        self,
        response: _FakeResponse | Exception | None = None,
        timeout: float | None = None,
    ) -> None:
        self.response = response
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_a: object) -> None:
        pass

    def post(
        self, url: str, json: Any = None, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if isinstance(self.response, Exception):
            raise self.response
        assert self.response is not None, "test forgot to set FakeClient.response"
        return self.response


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse | Exception | None = None,
) -> _FakeClient:
    """Replace ``postprocess.httpx.Client`` with one that yields ``response``.

    Returns the FakeClient instance so the test can read ``.calls``.
    """
    from talkat import postprocess

    fake = _FakeClient(response=response)

    def factory(**kwargs: Any) -> _FakeClient:
        fake.timeout = kwargs.get("timeout")
        return fake

    monkeypatch.setattr(postprocess.httpx, "Client", factory)
    return fake


def _profile(**overrides: Any) -> dict[str, Any]:
    base = {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.2:3b",
        "system_prompt": "Clean up grammar and punctuation. Return only the cleaned text.",
    }
    base.update(overrides)
    return base


def _ok_response(content: str = "processed text") -> _FakeResponse:
    return _FakeResponse(
        status_code=200,
        json_data={"choices": [{"message": {"content": content}}]},
    )


# ---------------------------------------------------------------------------
# Empty-input short circuit
# ---------------------------------------------------------------------------


def test_empty_text_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """No round trip on empty input — we'd just be wasting tokens."""
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch)
    out = postprocess_text("", "any-profile", config={"postprocess_profiles": {}})
    assert out == ""
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Missing / unknown profile → fail-open
# ---------------------------------------------------------------------------


def test_unknown_profile_returns_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch)
    out = postprocess_text("hello world", "nonexistent", config={"postprocess_profiles": {}})
    assert out == "hello world"
    assert fake.calls == []


def test_no_profiles_at_all_returns_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile dict missing entirely (older configs)."""
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch)
    out = postprocess_text("hello", "tidy", config={})
    assert out == "hello"
    assert fake.calls == []


def test_postprocess_profiles_none_treated_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch)
    out = postprocess_text("hello", "tidy", config={"postprocess_profiles": None})
    assert out == "hello"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_llm_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response("Hello, world."))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    out = postprocess_text("hello world", "tidy", config=config)
    assert out == "Hello, world."
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "http://localhost:11434/v1/chat/completions"
    assert call["json"]["model"] == "llama3.2:3b"
    assert call["json"]["stream"] is False
    assert call["json"]["messages"][0]["role"] == "system"
    assert call["json"]["messages"][1]["role"] == "user"
    assert call["json"]["messages"][1]["content"] == "hello world"


def test_happy_path_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMs frequently pad with trailing newlines; we strip so ydotool gets clean text."""
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch, _ok_response("  padded  \n"))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("x", "tidy", config=config) == "padded"


def test_base_url_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response())
    config = {
        "postprocess_profiles": {"tidy": _profile(base_url="http://localhost:11434/v1/")},
    }

    postprocess_text("hi", "tidy", config=config)
    assert fake.calls[0]["url"] == "http://localhost:11434/v1/chat/completions"


def test_custom_timeout_passed_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response())
    config = {"postprocess_profiles": {"tidy": _profile(timeout=7.5)}}

    postprocess_text("hi", "tidy", config=config)
    assert fake.timeout == 7.5


def test_default_timeout_when_unspecified(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import _DEFAULT_TIMEOUT, postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response())
    config = {"postprocess_profiles": {"tidy": _profile()}}

    postprocess_text("hi", "tidy", config=config)
    assert fake.timeout == _DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------


def test_http_500_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch, _FakeResponse(500, {"error": "boom"}))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("hello", "tidy", config=config) == "hello"


def test_connect_error_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch, httpx.ConnectError("refused"))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("hello", "tidy", config=config) == "hello"


def test_timeout_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch, httpx.TimeoutException("timed out"))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("hello", "tidy", config=config) == "hello"


def test_missing_choices_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server returned 200 but the shape isn't OpenAI-compatible."""
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch, _FakeResponse(200, {"unexpected": "shape"}))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("hello", "tidy", config=config) == "hello"


def test_empty_choices_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat.postprocess import postprocess_text

    _install_fake_client(monkeypatch, _FakeResponse(200, {"choices": []}))
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("hello", "tidy", config=config) == "hello"


def test_non_string_content_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some servers return content as a list of parts; we don't unpack that yet."""
    from talkat.postprocess import postprocess_text

    _install_fake_client(
        monkeypatch,
        _FakeResponse(200, {"choices": [{"message": {"content": [{"text": "x"}]}}]}),
    )
    config = {"postprocess_profiles": {"tidy": _profile()}}

    assert postprocess_text("hello", "tidy", config=config) == "hello"


# ---------------------------------------------------------------------------
# API-key handling — never store the key, look up by env-var name
# ---------------------------------------------------------------------------


def test_authorization_set_when_api_key_env_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response())
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-xxx")
    config = {
        "postprocess_profiles": {
            "openai": _profile(
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
                api_key_env="OPENAI_API_KEY_TEST",
            )
        }
    }

    postprocess_text("hi", "openai", config=config)
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-xxx"


def test_authorization_omitted_when_env_var_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var unset → request goes out without Authorization (warn-only)."""
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response())
    monkeypatch.delenv("OPENAI_API_KEY_TEST", raising=False)
    config = {
        "postprocess_profiles": {
            "openai": _profile(api_key_env="OPENAI_API_KEY_TEST"),
        }
    }

    postprocess_text("hi", "openai", config=config)
    assert "Authorization" not in fake.calls[0]["headers"]


def test_authorization_omitted_when_api_key_env_not_in_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-only profile (Ollama, llama.cpp): no Authorization, no warning."""
    from talkat.postprocess import postprocess_text

    fake = _install_fake_client(monkeypatch, _ok_response())
    config = {"postprocess_profiles": {"local": _profile()}}

    postprocess_text("hi", "local", config=config)
    assert "Authorization" not in fake.calls[0]["headers"]


# ---------------------------------------------------------------------------
# Config plumbing — explicit config arg wins over load_app_config
# ---------------------------------------------------------------------------


def test_explicit_config_arg_skips_load_app_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When config is passed in, we don't re-read the file."""
    from talkat import postprocess

    _install_fake_client(monkeypatch, _ok_response())

    def boom() -> dict[str, Any]:
        raise AssertionError("load_app_config should not be called when config is provided")

    monkeypatch.setattr("talkat.config.load_app_config", boom)

    config = {"postprocess_profiles": {"tidy": _profile()}}
    postprocess.postprocess_text("hi", "tidy", config=config)


def test_no_config_arg_loads_app_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat import postprocess

    _install_fake_client(monkeypatch, _ok_response("done"))
    called: list[bool] = []

    def fake_load() -> dict[str, Any]:
        called.append(True)
        return {"postprocess_profiles": {"tidy": _profile()}}

    monkeypatch.setattr("talkat.config.load_app_config", fake_load)
    out = postprocess.postprocess_text("hi", "tidy")
    assert out == "done"
    assert called == [True]


# ---------------------------------------------------------------------------
# Integration: real Flask server speaking OpenAI-compat
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait_for_port(port: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"Server did not bind 127.0.0.1:{port} within {timeout}s")


@pytest.fixture
def aipp_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Fake OpenAI-compat server. Returns (base_url, captured_requests)."""
    captured: list[dict[str, Any]] = []
    app = Flask(__name__)

    @app.route("/v1/chat/completions", methods=["POST"])
    def chat() -> Any:
        captured.append(
            {
                "headers": dict(request.headers),
                "json": request.get_json(),
            }
        )
        body = request.get_json()
        user_msg = body["messages"][-1]["content"]
        return jsonify(
            {"choices": [{"message": {"role": "assistant", "content": f"clean({user_msg})"}}]}
        )

    port = _free_port()
    server = create_server(app, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}/v1", captured
    finally:
        server.close()
        thread.join(timeout=2)


def test_integration_real_server_round_trip(
    aipp_server: tuple[str, list[dict[str, Any]]],
) -> None:
    from talkat.postprocess import postprocess_text

    base_url, captured = aipp_server
    config = {
        "postprocess_profiles": {
            "tidy": {
                "base_url": base_url,
                "model": "fake-model",
                "system_prompt": "Tidy this up.",
                "timeout": 5,
            }
        }
    }

    out = postprocess_text("um hello", "tidy", config=config)
    assert out == "clean(um hello)"
    assert len(captured) == 1
    assert captured[0]["json"]["model"] == "fake-model"
    assert captured[0]["json"]["messages"][0]["content"] == "Tidy this up."
    assert captured[0]["json"]["messages"][1]["content"] == "um hello"
