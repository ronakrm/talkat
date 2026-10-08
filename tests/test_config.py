"""Tests for talkat.config — layered loading, per-setting validation, user-file updates."""

import json
import logging
from pathlib import Path

import pytest

from talkat import config as config_mod
from talkat.config import (
    CODE_DEFAULTS,
    ConfigFileError,
    load_app_config,
    load_app_config_with_issues,
    update_user_config,
)


def _write(path: Path, data: object) -> None:
    """Write ``data`` to a config file: a str verbatim, anything else as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def test_load_returns_defaults_when_no_file(clean_config_file):
    """With no config file on disk, load_app_config returns the defaults."""
    assert not clean_config_file.exists()
    cfg = load_app_config()

    for key, value in CODE_DEFAULTS.items():
        assert key in cfg
        assert cfg[key] == value


def test_update_load_round_trip(clean_config_file):
    """Values set via update_user_config are returned by load_app_config."""
    to_save = {
        "model_type": "vosk",
        "save_transcripts": False,
        "http_timeout": 60,
        "idle_timeout": 120.0,
    }
    update_user_config(to_save)

    assert clean_config_file.exists()
    loaded = load_app_config()

    for key, value in to_save.items():
        assert loaded[key] == value

    # Defaults for keys we didn't override are still present.
    assert loaded["server_socket"] == CODE_DEFAULTS["server_socket"]
    assert loaded["model_name"] == CODE_DEFAULTS["model_name"]


def test_load_returns_defaults_on_malformed_json(clean_config_file):
    """Malformed JSON should be logged and defaults returned, not raised."""
    _write(clean_config_file, "{ this is not valid json")

    cfg, [issue] = load_app_config_with_issues()

    assert cfg["server_socket"] == CODE_DEFAULTS["server_socket"]
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]
    assert issue.key is None
    assert "whole file ignored" in issue.problem


@pytest.mark.parametrize("content", ["[1, 2]", '"text"', "null", "42"])
def test_non_object_file_is_skipped_whole(clean_config_file, content: str):
    _write(clean_config_file, content)

    cfg, [issue] = load_app_config_with_issues()

    assert cfg == CODE_DEFAULTS
    assert issue.key is None
    assert "not a JSON object" in issue.problem


def test_empty_file_counts_as_no_settings(clean_config_file):
    _write(clean_config_file, "  \n")

    cfg, issues = load_app_config_with_issues()

    assert issues == []
    assert cfg == CODE_DEFAULTS


# ---------------------------------------------------------------------------
# Per-setting validation — a bad value costs its own key, never the file
# ---------------------------------------------------------------------------


def test_invalid_value_drops_only_that_key(clean_config_file):
    """One bad value used to make talkat skip the whole file, every valid key with it."""
    _write(
        clean_config_file,
        {"silence_threshold": 99999, "language": "es", "output_mode": "clipboard"},
    )

    cfg, [issue] = load_app_config_with_issues()

    assert cfg["silence_threshold"] == CODE_DEFAULTS["silence_threshold"]
    assert cfg["language"] == "es"
    assert cfg["output_mode"] == "clipboard"
    assert issue.path == clean_config_file
    assert issue.key == "silence_threshold"
    assert issue.problem == "must be between 0 and 10000, got 99999"
    assert issue.using == repr(CODE_DEFAULTS["silence_threshold"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("idle_timeout", "120"),  # a string float() would have accepted
        ("max_consecutive_errors", True),  # a bool float() would have accepted
        ("max_consecutive_errors", 2.5),  # a count must be whole
        ("fw_device_index", "0"),  # used to reach WhisperModel as a string
        ("focus_guard", "false"),
    ],
)
def test_values_of_the_wrong_type_are_ignored(clean_config_file, key: str, value: object):
    _write(clean_config_file, {key: value})

    cfg, [issue] = load_app_config_with_issues()

    assert cfg[key] == CODE_DEFAULTS[key]
    assert issue.key == key


def test_numbers_take_the_type_of_their_default(clean_config_file):
    _write(clean_config_file, {"max_consecutive_errors": 3.0, "idle_timeout": 120})

    cfg = load_app_config()

    assert cfg["max_consecutive_errors"] == 3
    assert isinstance(cfg["max_consecutive_errors"], int)
    assert cfg["idle_timeout"] == 120.0
    assert isinstance(cfg["idle_timeout"], float)


_GARBAGE = [None, [], {}, "", "x" * 10_000, 10**400, -1, True, 1e308, float("nan")]


@pytest.mark.parametrize(
    "value", _GARBAGE, ids=[type(v).__name__ + str(i) for i, v in enumerate(_GARBAGE)]
)
def test_no_value_in_a_config_file_makes_loading_raise(clean_config_file, value: object):
    """Whatever a value is, it costs at most its own key — never the run."""
    _write(clean_config_file, {key: value for key in CODE_DEFAULTS})

    cfg, issues = load_app_config_with_issues()

    for issue in issues:
        assert issue.key is not None
        assert cfg[issue.key] == CODE_DEFAULTS[issue.key]


# ---------------------------------------------------------------------------
# Unknown keys — ignored, with a suggestion
# ---------------------------------------------------------------------------


def test_unknown_keys_are_ignored_with_a_suggestion(clean_config_file):
    _write(clean_config_file, {"idle_timout": 300.0, "idle_notify_interval": 45.0})

    cfg, [issue] = load_app_config_with_issues()

    assert "idle_timout" not in cfg
    assert cfg["idle_timeout"] == CODE_DEFAULTS["idle_timeout"]
    assert cfg["idle_notify_interval"] == 45.0
    assert issue.key == "idle_timout"
    assert "did you mean 'idle_timeout'" in issue.problem
    assert issue.using is None


@pytest.mark.parametrize(
    ("removed", "replacement"),
    [
        ("device", "'fw_device'"),
        ("model_cache_dir", "'faster_whisper_model_cache_dir' or 'vosk_model_base_dir'"),
        ("long_mode_silence_timeout", "'idle_timeout'"),
    ],
)
def test_replaced_keys_name_their_replacement(clean_config_file, removed: str, replacement: str):
    """Nothing ever read ``device`` or ``model_cache_dir``; say which keys do the job."""
    assert removed not in CODE_DEFAULTS
    _write(clean_config_file, {removed: "x"})

    cfg, [issue] = load_app_config_with_issues()

    assert removed not in cfg
    assert issue.problem.endswith(f"use {replacement} instead")


def test_pre_speech_padding_is_not_a_setting(clean_config_file):
    _write(clean_config_file, {"pre_speech_padding": 2})

    cfg, [issue] = load_app_config_with_issues()

    assert "pre_speech_padding" not in cfg
    assert issue.key == "pre_speech_padding"


# ---------------------------------------------------------------------------
# Path settings — trusted user input
# ---------------------------------------------------------------------------


def test_path_settings_accept_symlinks_dotdot_and_tilde(
    clean_config_file, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A model cache symlinked onto a bigger disk must just work.

    Symlinked and ``..`` paths used to raise SecurityError straight out of
    load_app_config, failing every command.
    """
    real = tmp_path / "big-disk" / "models"
    real.mkdir(parents=True)
    link = tmp_path / "models-link"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path))
    dotdot = str(tmp_path / "a" / ".." / "transcripts")
    _write(
        clean_config_file,
        {
            "faster_whisper_model_cache_dir": str(link),
            "transcript_dir": dotdot,
            "dictionary_file": "~/words.txt",
        },
    )

    cfg, issues = load_app_config_with_issues()

    assert issues == []
    assert cfg["faster_whisper_model_cache_dir"] == str(link)
    assert cfg["transcript_dir"] == dotdot
    assert cfg["dictionary_file"] == str(tmp_path / "words.txt")


