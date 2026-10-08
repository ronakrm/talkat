import difflib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .paths import (
    CONFIG_DIR,
    CONFIG_FILE,
    DICTIONARY_FILE,
    FASTER_WHISPER_CACHE_DIR,
    SOCKET_FILE,
    TRANSCRIPT_DIR,
    VOSK_CACHE_DIR,
    get_config_files,
)

logger = get_logger(__name__)

# 1. CODE DEFAULTS
CODE_DEFAULTS: dict[str, Any] = {
    # Audio and Recognition Settings
    # Calibrated speech level (`talkat calibrate`). It decides where the
    # segmenter cuts the recording into transcription pieces — never what is
    # sent, and no longer whether a recording stops.
    "silence_threshold": 200.0,
    "silence_threshold_fallback": 500.0,  # Fallback threshold when auto-detection fails
    "silence_threshold_min": 50.0,  # Minimum allowed silence threshold
    "silence_threshold_max": 5000.0,  # Maximum allowed silence threshold
    # Pin the capture device by case-insensitive name substring (e.g.
    # "pipewire", "headset"). None = use the system default input device.
    "input_device_name": None,
    # Recording Timeouts and Durations
    # Hard cap on one recording (seconds): a safety net for a mic left open,
    # not a usage limit — at 30 s it cut long dictation off mid-sentence.
    # `talkat listen` otherwise records until you toggle it off.
    "max_recording_duration": 600.0,
    # Stop once no speech has been transcribed for this long ...
    "idle_timeout": 60.0,
    # ... having said so every this often while it's quiet, so a session left
    # open doesn't look dead.
    "idle_notify_interval": 30.0,
    # Stop after this many pieces in a row can't be transcribed (their audio
    # is saved either way).
    "max_consecutive_errors": 5,
    # Server Configuration
    "server_socket": str(SOCKET_FILE),  # Unix domain socket path for the model server
    # Network Timeouts (apply to local unix-socket requests). Durations are
    # floats and counts are ints: validation takes the type from the default.
    "http_timeout": 120.0,  # General request timeout (seconds)
    "health_check_timeout": 2.0,  # Health check timeout (seconds)
    "file_processing_timeout_base": 30.0,  # Base timeout for file processing (seconds)
    # Server limits
    "max_upload_size_mb": 100,  # Reject /transcribe_file uploads larger than this
    # Audio preprocessing (server-side, applied before ASR).
    "audio_normalize_gain": True,  # RMS-target gain scaling for quiet/loud inputs
    "audio_target_rms_dbfs": -20.0,  # Target RMS level after normalization
    "audio_max_gain_db": 20.0,  # Hard cap to keep noise from being amplified
    # Long-form segmentation: audio longer than this is split at energy
    # minima and transcribed in pieces. Faster-Whisper handles long audio
    # internally, but very long single passes still hit memory and
    # position-embedding edge cases — segmenting bounds peak cost per pass.
    "max_segment_seconds": 480.0,  # 8 minutes — well under any single-pass cliff
    # Process Management Timeouts
    # The stop wait must cover the work a listen process legitimately does
    # AFTER the stop signal. Typing as you talk leaves little: the last
    # segment's ASR and typing. The ceiling is sized for a max-length
    # recording delivered at once (--postprocess): the LLM call plus ~22 ms
    # per typed character, ~2.5 min for 10 min of speech. The poll returns the
    # moment the process exits, so the common case doesn't feel this ceiling —
    # but hitting it escalates to SIGTERM, which aborts: untyped text goes to
    # the clipboard and untranscribed audio is saved.
    "process_stop_timeout": 300.0,
    "lock_acquire_timeout": 1.0,  # Max time to wait for lock acquisition
    "lock_retry_interval": 0.01,  # Sleep interval between lock acquisition attempts
    "process_check_interval": 0.1,  # Sleep interval when checking process status
    "background_process_delay": 0.5,  # Delay when stopping background processes
    # Model Configuration
    "model_type": "faster-whisper",  # Options: faster-whisper, vosk
    "model_name": "small.en",
    "faster_whisper_model_cache_dir": str(FASTER_WHISPER_CACHE_DIR),
    "fw_device": "cpu",
    "fw_compute_type": "int8",
    "fw_device_index": 0,
    "vosk_model_base_dir": str(VOSK_CACHE_DIR),
    # Language passed to the ASR backend. "auto" → autodetect (faster-whisper);
    # Vosk ignores this (language is baked into the loaded model).
    "language": "en",
    # Application Features
    "save_transcripts": True,
    "transcript_dir": str(TRANSCRIPT_DIR),
    # Where listen-mode output goes: "type" (ydotool into the focused
    # window) or "clipboard" (wl-copy/xclip only, never types).
    "output_mode": "type",
    # Refuse to type if the focused window changed between recording start
    # and transcription end (transcript goes to the clipboard instead).
    # Active on compositors with a supported IPC: niri, Hyprland, sway.
    "focus_guard": True,
    # Dictionary Configuration
    "dictionary_file": str(DICTIONARY_FILE),
    # AI Post-Processing (AIPP) — opt-in, off by default.
    # Map of profile-name → {base_url, model, system_prompt, api_key_env?, timeout?}.
    # Activated per-invocation with `--postprocess <name>`; see security.py
    # ``validate_postprocess_profile`` for the full schema.
    "postprocess_profiles": {},
}


