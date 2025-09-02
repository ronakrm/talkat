"""Security and input validation utilities for Talkat."""

import os
import re
from pathlib import Path
from typing import Any

from .logging_config import get_logger

logger = get_logger(__name__)


class SecurityError(Exception):
    """Raised when a security violation is detected."""

    pass


def validate_port(port: int | str) -> int:
    """
    Validate that a port number is within valid range.

    Args:
        port: Port number to validate

    Returns:
        Valid port number

    Raises:
        ValueError: If port is invalid
    """
    try:
        port_int = int(port)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid port number: {port}") from e

    if not 1 <= port_int <= 65535:
        raise ValueError(f"Port must be between 1 and 65535, got {port_int}")

    return port_int


def validate_file_path(
    path: str | Path, must_exist: bool = False, allow_symlinks: bool = False
) -> Path:
    """
    Validate a file path for security.

    Args:
        path: Path to validate
        must_exist: Whether the path must exist
        allow_symlinks: Whether to allow symbolic links

    Returns:
        Validated Path object

    Raises:
        SecurityError: If path is unsafe
        FileNotFoundError: If must_exist=True and path doesn't exist
    """
    # Convert to Path object
    path_obj = Path(path).expanduser().resolve()

    # Check for path traversal attempts
    try:
        if "../" in str(path) or "..\\" in str(path):
            raise SecurityError(f"Path traversal attempt detected: {path}")
    except (OSError, ValueError) as e:
        raise SecurityError(f"Invalid path: {path}") from e

    # Check if path exists when required
    if must_exist and not path_obj.exists():
        raise FileNotFoundError(f"Path does not exist: {path_obj}")

    # Check for symbolic links if not allowed
    if not allow_symlinks and path_obj.exists() and path_obj.is_symlink():
        raise SecurityError(f"Symbolic links not allowed: {path_obj}")

    # Ensure path is not in sensitive system directories
    sensitive_dirs = ["/etc", "/boot", "/sys", "/proc", "/dev", "/root"]
    path_parts = path_obj.parts
    if len(path_parts) > 1 and path_parts[1] in [d.strip("/") for d in sensitive_dirs]:
        logger.warning(f"Attempting to access sensitive directory: {path_obj}")

    return path_obj


def validate_model_name(model_name: str) -> str:
    """
    Validate a model name for security.

    Args:
        model_name: Model name to validate

    Returns:
        Validated model name

    Raises:
        ValueError: If model name is invalid
    """
    # Allow alphanumeric, dots, dashes, underscores, and forward slashes
    if not re.match(r"^[a-zA-Z0-9._/-]+$", model_name):
        raise ValueError(f"Invalid model name: {model_name}")

    # Prevent path traversal in model names
    if ".." in model_name:
        raise ValueError(f"Path traversal in model name not allowed: {model_name}")

    # Limit length to prevent DoS
    if len(model_name) > 256:
        raise ValueError(f"Model name too long: {len(model_name)} > 256")

    return model_name


def validate_command(command: list[str]) -> list[str]:
    """
    Validate a command for subprocess execution.

    Args:
        command: Command and arguments as list

    Returns:
        Validated command

    Raises:
        SecurityError: If command is potentially unsafe
    """
    if not command:
        raise ValueError("Command cannot be empty")

    # Check for shell metacharacters that could lead to injection
    dangerous_chars = [";", "&", "|", "`", "$", "(", ")", "{", "}", "<", ">", "\\n", "\\r"]
    for arg in command:
        for char in dangerous_chars:
            if char in str(arg):
                raise SecurityError(
                    f"Potentially dangerous character '{char}' in command argument: {arg}"
                )

    # Whitelist known safe commands
    safe_commands = [
        "ydotool",
        "wl-copy",
        "xclip",
        "notify-send",
        "pactl",
        "aplay",
        "ffmpeg",
        "sox",
    ]

    cmd_name = os.path.basename(command[0])
    if cmd_name not in safe_commands:
        logger.warning(f"Executing non-whitelisted command: {cmd_name}")

    return command


def sanitize_text_for_clipboard(text: str, max_length: int = 100000) -> str:
    """
    Sanitize text before copying to clipboard.

    Args:
        text: Text to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized text

    Raises:
        ValueError: If text is invalid
    """
    if not text:
        return ""

    # Limit length to prevent memory issues
    if len(text) > max_length:
        logger.warning(f"Text truncated from {len(text)} to {max_length} characters")
        text = text[:max_length]

    # Remove null bytes which can cause issues
    text = text.replace("\x00", "")

    # Normalize whitespace
    text = re.sub(r"[\r\n]+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


def sanitize_text_for_typing(text: str, max_length: int = 10000) -> str:
    """
    Sanitize text before typing with ydotool.

    Args:
        text: Text to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized text

    Raises:
        ValueError: If text is invalid
    """
    if not text:
        return ""

    # More restrictive length limit for typing
    if len(text) > max_length:
        logger.warning(f"Text truncated from {len(text)} to {max_length} characters for typing")
        text = text[:max_length]

    # Remove control characters except newline and tab
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)

    # Escape special characters that might be interpreted by ydotool
    # This is conservative - ydotool should handle these properly
    special_chars = ["\\", '"', "'", "$", "`"]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")

    return text


