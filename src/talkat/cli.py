#!/usr/bin/env python3

import argparse
import contextlib
import os
import subprocess
import sys

from .file_processor import batch_process_files, process_audio_file_command
from .logging_config import get_logger, setup_logging
from .paths import ensure_user_directories
from .process_manager import ProcessManager
from .security import safe_subprocess_run

logger = get_logger(__name__)


def get_long_pid() -> int | None:
    """Return the PID of the running long-dictation process, or None."""
    pm = ProcessManager("long_dictation")
    is_running, pid = pm.is_running()
    return pid if is_running else None


def get_listen_pid() -> int | None:
    """Return the PID of the running listen process, or None."""
    pm = ProcessManager("listen")
    is_running, pid = pm.is_running()
    return pid if is_running else None


def _start_long(pm: ProcessManager, debug: bool) -> int:
    """Start the long-dictation background process. Caller holds the pm lock."""
    cmd = [sys.executable, "-m", "talkat.cli", "long", "--background"]

    env = None
    if debug:
        env = os.environ.copy()
        env["TALKAT_DEBUG"] = "1"

    pid = pm.start_background_process(cmd, debug=debug, env=env)
    if pid:
        logger.info(f"Long dictation started in background (PID: {pid})")
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(
                ["notify-send", "Talkat", "Long dictation started"],
                check=False,
                stderr=subprocess.DEVNULL,
            )
        return 0
    logger.error("Failed to start long dictation")
    return 1


def _stop_long(pm: ProcessManager) -> int:
    """Stop the long-dictation background process. Caller holds the pm lock."""
    if pm.stop_process():
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(
                ["notify-send", "Talkat", "Long dictation stopped"],
                check=False,
                stderr=subprocess.DEVNULL,
            )
        return 0
    return 1


def start_long_background(debug: bool = False) -> int:
    """Start long dictation in background. Refuses if already running."""
    pm = ProcessManager("long_dictation")
    try:
        with pm:
            if get_long_pid():
                logger.info("Long dictation is already running.")
                return 1
            return _start_long(pm, debug)
    except RuntimeError as e:
        logger.error(str(e))
        return 1


def stop_long_background() -> int:
    """Stop the background long-dictation process."""
    pm = ProcessManager("long_dictation")
    try:
        with pm:
            return _stop_long(pm)
    except RuntimeError as e:
        logger.error(str(e))
        return 1


def toggle_long_background(debug: bool = False) -> int:
    """Toggle long dictation — start if stopped, stop if running."""
    pm = ProcessManager("long_dictation")
    try:
        with pm:
            is_running, _ = pm.is_running()
            return _stop_long(pm) if is_running else _start_long(pm, debug)
    except RuntimeError as e:
        logger.error(str(e))
        return 1


def stop_listen_process() -> int:
    """Stop the running listen process."""
    pm = ProcessManager("listen")
    try:
        with pm:
            if pm.stop_process():
                with contextlib.suppress(FileNotFoundError):
                    safe_subprocess_run(
                        ["notify-send", "Talkat", "Recording stopped, transcribing..."],
                        check=False,
                        stderr=subprocess.DEVNULL,
                    )
                return 0
            logger.info("No active listen process found.")
            return 1
    except RuntimeError as e:
        logger.error(str(e))
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Talkat - Voice Command System")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress all but ERROR messages"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (verbose logging + background process output)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    listen_parser = subparsers.add_parser(
        "listen",
        help="Toggle voice recording - starts if not running, stops if already recording",
    )
    listen_parser.add_argument(
        "-o", "--output", help="Save transcription to file instead of typing to screen"
    )

    long_parser = subparsers.add_parser(
        "long", help="Start long dictation mode (continuous recording)"
    )
    long_parser.add_argument(
        "-o", "--output", help="Save transcription to specified file instead of default location"
    )
    long_parser.add_argument(
        "--background",
        action="store_true",
        help=argparse.SUPPRESS,  # Internal flag used by start-long/toggle-long
    )
    long_parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="Disable clipboard copy when long dictation ends.",
    )

    subparsers.add_parser("start-long", help="Start long dictation in background")
    subparsers.add_parser("stop-long", help="Stop background long dictation")
    subparsers.add_parser(
        "toggle-long", help="Toggle long dictation (start if stopped, stop if running)"
    )

    subparsers.add_parser("server", help="Start the model server")
    subparsers.add_parser("calibrate", help="Calibrate microphone threshold")

    subparsers.add_parser(
        "install-service",
        help="Install + start the talkat user systemd service",
    )
    subparsers.add_parser(
        "uninstall-service",
        help="Stop + remove the talkat user systemd service",
    )

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

    # Create user directories on first real invocation (not at import time).
    ensure_user_directories()

    env_debug = os.environ.get("TALKAT_DEBUG") == "1"
    setup_logging(verbose=args.verbose or args.debug or env_debug, quiet=args.quiet)

    if args.command == "listen":
        existing_pid = get_listen_pid()
        if existing_pid:
            logger.info(f"Stopping active recording (PID: {existing_pid})...")
            sys.exit(stop_listen_process())
        from .main import listen_once

        sys.exit(listen_once(output_file=args.output))
    elif args.command == "long":
        from .config import load_app_config
        from .main import listen_continuous

        config = load_app_config()
        clipboard_enabled = config.get("clipboard_on_long", True) and not args.no_clipboard
        sys.exit(
            listen_continuous(
                output_file=args.output,
                background=args.background,
                clipboard=clipboard_enabled,
            )
        )
    elif args.command == "start-long":
        sys.exit(start_long_background(debug=args.debug))
    elif args.command == "stop-long":
        sys.exit(stop_long_background())
    elif args.command == "toggle-long":
        sys.exit(toggle_long_background(debug=args.debug))
    elif args.command == "server":
        from .model_server import main as server_main

        server_main()
    elif args.command == "calibrate":
        from .main import run_calibrate

        sys.exit(run_calibrate())
    elif args.command == "install-service":
        from .service import install_service

        sys.exit(install_service())
    elif args.command == "uninstall-service":
        from .service import uninstall_service

        sys.exit(uninstall_service())
    elif args.command == "file":
        sys.exit(process_audio_file_command(args.input, args.output, args.format, args.clipboard))
    elif args.command == "batch":
        sys.exit(batch_process_files(args.files, args.output_dir, args.format))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