class ConfigFileError(ValueError):
    """The user's config file can't be safely rewritten, so it was left as it is."""


@dataclass(frozen=True)
class ConfigIssue:
    """A config file, or one setting in it, that talkat had to ignore."""

    path: Path
    key: str | None  # None: the whole file was skipped
    problem: str
    using: str | None = None  # for an invalid setting: the value used instead

    @property
    def detail(self) -> str:
        """The issue without its file — doctor prints the file as the label."""
        text = f"{self.key}: {self.problem}" if self.key else self.problem
        return text if self.using is None else f"{text} — using {self.using}"

    def __str__(self) -> str:
        return f"{self.path}: {self.detail}"


# load_app_config runs many times per invocation; each issue is logged once.
_logged_issues: set[str] = set()


def _parse_file(path: Path) -> Any:
    """A config file's JSON (an empty file counts as ``{}``). Raises OSError/ValueError."""
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else {}


def _json_kind(value: object) -> str:
    """What a parsed JSON value is called in JSON, for messages."""
    names = {list: "an array", str: "a string", bool: "a boolean", type(None): "null"}
    return names.get(type(value), "a number")


# Settings that were removed in favor of another — say which, rather than guess.
_REPLACED_KEYS = {
    "device": "'fw_device'",
    "model_cache_dir": "'faster_whisper_model_cache_dir' or 'vosk_model_base_dir'",
    "long_mode_silence_timeout": "'idle_timeout'",
    "long_mode_max_session_duration": "'max_recording_duration'",
}


def _unknown_key_problem(key: str) -> str:
    if key in _REPLACED_KEYS:
        return f"no longer a talkat setting, ignored — use {_REPLACED_KEYS[key]} instead"
    close = difflib.get_close_matches(key, list(CODE_DEFAULTS), n=1)
    return "not a talkat setting, ignored" + (f" — did you mean {close[0]!r}?" if close else "")


def _valid_profiles(
    path: Path, profiles: dict[str, Any], issues: list[ConfigIssue]
) -> dict[str, Any]:
    """Check AIPP profiles one at a time, so a broken profile drops only itself."""
    from .security import validate_json_config

    valid: dict[str, Any] = {}
    for name, profile in profiles.items():
        try:
            checked = validate_json_config({"postprocess_profiles": {name: profile}})
        except Exception as e:  # same rule as _read_layer: a bad profile never raises
            problem = str(e).removeprefix(f"postprocess profile {name!r} ")
            issues.append(ConfigIssue(path, f"postprocess_profiles.{name}", problem))
        else:
            valid.update(checked["postprocess_profiles"])
    return valid


def _read_layer(path: Path) -> tuple[dict[str, Any], list[ConfigIssue]]:
    """The valid settings in one config file, and an issue for everything it ignores."""
    from .security import validate_json_config

    try:
        layer = _parse_file(path)
    except (OSError, ValueError, RecursionError) as e:
        return {}, [ConfigIssue(path, None, f"can't be read ({e}) — whole file ignored")]
    if not isinstance(layer, dict):
        problem = f"holds {_json_kind(layer)}, not a JSON object — whole file ignored"
        return {}, [ConfigIssue(path, None, problem)]

    valid: dict[str, Any] = {}
    issues: list[ConfigIssue] = []
    for key, value in layer.items():
        if key not in CODE_DEFAULTS:
            issues.append(ConfigIssue(path, key, _unknown_key_problem(key)))
        elif key == "postprocess_profiles" and isinstance(value, dict):
            valid[key] = _valid_profiles(path, value, issues)
        else:
            try:
                valid[key] = validate_json_config({key: value})[key]
            except Exception as e:  # whatever a bad value raises, it costs that key, never the run
                problem = str(e).removeprefix(f"{key} ") or type(e).__name__
                issues.append(ConfigIssue(path, key, problem))
    return valid, issues