def test_relative_path_falls_back_to_default(clean_config_file):
    _write(clean_config_file, {"transcript_dir": "transcripts"})

    cfg, [issue] = load_app_config_with_issues()

    assert cfg["transcript_dir"] == CODE_DEFAULTS["transcript_dir"]
    assert "absolute path" in issue.problem


# ---------------------------------------------------------------------------
# AIPP profiles — validated one at a time
# ---------------------------------------------------------------------------

_GOOD_PROFILE = {
    "base_url": "http://localhost:11434/v1",
    "model": "llama3.2:3b",
    "system_prompt": "Tidy the text.",
}


def test_bad_profile_drops_only_itself(clean_config_file):
    _write(
        clean_config_file,
        {"postprocess_profiles": {"tidy": _GOOD_PROFILE, "broken": {"model": "x"}}},
    )

    cfg, [issue] = load_app_config_with_issues()

    assert list(cfg["postprocess_profiles"]) == ["tidy"]
    assert issue.key == "postprocess_profiles.broken"
    assert issue.problem == "missing required key 'base_url'"


def test_profiles_that_are_not_an_object_fall_back(clean_config_file):
    _write(clean_config_file, {"postprocess_profiles": ["tidy"]})

    cfg, [issue] = load_app_config_with_issues()

    assert cfg["postprocess_profiles"] == {}
    assert issue.key == "postprocess_profiles"


# ---------------------------------------------------------------------------
# Logging — each issue once per process
# ---------------------------------------------------------------------------


