"""Tests for talkat.config — load/save round-trip and error handling."""

import json

from talkat.config import CODE_DEFAULTS, load_app_config, save_app_config


def test_load_returns_defaults_when_no_file(clean_config_file):
    """With no config file on disk, load_app_config returns the defaults."""
    assert not clean_config_file.exists()
    cfg = load_app_config()

    # All defaults are present and match.
    for key, value in CODE_DEFAULTS.items():
        assert key in cfg
        assert cfg[key] == value


def test_save_load_round_trip(clean_config_file):
    """Values saved via save_app_config are returned by load_app_config."""
    to_save = {
        "server_port": 5556,
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
    assert loaded["server_host"] == CODE_DEFAULTS["server_host"]
    assert loaded["model_name"] == CODE_DEFAULTS["model_name"]


def test_load_returns_defaults_on_malformed_json(clean_config_file):
    """Malformed JSON should be logged and defaults returned, not raised."""
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.write_text("{ this is not valid json")

    cfg = load_app_config()

    # We get the defaults back.
    assert cfg["server_port"] == CODE_DEFAULTS["server_port"]
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]


def test_load_returns_defaults_on_invalid_port(clean_config_file):
    """Invalid value in config file → defaults returned, no exception."""
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.write_text(json.dumps({"server_port": 70000}))

    cfg = load_app_config()

    # Invalid file was rejected; defaults are used.
    assert cfg["server_port"] == CODE_DEFAULTS["server_port"]