def _with_fallback(issue: ConfigIssue, config: dict[str, Any]) -> ConfigIssue:
    """An ignored setting falls back to whatever the merge settled on — say what."""
    if issue.key is None or issue.key not in CODE_DEFAULTS:
        return issue
    return replace(issue, using=repr(config[issue.key]))


def load_app_config_with_issues() -> tuple[dict[str, Any], list[ConfigIssue]]:
    """The effective configuration, plus everything in the config files it ignored.

    Layers, lowest to highest precedence:
        1. ``CODE_DEFAULTS`` (built into the package)
        2. ``/etc/talkat/config.json`` (system, optional — set by packagers)
        3. ``~/.config/talkat/config.json`` (per-user, optional)

    Each layer partially overrides the previous, so a user can override one
    key without restating the rest. Validation is per setting: an invalid
    value falls back to the layer below — ultimately the default — while the
    rest of its file still applies, and keys talkat doesn't know are ignored.
    Only a file that doesn't parse as a JSON object is skipped whole. Nothing
    in a config file can make this raise.

    CLI-level overrides (``--max-recording`` etc.) are merged on top of
    the result by callers in ``cli.py``; they do not live here.
    """
    config = CODE_DEFAULTS.copy()
    issues: list[ConfigIssue] = []
    for path in get_config_files():
        logger.debug(f"Loading config from {path}...")
        layer, layer_issues = _read_layer(path)
        config.update(layer)
        issues.extend(layer_issues)

    if os.environ.get("TALKAT_RUNTIME_DIR"):
        # Dev-isolation override (see paths.py / dev.sh): the whole runtime
        # dir moved, so a socket path pinned in a config file must not win —
        # it would defeat the point of the override.
        config["server_socket"] = str(SOCKET_FILE)

    return config, [_with_fallback(issue, config) for issue in issues]


def load_app_config() -> dict[str, Any]:
    """The effective configuration — see :func:`load_app_config_with_issues`.

    What the config files had that couldn't be used is logged, each issue
    once per process; ``talkat doctor`` lists them all.
    """
    config, issues = load_app_config_with_issues()
    for issue in issues:
        text = str(issue)
        if text in _logged_issues:
            continue
        _logged_issues.add(text)
        if issue.key is None:
            logger.error(text)
        else:
            logger.warning(text)
    return config


def _read_user_file() -> dict[str, Any]:
    """The user's config file exactly as written — ``{}`` if there isn't one."""
    try:
        data = _parse_file(CONFIG_FILE)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as e:
        raise ConfigFileError(f"{CONFIG_FILE} can't be read ({e}), so it was left as is") from e
    if not isinstance(data, dict):
        raise ConfigFileError(
            f"{CONFIG_FILE} holds {_json_kind(data)}, not a JSON object, so it was left as is"
        )
    return data


def update_user_config(updates: dict[str, Any]) -> Path:
    """Set ``updates`` in the user's config file, leaving everything else as written.

    A read-modify-write of ``~/.config/talkat/config.json`` alone: values from
    /etc or the defaults are never copied in — that would freeze them,
    silently opting the user out of future default changes — and a key set
    back to its default is removed for the same reason. The user's other
    entries are kept, invalid ones included (loading reports those). Only
    keys this version doesn't know at all, left over from older releases,
    are dropped, with a log line.

    Raises :class:`ConfigFileError` rather than replace a file that doesn't
    parse: writing over it would throw away everything in it. Returns the
    path written.
    """
    from .security import validate_json_config

    unknown = sorted(set(updates) - set(CODE_DEFAULTS))
    if unknown:
        raise ValueError(f"Not talkat settings: {', '.join(unknown)}")
    updates = validate_json_config(dict(updates))

    user_config = _read_user_file()
    dropped = sorted(key for key in user_config if key not in CODE_DEFAULTS)
    if dropped:
        logger.info(f"Dropping config keys unknown to this version: {', '.join(dropped)}")
    user_config = {key: value for key, value in user_config.items() if key in CODE_DEFAULTS}
    for key, value in updates.items():
        if value == CODE_DEFAULTS[key]:
            user_config.pop(key, None)
        else:
            user_config[key] = value

    # Serialize before opening the file, so a failure can't leave it truncated.
    text = json.dumps(user_config, indent=4) + "\n"
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(text, encoding="utf-8")
    except OSError as e:
        raise ConfigFileError(f"Couldn't write {CONFIG_FILE}: {e}") from e
    logger.info(f"Configuration saved to {CONFIG_FILE}")
    return CONFIG_FILE
