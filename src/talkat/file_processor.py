"""Audio file processing module for transcribing audio files directly."""

import json
import sys
from pathlib import Path
from typing import Any

import httpx

from .clipboard import copy_to_clipboard
from .diagnostics import build_record, write_record
from .logging_config import get_logger
from .security import validate_file_path

logger = get_logger(__name__)


# Side-channel for the most recent file transcribe call. The public
# ``transcribe_audio_file`` keeps its ``(text, duration)`` return shape
# (tests and external callers depend on it); commands that want the
# server's full response (applied_gain_db, asr_seconds, etc.) read this
# right after the call. Resetting it on each call keeps stale values
# from leaking between transcriptions.
_LAST_RESPONSE: dict[str, Any] = {}


def get_last_response_metadata() -> dict[str, Any]:
    """Return server metadata from the most recent transcribe_audio_file call."""
    return dict(_LAST_RESPONSE)


def _make_client(socket_path: str, timeout: float) -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=socket_path), timeout=timeout)


def transcribe_audio_file(
    file_path: str,
    socket_path: str | None = None,
    output_format: str = "text",
    language: str | None = None,
) -> tuple[str, float]:
    """
    Transcribe an audio file by uploading it to the model server.

    Args:
        file_path: Path to the audio file
        socket_path: Unix socket path of the model server (uses config default if None)
        output_format: Output format (text, json, srt, vtt)
        language: ASR language code (e.g. "en", "es", "auto"). When None,
            the server applies its configured default.

    Returns:
        Tuple of (transcription, duration in seconds)
    """
    from .config import CODE_DEFAULTS, load_app_config

    config = load_app_config()

    if socket_path is None:
        socket_path = config.get("server_socket", CODE_DEFAULTS["server_socket"])
    if language is None:
        language = config.get("language")

    try:
        file_path_obj = validate_file_path(file_path, must_exist=True)
    except Exception as e:
        raise FileNotFoundError(f"Invalid audio file: {e}") from e

    supported_formats = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".mp4", ".webm"}
    if file_path_obj.suffix.lower() not in supported_formats:
        raise ValueError(f"Unsupported file format: {file_path_obj.suffix}")

    max_size_mb = int(config.get("max_upload_size_mb", CODE_DEFAULTS["max_upload_size_mb"]))
    file_size_bytes = file_path_obj.stat().st_size
    if file_size_bytes > max_size_mb * 1024 * 1024:
        raise ValueError(
            f"Audio file is {file_size_bytes / 1024 / 1024:.1f} MB; "
            f"exceeds the {max_size_mb} MB server limit. "
            "Raise it via 'max_upload_size_mb' in config.json if needed."
        )

    try:
        import librosa
        import soundfile  # noqa: F401 - test for availability
    except ImportError:
        logger.error("librosa and soundfile are required for file processing")
        logger.error("Install them with: pip install librosa soundfile")
        sys.exit(1)

    logger.info(f"Loading audio file: {file_path_obj}")

    try:
        audio_data, sample_rate = librosa.load(str(file_path_obj), sr=16000, mono=True)
        duration = len(audio_data) / sample_rate
        logger.info(f"Audio loaded: {duration:.1f} seconds at {sample_rate} Hz")

        health_timeout = config.get("health_check_timeout", CODE_DEFAULTS["health_check_timeout"])
        request_timeout = max(
            config.get(
                "file_processing_timeout_base",
                CODE_DEFAULTS["file_processing_timeout_base"],
            ),
            duration * 2,
        )

        with _make_client(socket_path, request_timeout) as client:
            try:
                health = client.get("http://talkat/health", timeout=health_timeout)
                if health.status_code != 200:
                    logger.error("Model server is not ready")
                    sys.exit(1)
            except httpx.ConnectError:
                logger.error(f"Model server is not running (socket: {socket_path})")
                logger.error("Start it with: systemctl --user start talkat")
                sys.exit(1)

            with open(file_path_obj, "rb") as f:
                files = {"audio": (file_path_obj.name, f, "audio/*")}
                data = {"language": language} if language else None
                response = client.post("http://talkat/transcribe_file", files=files, data=data)

        if response.status_code == 413:
            logger.error(f"Server rejected upload: {response.json().get('error', 'too large')}")
            sys.exit(1)
        if response.status_code != 200:
            error_msg = response.json().get("error", "Unknown error")
            logger.error(f"Error transcribing file: {error_msg}")
            sys.exit(1)

        result = response.json()
        transcription = result.get("text", "").strip()

        global _LAST_RESPONSE
        _LAST_RESPONSE = {
            "audio_duration": float(result.get("audio_duration", duration) or duration),
            "applied_gain_db": float(result.get("applied_gain_db", 0.0) or 0.0),
            "asr_seconds": float(result.get("asr_seconds", 0.0) or 0.0),
        }

        if not transcription:
            logger.warning("No speech detected in the audio file")
            return "", duration

        return transcription, duration

    except Exception as e:
        if "NoBackendError" in str(type(e).__name__):
            logger.error("No audio backend available")
            logger.error("Install ffmpeg: sudo apt-get install ffmpeg")
            sys.exit(1)
        else:
            logger.error(f"Error processing audio file: {e}")
            import traceback

            traceback.print_exc()
            sys.exit(1)


