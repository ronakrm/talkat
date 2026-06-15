"""Tests for talkat.config — load/save round-trip and error handling."""

import json
from pathlib import Path

import pytest

from talkat.config import CODE_DEFAULTS, load_app_config, save_app_config


def test_load_returns_defaults_when_no_file(clean_config_file):
    """With no config file on disk, load_app_config returns the defaults."""
    assert not clean_config_file.exists()
    cfg = load_app_config()

    for key, value in CODE_DEFAULTS.items():
        assert key in cfg
        assert cfg[key] == value


def test_save_load_round_trip(clean_config_file):
    """Values saved via save_app_config are returned by load_app_config."""
    to_save = {
        "model_type": "vosk",
        "clipboard_on_long": False,
        "save_transcripts": False,
        "http_timeout": 60,
        "silence_duration": 2.5,
    }
    save_app_config(to_save)

    assert clean_config_file.exists()
    loaded = load_app_config()

    for key, value in to_save.items():
        assert loaded[key] == value

    # Defaults for keys we didn't override are still present.
    assert loaded["server_socket"] == CODE_DEFAULTS["server_socket"]
    assert loaded["model_name"] == CODE_DEFAULTS["model_name"]


def test_load_returns_defaults_on_malformed_json(clean_config_file):
    """Malformed JSON should be logged and defaults returned, not raised."""
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.write_text("{ this is not valid json")

    cfg = load_app_config()

    assert cfg["server_socket"] == CODE_DEFAULTS["server_socket"]
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]


def test_load_returns_defaults_on_invalid_value(clean_config_file):
    """Invalid value in config file → defaults returned, no exception."""
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.write_text(json.dumps({"http_timeout": -5}))

    cfg = load_app_config()

    # Invalid file was rejected; defaults are used.
    assert cfg["http_timeout"] == CODE_DEFAULTS["http_timeout"]


# ---------------------------------------------------------------------------
# Layered config merge: CODE_DEFAULTS → /etc → ~/.config
# ---------------------------------------------------------------------------


@pytest.fixture
def system_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect SYSTEM_CONFIG_FILE into a tmp_path for the test.

    The real path is /etc/talkat/config.json which we can't write to in
    tests. Patching the module-level constant lets get_config_files()
    pick up our fake without changing its semantics.
    """
    fake = tmp_path / "etc" / "talkat" / "config.json"
    fake.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("talkat.paths.SYSTEM_CONFIG_FILE", fake)
    return fake


def test_system_config_applied_when_user_missing(clean_config_file, system_config_path: Path):
    """With /etc config present and no user config, the system values win over defaults."""
    assert not clean_config_file.exists()
    system_config_path.write_text(json.dumps({"http_timeout": 42, "silence_duration": 1.5}))

    cfg = load_app_config()

    assert cfg["http_timeout"] == 42
    assert cfg["silence_duration"] == 1.5
    # Keys not in /etc still fall back to CODE_DEFAULTS.
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]


def test_user_config_overrides_system(clean_config_file, system_config_path: Path):
    """User ~/.config layer wins where keys overlap with /etc."""
    system_config_path.write_text(
        json.dumps({"http_timeout": 42, "silence_duration": 1.5, "model_type": "vosk"})
    )
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.write_text(json.dumps({"http_timeout": 99}))

    cfg = load_app_config()

    # User value beats system.
    assert cfg["http_timeout"] == 99
    # System keys not overridden by user are still applied.
    assert cfg["silence_duration"] == 1.5
    assert cfg["model_type"] == "vosk"


def test_partial_user_override_preserves_system_keys(clean_config_file, system_config_path: Path):
    """Pre-fix bug: a user config file used to wholesale shadow the system file.

    With layered merge, setting one key in ~/.config must not erase the
    other keys that came from /etc.
    """
    system_config_path.write_text(
        json.dumps({"http_timeout": 42, "model_type": "vosk", "language": "es"})
    )
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    # User overrides exactly one key.
    clean_config_file.write_text(json.dumps({"http_timeout": 99}))

    cfg = load_app_config()

    assert cfg["http_timeout"] == 99
    assert cfg["model_type"] == "vosk"  # would have been "faster-whisper" pre-fix
    assert cfg["language"] == "es"


def test_malformed_system_config_does_not_block_user_layer(
    clean_config_file, system_config_path: Path
):
    """A broken /etc/talkat/config.json must not prevent the user layer from applying."""
    system_config_path.write_text("{ broken json")
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.write_text(json.dumps({"http_timeout": 17}))

    cfg = load_app_config()

    # System layer was skipped (logged), user layer still applied.
    assert cfg["http_timeout"] == 17
    # Defaults fill in for anything the user didn't set.
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]