def test_each_issue_is_logged_once_per_process(
    clean_config_file, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_mod, "_logged_issues", set())
    _write(clean_config_file, {"silence_threshold": 99999})

    with caplog.at_level(logging.WARNING):
        load_app_config()
        load_app_config()

    hits = [r for r in caplog.records if "silence_threshold" in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING
    assert hits[0].getMessage().endswith("— using 200.0")


def test_unreadable_file_is_logged_as_an_error(
    clean_config_file, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_mod, "_logged_issues", set())
    _write(clean_config_file, "{ broken")

    with caplog.at_level(logging.WARNING):
        load_app_config()

    [record] = [r for r in caplog.records if "whole file ignored" in r.getMessage()]
    assert record.levelno == logging.ERROR


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
    system_config_path.write_text(json.dumps({"http_timeout": 42, "idle_timeout": 90.0}))

    cfg = load_app_config()

    assert cfg["http_timeout"] == 42
    assert cfg["idle_timeout"] == 90.0
    # Keys not in /etc still fall back to CODE_DEFAULTS.
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]


def test_user_config_overrides_system(clean_config_file, system_config_path: Path):
    """User ~/.config layer wins where keys overlap with /etc."""
    system_config_path.write_text(
        json.dumps({"http_timeout": 42, "idle_timeout": 90.0, "model_type": "vosk"})
    )
    _write(clean_config_file, {"http_timeout": 99})

    cfg = load_app_config()

    # User value beats system.
    assert cfg["http_timeout"] == 99
    # System keys not overridden by user are still applied.
    assert cfg["idle_timeout"] == 90.0
    assert cfg["model_type"] == "vosk"


def test_partial_user_override_preserves_system_keys(clean_config_file, system_config_path: Path):
    """Pre-fix bug: a user config file used to wholesale shadow the system file.

    With layered merge, setting one key in ~/.config must not erase the
    other keys that came from /etc.
    """
    system_config_path.write_text(
        json.dumps({"http_timeout": 42, "model_type": "vosk", "language": "es"})
    )
    # User overrides exactly one key.
    _write(clean_config_file, {"http_timeout": 99})

    cfg = load_app_config()

    assert cfg["http_timeout"] == 99
    assert cfg["model_type"] == "vosk"  # would have been "faster-whisper" pre-fix
    assert cfg["language"] == "es"


def test_malformed_system_config_does_not_block_user_layer(
    clean_config_file, system_config_path: Path
):
    """A broken /etc/talkat/config.json must not prevent the user layer from applying."""
    system_config_path.write_text("{ broken json")
    _write(clean_config_file, {"http_timeout": 17})

    cfg = load_app_config()

    # System layer was skipped (logged), user layer still applied.
    assert cfg["http_timeout"] == 17
    # Defaults fill in for anything the user didn't set.
    assert cfg["model_type"] == CODE_DEFAULTS["model_type"]


def test_invalid_user_value_falls_back_to_system_layer(clean_config_file, system_config_path: Path):
    """An ignored setting falls back to the layer below, not straight to the default."""
    system_config_path.write_text(json.dumps({"idle_timeout": 90.0}))
    _write(clean_config_file, {"idle_timeout": "two minutes", "http_timeout": 99})

    cfg, [issue] = load_app_config_with_issues()

    assert cfg["idle_timeout"] == 90.0
    assert cfg["http_timeout"] == 99
    assert issue.path == clean_config_file
    assert issue.using == "90.0"


# ---------------------------------------------------------------------------
# update_user_config — read-modify-write of the user's own file
# ---------------------------------------------------------------------------


def test_update_writes_only_the_given_key(clean_config_file, system_config_path: Path):
    """Neither defaults nor /etc values get copied in.

    Copying them freezes them: the user silently stops receiving default
    improvements for every key in the file. Regression test for the
    calibrate-saves-everything bug.
    """
    system_config_path.write_text(json.dumps({"http_timeout": 42}))

    update_user_config({"silence_threshold": 129.5})

    assert json.loads(clean_config_file.read_text()) == {"silence_threshold": 129.5}


def test_update_keeps_the_users_other_entries(clean_config_file):
    """Even an invalid entry survives: the old save wiped the whole file whenever one existed."""
    _write(clean_config_file, {"model_name": "tiny.en", "idle_timeout": 2, "focus_guard": False})

    update_user_config({"silence_threshold": 300.0})

    assert json.loads(clean_config_file.read_text()) == {
        "model_name": "tiny.en",
        "idle_timeout": 2,
        "focus_guard": False,
        "silence_threshold": 300.0,
    }