def format_output(transcription: str, duration: float, output_format: str = "text") -> str:
    """
    Format transcription output.

    Args:
        transcription: The transcribed text
        duration: Duration of the audio in seconds
        output_format: Output format (text, json, srt, vtt)

    Returns:
        Formatted output string
    """
    if output_format == "json":
        return json.dumps(
            {"text": transcription, "duration": duration, "words": transcription.split()}, indent=2
        )

    elif output_format == "srt":
        # Simple SRT format (single subtitle for entire transcription)
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        seconds = duration % 60

        return f"""1
00:00:00,000 --> {hours:02d}:{minutes:02d}:{seconds:06.3f}
{transcription}
"""

    elif output_format == "vtt":
        # WebVTT format
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        seconds = duration % 60

        return f"""WEBVTT

00:00:00.000 --> {hours:02d}:{minutes:02d}:{seconds:06.3f}
{transcription}
"""

    else:  # Default to text
        return transcription


def process_audio_file_command(
    file_path: str,
    output_file: str | None = None,
    output_format: str = "text",
    clipboard: bool = False,
    language: str | None = None,
    postprocess: str | None = None,
) -> int:
    """
    Process an audio file and output the transcription.

    Args:
        file_path: Path to the audio file
        output_file: Optional output file path
        output_format: Output format (text, json, srt, vtt)
        clipboard: Whether to copy to clipboard
        language: ASR language code; None defers to config default
        postprocess: Optional AIPP profile name; applies post-transcription,
            fail-open to the raw transcript.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    try:
        # Transcribe the file
        transcription, duration = transcribe_audio_file(file_path, language=language)
        server_meta = get_last_response_metadata()

        if not transcription:
            logger.warning("No speech detected in the audio file")
            _write_file_diagnostics(
                file_path=file_path,
                transcription="",
                duration=duration,
                server_meta=server_meta,
                postprocess=postprocess,
                error=None,
            )
            return 1

        if postprocess:
            from .postprocess import postprocess_text

            transcription = postprocess_text(transcription, postprocess)

        # Format the output
        formatted_output = format_output(transcription, duration, output_format)

        # Output to file if specified
        if output_file:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(formatted_output)
            logger.info(f"Transcription saved to: {output_path}")
        else:
            # Output to stdout
            print(formatted_output)  # Keep this as print for stdout output

        if clipboard:
            if copy_to_clipboard(transcription):
                logger.info("Transcription copied to clipboard")
            else:
                logger.warning("Could not copy to clipboard (wl-copy or xclip not available)")

        # Show summary
        logger.info("\nSummary:")
        logger.info(f"  Duration: {duration:.1f} seconds")
        logger.info(f"  Words: {len(transcription.split())}")
        logger.info(f"  Characters: {len(transcription)}")

        _write_file_diagnostics(
            file_path=file_path,
            transcription=transcription,
            duration=duration,
            server_meta=server_meta,
            postprocess=postprocess,
            error=None,
        )

        return 0

    except Exception as e:
        logger.error(f"Error: {e}")
        _write_file_diagnostics(
            file_path=file_path,
            transcription="",
            duration=0.0,
            server_meta={},
            postprocess=postprocess,
            error=str(e),
        )
        return 1


def _write_file_diagnostics(
    *,
    file_path: str,
    transcription: str,
    duration: float,
    server_meta: dict[str, Any],
    postprocess: str | None,
    error: str | None,
) -> None:
    """Write a diagnostics record for a file-mode transcription. Advisory."""
    try:
        from .config import CODE_DEFAULTS, load_app_config

        config = load_app_config()
        model_type = str(config.get("model_type", CODE_DEFAULTS["model_type"]))
        model_name = str(config.get("model_name", CODE_DEFAULTS["model_name"]))
        record = build_record(
            mode="file",
            audio_duration=float(server_meta.get("audio_duration", duration)),
            asr_seconds=float(server_meta.get("asr_seconds", 0.0)),
            applied_gain_db=float(server_meta.get("applied_gain_db", 0.0)),
            model_type=model_type,
            model_name=model_name,
            transcript_chars=len(transcription),
            transcript_words=len(transcription.split()) if transcription else 0,
            postprocess_profile=postprocess,
            errors=[error] if error else [],
            extra={"input_file": file_path},
        )
        write_record(record)
    except Exception as e:  # noqa: BLE001 — diagnostics are advisory
        logger.debug(f"File diagnostics skipped (non-fatal): {e}")


def batch_process_files(
    file_paths: list[str],
    output_dir: str | None = None,
    output_format: str = "text",
    language: str | None = None,
    postprocess: str | None = None,
) -> int:
    """
    Process multiple audio files in batch.

    Args:
        file_paths: List of audio file paths
        output_dir: Optional output directory
        output_format: Output format for transcriptions
        language: ASR language code; None defers to config default
        postprocess: Optional AIPP profile name; applied per file.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    output_dir_path: Path | None = None
    if output_dir:
        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)

    success_count = 0
    error_count = 0

    for file_path_str in file_paths:
        file_path = Path(file_path_str)
        logger.info(f"\nProcessing: {file_path.name}")
        logger.info("-" * 40)

        try:
            transcription, duration = transcribe_audio_file(file_path_str, language=language)

            if transcription:
                if postprocess:
                    from .postprocess import postprocess_text

                    transcription = postprocess_text(transcription, postprocess)

                formatted_output = format_output(transcription, duration, output_format)

                if output_dir_path:
                    # Determine output file extension
                    if output_format == "json":
                        ext = ".json"
                    elif output_format == "srt":
                        ext = ".srt"
                    elif output_format == "vtt":
                        ext = ".vtt"
                    else:
                        ext = ".txt"

                    output_file = output_dir_path / f"{file_path.stem}{ext}"
                    output_file.write_text(formatted_output)
                    logger.info(f"Saved to: {output_file}")
                else:
                    print(formatted_output)  # Keep this as print for stdout output

                success_count += 1
            else:
                logger.warning("No speech detected")
                error_count += 1

        except Exception as e:
            logger.error(f"Error: {e}")
            error_count += 1

    logger.info(f"\n{'=' * 40}")
    logger.info("Batch processing complete:")
    logger.info(f"  Success: {success_count}")
    logger.info(f"  Errors: {error_count}")

    return 0 if error_count == 0 else 1
