"""Tests for talkat.security — sanitize / validate / command-safety helpers."""

import subprocess as _subprocess_module
from unittest.mock import MagicMock

import pytest

from talkat.security import (
    SecurityError,
    safe_subprocess_run,
    sanitize_text_for_clipboard,
    sanitize_text_for_typing,
    validate_audio_params,
    validate_command,
    validate_file_path,
    validate_json_config,
    validate_model_name,
    validate_port,
)

# ---------------------------------------------------------------------------
# sanitize_text_for_typing
# ---------------------------------------------------------------------------


def test_sanitize_typing_preserves_apostrophes():
    """Regression test for bug #3: apostrophes must NOT get backslash-escaped."""
    assert sanitize_text_for_typing("what's up") == "what's up"


def test_sanitize_typing_strips_null_byte():
    """Null bytes are control characters and must be stripped."""
    assert sanitize_text_for_typing("hello\x00world") == "helloworld"


def test_sanitize_typing_does_not_shell_escape():
    """Shell metacharacters should pass through unchanged (no shell escaping)."""
    assert sanitize_text_for_typing("a$b`c") == "a$b`c"


def test_sanitize_typing_empty_string():
    assert sanitize_text_for_typing("") == ""


def test_sanitize_typing_keeps_newline_and_tab():
    """Newlines and tabs are explicitly preserved."""
    assert sanitize_text_for_typing("a\nb\tc") == "a\nb\tc"


# ---------------------------------------------------------------------------
# validate_json_config
# ---------------------------------------------------------------------------


def test_validate_empty_config_ok():
    assert validate_json_config({}) == {}


def test_validate_model_type_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"model_type": "gpt-4"})


def test_validate_model_type_faster_whisper_ok():
    cfg = validate_json_config({"model_type": "faster-whisper"})
    assert cfg["model_type"] == "faster-whisper"


def test_validate_model_type_vosk_ok():
    cfg = validate_json_config({"model_type": "vosk"})
    assert cfg["model_type"] == "vosk"


def test_validate_distil_whisper_rejected():
    """distil-whisper is no longer a valid model_type (was never implemented)."""
    with pytest.raises(ValueError):
        validate_json_config({"model_type": "distil-whisper"})


def test_validate_clipboard_on_long_must_be_bool():
    with pytest.raises(ValueError):
        validate_json_config({"clipboard_on_long": "yes"})


def test_validate_negative_http_timeout_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"http_timeout": -5})


def test_validate_http_timeout_ok():
    cfg = validate_json_config({"http_timeout": 60})
    assert cfg["http_timeout"] == 60


def test_validate_silence_threshold_out_of_range_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"silence_threshold": 100000})


# ---------------------------------------------------------------------------
# validate_port
# ---------------------------------------------------------------------------


def test_validate_port_accepts_valid_range():
    assert validate_port(1) == 1
    assert validate_port(8080) == 8080
    assert validate_port(65535) == 65535
    # Strings are coerced.
    assert validate_port("443") == 443


def test_validate_port_rejects_out_of_range():
    with pytest.raises(ValueError):
        validate_port(0)
    with pytest.raises(ValueError):
        validate_port(65536)


def test_validate_port_rejects_non_numeric():
    with pytest.raises(ValueError):
        validate_port("not-a-port")


# ---------------------------------------------------------------------------
# validate_file_path
# ---------------------------------------------------------------------------


def test_validate_file_path_returns_resolved_path(tmp_path):
    target = tmp_path / "file.wav"
    target.write_text("ok")
    result = validate_file_path(str(target), must_exist=True)
    assert result == target.resolve()


def test_validate_file_path_raises_when_must_exist_and_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_file_path(str(tmp_path / "missing.wav"), must_exist=True)


def test_validate_file_path_blocks_path_traversal():
    with pytest.raises(SecurityError):
        validate_file_path("../etc/passwd")