def validate_audio_params(
    sample_rate: int, channels: int = 1, chunk_size: int = 1024
) -> tuple[int, int, int]:
    """
    Validate audio parameters.

    Args:
        sample_rate: Sample rate in Hz
        channels: Number of audio channels
        chunk_size: Audio chunk size

    Returns:
        Tuple of (sample_rate, channels, chunk_size)

    Raises:
        ValueError: If parameters are invalid
    """
    # Valid sample rates
    valid_sample_rates = [8000, 16000, 22050, 44100, 48000]
    if sample_rate not in valid_sample_rates:
        raise ValueError(f"Invalid sample rate: {sample_rate}. Must be one of {valid_sample_rates}")

    # Valid channel counts
    if channels not in [1, 2]:
        raise ValueError(f"Invalid channel count: {channels}. Must be 1 or 2")

    # Valid chunk sizes (powers of 2 between 256 and 8192)
    if not (256 <= chunk_size <= 8192 and (chunk_size & (chunk_size - 1)) == 0):
        raise ValueError(
            f"Invalid chunk size: {chunk_size}. Must be power of 2 between 256 and 8192"
        )

    return sample_rate, channels, chunk_size


def validate_json_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Validate configuration dictionary for security.

    Args:
        config: Configuration dictionary

    Returns:
        Validated configuration

    Raises:
        ValueError: If configuration is invalid
    """
    # Validate specific config keys
    if "model_name" in config:
        config["model_name"] = validate_model_name(config["model_name"])

    if "transcript_dir" in config:
        # Don't require existence for transcript dir as it will be created
        config["transcript_dir"] = str(
            validate_file_path(config["transcript_dir"], must_exist=False)
        )

    if "model_cache_dir" in config:
        config["model_cache_dir"] = str(
            validate_file_path(config["model_cache_dir"], must_exist=False)
        )

    # Validate numeric parameters
    numeric_params = {
        "silence_threshold": (0, 10000),
        "fw_device_index": (0, 100),
    }

    for param, (min_val, max_val) in numeric_params.items():
        if param in config:
            try:
                val = float(config[param])
                if not min_val <= val <= max_val:
                    raise ValueError(f"{param} must be between {min_val} and {max_val}")
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid {param}: {config[param]}") from e

    # Validate boolean parameters
    bool_params = ["clipboard_on_long", "save_transcripts"]
    for param in bool_params:
        if param in config and not isinstance(config[param], bool):
            raise ValueError(f"{param} must be boolean, got {type(config[param])}")

    # Validate string choice parameters
    choice_params = {
        "model_type": ["faster-whisper", "distil-whisper", "vosk"],
        "fw_device": ["cpu", "cuda", "auto"],
        "fw_compute_type": ["int8", "float16", "float32"],
        "device": ["cpu", "cuda", "auto"],
    }

    for param, choices in choice_params.items():
        if param in config and config[param] not in choices:
            raise ValueError(f"{param} must be one of {choices}, got {config[param]}")

    return config


def safe_subprocess_run(command: list[str], **kwargs) -> Any:
    """
    Safely run a subprocess command with validation.

    Args:
        command: Command to run
        **kwargs: Additional arguments for subprocess.run

    Returns:
        subprocess.CompletedProcess object

    Raises:
        SecurityError: If command is unsafe
    """
    import subprocess

    # Validate command
    command = validate_command(command)

    # Set safe defaults
    safe_kwargs = {
        "shell": False,  # Never use shell=True
        "timeout": kwargs.get("timeout", 30),  # Default 30 second timeout
        "check": kwargs.get("check", False),
    }

    # Only allow specific kwargs
    allowed_kwargs = [
        "input",
        "capture_output",
        "text",
        "encoding",
        "stdout",
        "stderr",
        "check",
        "timeout",
    ]
    for k, v in kwargs.items():
        if k in allowed_kwargs:
            safe_kwargs[k] = v

    try:
        return subprocess.run(command, **safe_kwargs)
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out: {command[0]}")
        raise
    except Exception as e:
        logger.error(f"Error running command {command[0]}: {e}")
        raise
