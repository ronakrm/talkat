"""Tests for talkat.security — sanitize_text_for_typing and validate_json_config."""

import pytest

from talkat.security import sanitize_text_for_typing, validate_json_config

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


def test_validate_server_port_ok():
    cfg = validate_json_config({"server_port": 5555})
    assert cfg["server_port"] == 5555


def test_validate_server_port_too_high_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"server_port": 70000})


def test_validate_server_port_zero_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"server_port": 0})


def test_validate_model_type_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"model_type": "gpt-4"})


def test_validate_clipboard_on_long_must_be_bool():
    with pytest.raises(ValueError):
        validate_json_config({"clipboard_on_long": "yes"})


def test_validate_negative_http_timeout_rejected():
    with pytest.raises(ValueError):
        validate_json_config({"http_timeout": -5})


def test_validate_http_timeout_ok():
    cfg = validate_json_config({"http_timeout": 60})
    assert cfg["http_timeout"] == 60