def test_validate_file_path_blocks_symlinks_by_default(tmp_path):
    """Symlinks must be rejected by default.

    Regression guard: before the §architecture fix, .resolve() ran before
    .is_symlink(), so the symlink check was always against the resolved
    target — never the symlink itself — making the block dead code.
    """
    target = tmp_path / "real_file.wav"
    target.write_text("real content")
    link = tmp_path / "link.wav"
    link.symlink_to(target)

    with pytest.raises(SecurityError):
        validate_file_path(str(link))


def test_validate_file_path_allows_symlinks_when_opted_in(tmp_path):
    """allow_symlinks=True must let symlinks through (resolved to the target)."""
    target = tmp_path / "real_file.wav"
    target.write_text("real content")
    link = tmp_path / "link.wav"
    link.symlink_to(target)

    resolved = validate_file_path(str(link), allow_symlinks=True)
    # The result is resolved to the real target, not the symlink.
    assert resolved == target.resolve()


# ---------------------------------------------------------------------------
# validate_model_name
# ---------------------------------------------------------------------------


def test_validate_model_name_accepts_typical_whisper_names():
    assert validate_model_name("base.en") == "base.en"
    assert validate_model_name("small.en-v3") == "small.en-v3"
    assert validate_model_name("models/distil-whisper-small") == ("models/distil-whisper-small")


def test_validate_model_name_rejects_metacharacters():
    with pytest.raises(ValueError):
        validate_model_name("base; rm -rf /")
    with pytest.raises(ValueError):
        validate_model_name("base$(curl evil)")


def test_validate_model_name_blocks_path_traversal():
    with pytest.raises(ValueError):
        validate_model_name("../../etc/passwd")


def test_validate_model_name_rejects_overlong():
    with pytest.raises(ValueError):
        validate_model_name("a" * 257)


# ---------------------------------------------------------------------------
# validate_command
# ---------------------------------------------------------------------------


def test_validate_command_rejects_empty():
    with pytest.raises(ValueError):
        validate_command([])


def test_validate_command_rejects_shell_metacharacters():
    with pytest.raises(SecurityError):
        validate_command(["ydotool", "type", "evil; rm -rf /"])
    with pytest.raises(SecurityError):
        validate_command(["ydotool", "type", "$(curl evil)"])


def test_validate_command_passes_whitelisted_command():
    cmd = ["ydotool", "type", "hello world"]
    assert validate_command(cmd) == cmd


