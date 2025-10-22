"""Audio file processing module for transcribing audio files directly."""

import json
import sys
from pathlib import Path

import requests

from .logging_config import get_logger
from .security import safe_subprocess_run, validate_file_path

logger = get_logger(__name__)


def transcribe_audio_file(
    file_path: str, server_url: str | None = None, output_format: str = "text"
) -> tuple[str, float]:
    """
    Transcribe an audio file using the model server.

    Args:
        file_path: Path to the audio file
        server_url: URL of the model server (uses config default if None)
        output_format: Output format (text, json, srt, vtt)

    Returns:
        Tuple of (transcription, duration in seconds)
    """
    # Load configuration
    from .config import CODE_DEFAULTS, load_app_config
    config = load_app_config()
    
    # Use configured server URL if not provided
    if server_url is None:
        server_url = config.get("server_url", CODE_DEFAULTS["server_url"])
    
    # Validate and sanitize file path
    try:
        file_path_obj = validate_file_path(file_path, must_exist=True)
    except Exception as e:
        raise FileNotFoundError(f"Invalid audio file: {e}") from e

    # Check file extension
    supported_formats = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".mp4", ".webm"}
    if file_path_obj.suffix.lower() not in supported_formats:
        raise ValueError(f"Unsupported file format: {file_path_obj.suffix}")

    try:
        import librosa
        import soundfile  # noqa: F401 - test for availability
    except ImportError:
        logger.error("librosa and soundfile are required for file processing")
        logger.error("Install them with: pip install librosa soundfile")
        sys.exit(1)

    logger.info(f"Loading audio file: {file_path_obj}")

    try:
        # Load audio file and resample to 16kHz
        audio_data, sample_rate = librosa.load(str(file_path_obj), sr=16000, mono=True)
        duration = len(audio_data) / sample_rate

        logger.info(f"Audio loaded: {duration:.1f} seconds at {sample_rate} Hz")

        # Check if server is running
        try:
            health_response = requests.get(
                f"{server_url}/health", 
                timeout=config.get("health_check_timeout", CODE_DEFAULTS["health_check_timeout"])
            )
            if health_response.status_code != 200:
                logger.error("Model server is not ready")
                sys.exit(1)
        except requests.ConnectionError:
            logger.error("Model server is not running")
            logger.error("Start it with: talkat server")
            sys.exit(1)

        # Send file to server for transcription
        with open(file_path_obj, "rb") as f:
            files = {"audio": (file_path_obj.name, f, "audio/*")}
            response = requests.post(
                f"{server_url}/transcribe_file",
                files=files,
                timeout=max(
                    config.get("file_processing_timeout_base", CODE_DEFAULTS["file_processing_timeout_base"]), 
                    duration * 2
                ),  # Dynamic timeout based on duration
            )

        if response.status_code != 200:
            error_msg = response.json().get("error", "Unknown error")
            logger.error(f"Error transcribing file: {error_msg}")
            sys.exit(1)

        result = response.json()
        transcription = result.get("text", "").strip()

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
) -> int:
    """
    Process an audio file and output the transcription.

    Args:
        file_path: Path to the audio file
        output_file: Optional output file path
        output_format: Output format (text, json, srt, vtt)
        clipboard: Whether to copy to clipboard

    Returns:
        Exit code (0 for success, 1 for error)
    """
    try:
        # Transcribe the file
        transcription, duration = transcribe_audio_file(file_path)

        if not transcription:
            logger.warning("No speech detected in the audio file")
            return 1

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

        # Copy to clipboard if requested
        if clipboard:
            try:
                import subprocess

                # Try wl-copy first (Wayland)
                try:
                    safe_subprocess_run(
                        ["wl-copy"],
                        input=transcription.encode("utf-8"),
                        check=True,
                        capture_output=True,
                    )
                    logger.info("Transcription copied to clipboard")
                except (subprocess.CalledProcessError, FileNotFoundError):
                    # Fallback to xclip (X11)
                    try:
                        safe_subprocess_run(
                            ["xclip", "-selection", "clipboard"],
                            input=transcription.encode("utf-8"),
                            check=True,
                            capture_output=True,
                        )
                        logger.info("Transcription copied to clipboard")
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        logger.warning("Could not copy to clipboard (wl-copy or xclip not found)")
            except Exception as e:
                logger.warning(f"Could not copy to clipboard: {e}")

        # Show summary
        logger.info("\nSummary:")
        logger.info(f"  Duration: {duration:.1f} seconds")
        logger.info(f"  Words: {len(transcription.split())}")
        logger.info(f"  Characters: {len(transcription)}")

        return 0

    except Exception as e:
        logger.error(f"Error: {e}")
        return 1


def batch_process_files(
    file_paths: list[str], output_dir: str | None = None, output_format: str = "text"
) -> int:
    """
    Process multiple audio files in batch.

    Args:
        file_paths: List of audio file paths
        output_dir: Optional output directory
        output_format: Output format for transcriptions

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
            transcription, duration = transcribe_audio_file(file_path_str)

            if transcription:
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