def test_update_drops_keys_unknown_to_this_version(
    clean_config_file, caplog: pytest.LogCaptureFixture
):
    """Dead keys from older releases are dropped on write, with a log line."""
    _write(
        clean_config_file,
        {
            "silence_threshold": 300.0,
            "long_mode_max_duration": 600.0,  # renamed in a past release
            "distil_model_name": "distil-whisper/x",  # removed feature
        },
    )

    with caplog.at_level(logging.INFO):
        update_user_config({"model_name": "medium.en"})

    assert json.loads(clean_config_file.read_text()) == {
        "silence_threshold": 300.0,
        "model_name": "medium.en",
    }
    assert "distil_model_name, long_mode_max_duration" in caplog.text


def test_update_back_to_default_removes_the_key(clean_config_file):
    _write(clean_config_file, {"silence_threshold": 300.0, "model_name": "tiny.en"})

    update_user_config({"silence_threshold": CODE_DEFAULTS["silence_threshold"]})

    assert json.loads(clean_config_file.read_text()) == {"model_name": "tiny.en"}


def test_update_creates_the_file(clean_config_file):
    update_user_config({"focus_guard": False})

    assert json.loads(clean_config_file.read_text()) == {"focus_guard": False}


@pytest.mark.parametrize("content", ["{ broken json", "[1, 2]"])
def test_update_refuses_to_overwrite_a_file_it_cant_parse(clean_config_file, content: str):
    """Writing over an unparseable file would throw away everything in it."""
    _write(clean_config_file, content)

    with pytest.raises(ConfigFileError, match="left as is"):
        update_user_config({"silence_threshold": 300.0})

    assert clean_config_file.read_text() == content


@pytest.mark.parametrize("updates", [{"no_such_key": 1}, {"silence_threshold": -1}])
def test_update_rejects_bad_settings_without_writing(clean_config_file, updates: dict):
    with pytest.raises(ValueError):
        update_user_config(updates)

    assert not clean_config_file.exists()


def test_update_writes_through_a_symlinked_config(clean_config_file, tmp_path: Path):
    """Dotfile managers symlink config.json; an update must not replace the link."""
    real = tmp_path / "dotfiles" / "talkat.json"
    _write(real, {"model_name": "tiny.en"})
    clean_config_file.parent.mkdir(parents=True, exist_ok=True)
    clean_config_file.symlink_to(real)

    update_user_config({"silence_threshold": 300.0})

    assert clean_config_file.is_symlink()
    assert json.loads(real.read_text()) == {"model_name": "tiny.en", "silence_threshold": 300.0}


# ---------------------------------------------------------------------------
# talkat calibrate — writes only its threshold, never a broken file
# ---------------------------------------------------------------------------


def test_calibrate_saves_only_the_threshold(clean_config_file, monkeypatch: pytest.MonkeyPatch):
    from talkat import main as main_mod

    monkeypatch.setattr(main_mod, "calibrate_microphone", lambda: 321.5)
    monkeypatch.setattr(main_mod, "_notify", lambda message: None)
    _write(clean_config_file, {"model_name": "tiny.en"})

    assert main_mod.run_calibrate() == 0

    assert json.loads(clean_config_file.read_text()) == {
        "model_name": "tiny.en",
        "silence_threshold": 321.5,
    }


def test_calibrate_reports_the_threshold_when_config_cant_be_saved(
    clean_config_file, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    from talkat import main as main_mod

    monkeypatch.setattr(main_mod, "calibrate_microphone", lambda: 321.5)
    monkeypatch.setattr(main_mod, "_notify", lambda message: None)
    _write(clean_config_file, "{ broken")

    with caplog.at_level(logging.ERROR):
        assert main_mod.run_calibrate() == 1

    assert clean_config_file.read_text() == "{ broken"
    assert '"silence_threshold": 321.5' in caplog.text


# ---------------------------------------------------------------------------
# TALKAT_RUNTIME_DIR — a config-pinned socket must not defeat dev isolation
# ---------------------------------------------------------------------------


def test_runtime_dir_override_forces_socket_over_config_pin(
    clean_config_file, monkeypatch: pytest.MonkeyPatch
):
    from talkat.paths import SOCKET_FILE

    _write(clean_config_file, {"server_socket": "/tmp/pinned-elsewhere.sock"})

    monkeypatch.setenv("TALKAT_RUNTIME_DIR", "/tmp/talkat-dev-test")
    cfg = load_app_config()
    assert cfg["server_socket"] == str(SOCKET_FILE)

    monkeypatch.delenv("TALKAT_RUNTIME_DIR")
    cfg = load_app_config()
    assert cfg["server_socket"] == "/tmp/pinned-elsewhere.sock"