def test_validate_command_logs_but_passes_non_whitelisted(caplog):
    """Non-whitelisted commands log a warning but are still returned."""
    import logging

    with caplog.at_level(logging.WARNING):
        cmd = ["unusual-command", "--arg"]
        assert validate_command(cmd) == cmd
    assert any("non-whitelisted" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# sanitize_text_for_clipboard
# ---------------------------------------------------------------------------


def test_sanitize_clipboard_strips_null_byte():
    assert "\x00" not in sanitize_text_for_clipboard("hello\x00world")


def test_sanitize_clipboard_empty_returns_empty():
    assert sanitize_text_for_clipboard("") == ""


def test_sanitize_clipboard_normalizes_whitespace():
    # Multiple spaces collapse, tabs become a single space.
    result = sanitize_text_for_clipboard("a   b\t\tc")
    assert result == "a b c"


def test_sanitize_clipboard_normalizes_newlines():
    # \r\n and \n\n collapse to a single \n.
    result = sanitize_text_for_clipboard("line1\r\n\r\nline2")
    assert result == "line1\nline2"


def test_sanitize_clipboard_truncates_to_max_length():
    overlong = "a" * 200
    result = sanitize_text_for_clipboard(overlong, max_length=50)
    assert len(result) <= 50


# ---------------------------------------------------------------------------
# validate_audio_params
# ---------------------------------------------------------------------------


def test_validate_audio_params_accepts_typical_voice_setup():
    assert validate_audio_params(16000, 1, 1024) == (16000, 1, 1024)
    assert validate_audio_params(44100, 2, 4096) == (44100, 2, 4096)


def test_validate_audio_params_rejects_unsupported_sample_rate():
    with pytest.raises(ValueError):
        validate_audio_params(12345)


def test_validate_audio_params_rejects_invalid_channel_count():
    with pytest.raises(ValueError):
        validate_audio_params(16000, channels=3)
    with pytest.raises(ValueError):
        validate_audio_params(16000, channels=0)


def test_validate_audio_params_rejects_non_power_of_two_chunk():
    with pytest.raises(ValueError):
        validate_audio_params(16000, 1, 1000)


def test_validate_audio_params_rejects_chunk_out_of_range():
    with pytest.raises(ValueError):
        validate_audio_params(16000, 1, 128)  # below min 256
    with pytest.raises(ValueError):
        validate_audio_params(16000, 1, 16384)  # above max 8192


# ---------------------------------------------------------------------------
# safe_subprocess_run
# ---------------------------------------------------------------------------
#
# safe_subprocess_run is the single subprocess gateway used by every
# ydotool / wl-copy / xclip / notify-send call. A bug here silently
# affects clipboard, typing, and notifications. These tests pin down:
#
#   - the 30s default timeout (so callers don't hang forever)
#   - timeout=None pass-through (typing a long transcript opts in)
#   - shell=False is non-negotiable (no shell injection from sanitize-bypass)
#   - kwargs allowlist filters unknown/dangerous kwargs (env, cwd, …)
#   - validate_command runs BEFORE subprocess.run (defence in depth)
#   - TimeoutExpired and FileNotFoundError propagate (callers catch them)


def _patch_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch subprocess.run to a recording mock and return the mock."""
    fake = MagicMock(name="subprocess.run")
    fake.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(_subprocess_module, "run", fake)
    return fake


def test_safe_subprocess_run_default_timeout_is_30s(monkeypatch: pytest.MonkeyPatch):
    """When no timeout is passed, safe_subprocess_run must default to 30s."""
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(["ydotool", "type", "hi"])
    args, kwargs = fake.call_args
    assert args[0] == ["ydotool", "type", "hi"]
    assert kwargs["timeout"] == 30


def test_safe_subprocess_run_explicit_timeout_passed_through(monkeypatch: pytest.MonkeyPatch):
    """A custom integer timeout is forwarded verbatim."""
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(["ydotool", "type", "hi"], timeout=5)
    _args, kwargs = fake.call_args
    assert kwargs["timeout"] == 5


def test_safe_subprocess_run_explicit_None_timeout_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """Passing timeout=None must disable the default — required by typing-long-text.

    ``listen_once`` passes ``timeout=None`` for ``ydotool type --key-delay=1``
    because a long transcript can take well over 30s. If the default kicked in
    here, dictation of long passages would silently get truncated.
    """
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(["ydotool", "type", "very long text"], timeout=None)
    _args, kwargs = fake.call_args
    assert kwargs["timeout"] is None


def test_safe_subprocess_run_shell_kwarg_is_forced_off(monkeypatch: pytest.MonkeyPatch):
    """No caller-supplied shell=True can reach subprocess.run."""
    fake = _patch_subprocess_run(monkeypatch)
    # Try to sneak shell=True past the filter — the function must override it.
    safe_subprocess_run(["ydotool", "type", "hi"], shell=True)  # type: ignore[call-arg]
    _args, kwargs = fake.call_args
    assert kwargs["shell"] is False


def test_safe_subprocess_run_drops_disallowed_kwargs(monkeypatch: pytest.MonkeyPatch):
    """env, cwd, etc. are NOT in the allowlist and must be filtered out."""
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(
        ["ydotool", "type", "hi"],
        env={"HACK": "1"},  # type: ignore[call-arg]
        cwd="/etc",  # type: ignore[call-arg]
    )
    _args, kwargs = fake.call_args
    assert "env" not in kwargs
    assert "cwd" not in kwargs


def test_safe_subprocess_run_forwards_allowed_kwargs(monkeypatch: pytest.MonkeyPatch):
    """input, capture_output, text, encoding, stdout, stderr are in the allowlist."""
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(
        ["ydotool", "type", "hi"],
        input=b"stdin data",
        capture_output=True,
        text=False,
        encoding="utf-8",
    )
    _args, kwargs = fake.call_args
    assert kwargs["input"] == b"stdin data"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is False
    assert kwargs["encoding"] == "utf-8"


def test_safe_subprocess_run_check_defaults_to_false(monkeypatch: pytest.MonkeyPatch):
    """check defaults to False — callers that want raise-on-nonzero must opt in.

    A naive default of check=True would change semantics for every caller
    that currently relies on inspecting returncode.
    """
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(["ydotool", "type", "hi"])
    _args, kwargs = fake.call_args
    assert kwargs["check"] is False


def test_safe_subprocess_run_check_can_be_overridden(monkeypatch: pytest.MonkeyPatch):
    """Callers (e.g. copy_to_clipboard) opt in to check=True for raise-on-nonzero."""
    fake = _patch_subprocess_run(monkeypatch)
    safe_subprocess_run(["ydotool", "type", "hi"], check=True)
    _args, kwargs = fake.call_args
    assert kwargs["check"] is True


def test_safe_subprocess_run_rejects_shell_metacharacters_before_running(
    monkeypatch: pytest.MonkeyPatch,
):
    """validate_command must run before subprocess.run — dangerous chars never spawn.

    If a metacharacter slips into the args, validate_command raises
    SecurityError and subprocess.run is never invoked. This is the defence-
    in-depth check: even if sanitize_text_for_typing missed something,
    safe_subprocess_run won't execute it.
    """
    fake = _patch_subprocess_run(monkeypatch)
    with pytest.raises(SecurityError):
        safe_subprocess_run(["ydotool", "type", "evil; rm -rf /"])
    fake.assert_not_called()


def test_safe_subprocess_run_reraises_TimeoutExpired(monkeypatch: pytest.MonkeyPatch):
    """A subprocess timeout must surface as TimeoutExpired (not swallowed)."""
    fake = MagicMock(side_effect=_subprocess_module.TimeoutExpired(cmd=["ydotool"], timeout=1))
    monkeypatch.setattr(_subprocess_module, "run", fake)

    with pytest.raises(_subprocess_module.TimeoutExpired):
        safe_subprocess_run(["ydotool", "type", "hi"], timeout=1)


def test_safe_subprocess_run_reraises_FileNotFoundError(monkeypatch: pytest.MonkeyPatch):
    """If the binary doesn't exist, FileNotFoundError must propagate.

    copy_to_clipboard relies on this to fall back from wl-copy to xclip.
    """
    fake = MagicMock(side_effect=FileNotFoundError(2, "No such file"))
    monkeypatch.setattr(_subprocess_module, "run", fake)

    with pytest.raises(FileNotFoundError):
        safe_subprocess_run(["nonexistent-binary"])


def test_safe_subprocess_run_returns_completed_process_verbatim(
    monkeypatch: pytest.MonkeyPatch,
):
    """Successful path returns subprocess.run's return value unchanged."""
    expected = MagicMock(returncode=0, stdout=b"hello", stderr=b"")
    fake = MagicMock(return_value=expected)
    monkeypatch.setattr(_subprocess_module, "run", fake)

    result = safe_subprocess_run(["ydotool", "type", "hi"])
    assert result is expected


# ---------------------------------------------------------------------------
# validate_postprocess_profile (§5a)
# ---------------------------------------------------------------------------


def _minimal_profile():
    return {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.2:3b",
        "system_prompt": "Tidy this up.",
    }


def test_validate_postprocess_profile_accepts_minimal():
    from talkat.security import validate_postprocess_profile

    out = validate_postprocess_profile("tidy", _minimal_profile())
    assert out["base_url"] == "http://localhost:11434/v1"


def test_validate_postprocess_profile_accepts_https_and_optional_keys():
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["base_url"] = "https://api.openai.com/v1"
    p["api_key_env"] = "OPENAI_API_KEY"
    p["timeout"] = 45.0
    out = validate_postprocess_profile("openai", p)
    assert out["api_key_env"] == "OPENAI_API_KEY"
    assert out["timeout"] == 45.0


def test_validate_postprocess_profile_strips_trailing_slash_in_base_url():
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["base_url"] = "http://localhost:11434/v1/"
    out = validate_postprocess_profile("tidy", p)
    assert out["base_url"] == "http://localhost:11434/v1"


def test_validate_postprocess_profile_rejects_non_dict():
    from talkat.security import validate_postprocess_profile

    with pytest.raises(ValueError, match="must be an object"):
        validate_postprocess_profile("tidy", "not a dict")  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", ["base_url", "model", "system_prompt"])
def test_validate_postprocess_profile_rejects_missing_required(missing: str):
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    del p[missing]
    with pytest.raises(ValueError, match=f"missing required key {missing!r}"):
        validate_postprocess_profile("tidy", p)


@pytest.mark.parametrize("empty_field", ["base_url", "model", "system_prompt"])
def test_validate_postprocess_profile_rejects_empty_required(empty_field: str):
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p[empty_field] = ""
    with pytest.raises(ValueError, match="non-empty string"):
        validate_postprocess_profile("tidy", p)


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "localhost:11434/v1",  # missing scheme
        "/relative/path",
    ],
)
def test_validate_postprocess_profile_rejects_non_http_scheme(bad_url: str):
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["base_url"] = bad_url
    with pytest.raises(ValueError, match="http:// or https://"):
        validate_postprocess_profile("tidy", p)


