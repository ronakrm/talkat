#!/usr/bin/env python3

import argparse
import contextlib
import sys

from .file_processor import batch_process_files, process_audio_file_command
from .logging_config import get_logger, setup_logging
from .main import main as client_main
from .model_server import main as server_main
from .process_manager import ProcessManager
from .security import safe_subprocess_run

logger = get_logger(__name__)


def get_long_pid() -> int | None:
    """Get the PID of the running long dictation process."""
    pm = ProcessManager("long_dictation")
    is_running, pid = pm.is_running()
    return pid if is_running else None


def start_long_background() -> int:
    """Start long dictation in background."""
    pm = ProcessManager("long_dictation")

    with pm:
        if get_long_pid():
            logger.info("Long dictation is already running.")
            return 1

        pid = pm.start_background_process([sys.executable, "-m", "talkat.cli", "long"])

        if pid:
            logger.info(f"Long dictation started in background (PID: {pid})")
            with contextlib.suppress(FileNotFoundError):
                safe_subprocess_run(
                    ["notify-send", "Talkat", "Long dictation started"], check=False
                )
            return 0
        else:
            logger.error("Failed to start long dictation")
            return 1


def stop_long_background() -> int:
    """Stop the background long dictation process."""
    pm = ProcessManager("long_dictation")

    with pm:
        if pm.stop_process():
            with contextlib.suppress(FileNotFoundError):
                safe_subprocess_run(
                    ["notify-send", "Talkat", "Long dictation stopped"], check=False
                )
            return 0
        else:
            return 1


def toggle_long_background() -> int:
    """Toggle long dictation - start if stopped, stop if running."""
    pm = ProcessManager("long_dictation")
    is_running, _ = pm.is_running()

    if is_running:
        return stop_long_background()
    else:
        return start_long_background()


def get_listen_pid() -> int | None:
    """Get the PID of the running listen process."""
    pm = ProcessManager("listen")
    is_running, pid = pm.is_running()
    return pid if is_running else None


def stop_listen_process() -> int:
    """Stop the running listen process."""
    pm = ProcessManager("listen")

    with pm:
        if pm.stop_process():
            with contextlib.suppress(FileNotFoundError):
                safe_subprocess_run(
                    ["notify-send", "Talkat", "Recording stopped, transcribing..."], check=False
                )
            return 0
        else:
            logger.info("No active listen process found.")
            return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Talkat - Voice Command System")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress all but ERROR messages"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Client mode (listen)
    subparsers.add_parser(
        "listen", help="Toggle voice recording - starts if not running, stops if already recording"
    )

    # Long dictation mode
    subparsers.add_parser("long", help="Start long dictation mode (continuous recording)")

    # Start long dictation in background
    subparsers.add_parser("start-long", help="Start long dictation in background")

    # Stop long dictation
    subparsers.add_parser("stop-long", help="Stop background long dictation")

    # Toggle long dictation
    subparsers.add_parser(
        "toggle-long", help="Toggle long dictation (start if stopped, stop if running)"
    )

    # Server mode
    subparsers.add_parser("server", help="Start the model server")

    # Calibration mode
    subparsers.add_parser("calibrate", help="Calibrate microphone threshold")

    # File transcription mode
    file_parser = subparsers.add_parser("file", help="Transcribe an audio file")
    file_parser.add_argument("input", help="Path to audio file (.wav, .mp3, .flac, etc.)")
    file_parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    file_parser.add_argument(
        "-f",
        "--format",
        choices=["text", "json", "srt", "vtt"],
        default="text",
        help="Output format (default: text)",
    )
    file_parser.add_argument(
        "-c", "--clipboard", action="store_true", help="Copy transcription to clipboard"
    )

    # Batch file processing
    batch_parser = subparsers.add_parser("batch", help="Process multiple audio files")
    batch_parser.add_argument("files", nargs="+", help="Audio files to process")
    batch_parser.add_argument("-o", "--output-dir", help="Output directory for transcriptions")
    batch_parser.add_argument(
        "-f",
        "--format",
        choices=["text", "json", "srt", "vtt"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args()

    # Setup logging based on verbosity flags
    setup_logging(verbose=args.verbose, quiet=args.quiet)

    if args.command == "listen":
        # Check if a listen process is already running
        existing_pid = get_listen_pid()
        if existing_pid:
            # Stop the existing process
            logger.info(f"Stopping active recording (PID: {existing_pid})...")
            sys.exit(stop_listen_process())
        else:
            # Start a new listen process
            client_main()
    elif args.command == "long":
        client_main(mode="long")
    elif args.command == "start-long":
        sys.exit(start_long_background())
    elif args.command == "stop-long":
        sys.exit(stop_long_background())
    elif args.command == "toggle-long":
        sys.exit(toggle_long_background())
    elif args.command == "server":
        server_main()
    elif args.command == "calibrate":
        # Run calibration through main with calibrate mode
        client_main(mode="calibrate")
    elif args.command == "file":
        sys.exit(process_audio_file_command(args.input, args.output, args.format, args.clipboard))
    elif args.command == "batch":
        sys.exit(batch_process_files(args.files, args.output_dir, args.format))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