def test_validate_postprocess_profile_rejects_oversize_model():
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["model"] = "x" * 300
    with pytest.raises(ValueError, match="model too long"):
        validate_postprocess_profile("tidy", p)


def test_validate_postprocess_profile_rejects_unknown_keys():
    """Typos like 'system_promt' should fail loudly, not silently no-op."""
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["system_promt"] = "x"  # intentional typo
    with pytest.raises(ValueError, match="unknown keys"):
        validate_postprocess_profile("tidy", p)


@pytest.mark.parametrize(
    "bad_env",
    ["1OPENAI", "OPENAI-KEY", "with space", "x;y", ""],
)
def test_validate_postprocess_profile_rejects_invalid_env_name(bad_env: str):
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["api_key_env"] = bad_env
    with pytest.raises(ValueError):
        validate_postprocess_profile("tidy", p)


def test_validate_postprocess_profile_api_key_env_can_be_null():
    """Explicit null is the same as the key being absent."""
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["api_key_env"] = None
    out = validate_postprocess_profile("tidy", p)
    assert out["api_key_env"] is None


@pytest.mark.parametrize("bad_timeout", [-1, 0, 601, "30", True])
def test_validate_postprocess_profile_rejects_bad_timeout(bad_timeout: object):
    from talkat.security import validate_postprocess_profile

    p = _minimal_profile()
    p["timeout"] = bad_timeout
    with pytest.raises(ValueError):
        validate_postprocess_profile("tidy", p)


# Integration with validate_json_config


def test_validate_json_config_validates_postprocess_profiles_dict():
    from talkat.security import validate_json_config

    out = validate_json_config(
        {
            "postprocess_profiles": {
                "tidy": _minimal_profile(),
            }
        }
    )
    assert "tidy" in out["postprocess_profiles"]


def test_validate_json_config_rejects_non_dict_profiles_field():
    from talkat.security import validate_json_config

    with pytest.raises(ValueError, match="must be an object"):
        validate_json_config({"postprocess_profiles": ["not", "a", "dict"]})


def test_validate_json_config_rejects_profile_with_bad_url():
    from talkat.security import validate_json_config

    p = _minimal_profile()
    p["base_url"] = "ftp://nope"
    with pytest.raises(ValueError):
        validate_json_config({"postprocess_profiles": {"bad": p}})


def test_code_defaults_includes_empty_postprocess_profiles():
    from talkat.config import CODE_DEFAULTS

    assert CODE_DEFAULTS["postprocess_profiles"] == {}
