#!/usr/bin/env python3

import argparse
import os
import sys
from typing import Any

from .logging_config import get_logger, setup_logging
from .paths import ensure_user_directories
from .process_manager import LockTimeout, ProcessManager

logger = get_logger(__name__)


def get_listen_pid() -> int | None:
    """Return the PID of the running listen process, or None."""
    pm = ProcessManager("listen")
    is_running, pid = pm.is_running()
    return pid if is_running else None


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Collect non-None CLI config overrides keyed by their config name."""
    mapping: dict[str, Any] = {
        "max_recording_duration": getattr(args, "max_recording", None),
        "http_timeout": getattr(args, "http_timeout", None),
        "language": getattr(args, "language", None),
    }
    return {k: v for k, v in mapping.items() if v is not None}


def _run_model_command(args: argparse.Namespace) -> int:
    """Dispatch ``talkat model {list,download,use}``.

    Returns an int suitable for ``sys.exit``. Stays out of the main parser
    body so main() doesn't grow another inline branch.
    """
    from .model_manager import (
        ModelManagerError,
        download_model,
        format_size,
        known_model_names,
        list_models,
        use_model,
    )

    sub = getattr(args, "model_command", None)
    if sub is None:
        logger.info("Usage: talkat model {list,download,use} ...")
        logger.info(f"Known model sizes: {', '.join(known_model_names())}")
        return 1

    if sub == "list":
        models = list_models()
        if not models:
            logger.info("No faster-whisper models installed yet.")
            logger.info("Download one with: talkat model download <name>")
            logger.info(f"Known sizes: {', '.join(known_model_names())}")
            return 0
        # Plain print() so users can pipe into wc/grep without log noise.
        print(f"{'NAME':<24} {'SIZE':>10}  PATH")
        for m in models:
            print(f"{m.name:<24} {format_size(m.size_bytes):>10}  {m.path}")
        return 0

    if sub == "download":
        try:
            path = download_model(args.name)
        except ModelManagerError as e:
            logger.error(str(e))
            return 1
        logger.info(f"Model {args.name!r} ready at {path}")
        return 0

    if sub == "use":
        try:
            config_file, cached = use_model(args.name)
        except (ValueError, ModelManagerError) as e:
            logger.error(str(e))
            return 1
        logger.info(f"Default model set to {args.name!r} in {config_file}")
        if cached:
            logger.info("Restart the model server to pick up the change.")
        return 0

    logger.error(f"Unknown model subcommand: {sub}")
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
        help="Enable debug mode (verbose logging)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    listen_parser = subparsers.add_parser(
        "listen",
        help="Toggle dictation - starts if not running, stops if already recording",
    )
    listen_parser.add_argument(
        "-o", "--output", help="Save transcription to file instead of typing to screen"
    )
    listen_parser.add_argument(
        "--to-file",
        action="store_true",
        help="Append each piece to the transcript file as it is recognized and copy the "
        "whole transcript to the clipboard at the end, instead of typing (long-form "
        "note taking). Use -o to choose the file.",
    )
    listen_parser.add_argument(
        "--try-lock",
        action="store_true",
        help="Refuse to toggle/start if another talkat command is in progress or "
        "a listen is already running (instead of waiting or stopping it).",
    )
    listen_parser.add_argument(
        "--max-recording",
        type=float,
        metavar="SECONDS",
        help="Cap on one recording (overrides max_recording_duration).",
    )
    listen_parser.add_argument(
        "--http-timeout",
        type=float,
        metavar="SECONDS",
        help="Per-request HTTP timeout against the model server (overrides http_timeout).",
    )
    listen_parser.add_argument(
        "--language",
        type=str,
        metavar="CODE",
        help="ASR language code (e.g. 'en', 'es', 'auto'). Overrides config 'language'.",
    )
    listen_parser.add_argument(
        "--postprocess",
        type=str,
        metavar="PROFILE",
        help="Pipe the transcript through the named AIPP profile from config "
        "(see 'postprocess_profiles'). Falls back to raw transcript on any error.",
    )

    subparsers.add_parser("server", help="Start the model server")
    subparsers.add_parser("calibrate", help="Calibrate microphone threshold")
    subparsers.add_parser(
        "doctor",
        help="Check the environment: install origin, PATH/unit shadowing, "
        "server health, audio devices, desktop tools",
    )

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
    file_parser.add_argument(
        "--language",
        type=str,
        metavar="CODE",
        help="ASR language code (e.g. 'en', 'es', 'auto'). Overrides config 'language'.",
    )
    file_parser.add_argument(
        "--postprocess",
        type=str,
        metavar="PROFILE",
        help="Pipe the transcription through the named AIPP profile before output.",
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
    batch_parser.add_argument(
        "--language",
        type=str,
        metavar="CODE",
        help="ASR language code (e.g. 'en', 'es', 'auto'). Overrides config 'language'.",
    )
    batch_parser.add_argument(
        "--postprocess",
        type=str,
        metavar="PROFILE",
        help="Pipe each file's transcription through the named AIPP profile before output.",
    )

    model_parser = subparsers.add_parser(
        "model",
        help="Manage faster-whisper models (list, download, use)",
    )
    model_sub = model_parser.add_subparsers(dest="model_command", required=False)
    model_sub.add_parser("list", help="Show installed faster-whisper models + sizes")
    model_download = model_sub.add_parser(
        "download",
        help="Download a faster-whisper model into the local cache",
    )
    model_download.add_argument(
        "name",
        help="Model name (e.g. 'small.en') or HuggingFace repo id (e.g. 'org/repo')",
    )
    model_use = model_sub.add_parser(
        "use", help="Set the default faster-whisper model in user config"
    )
    model_use.add_argument(
        "name",
        help="Model name (e.g. 'small.en') or HuggingFace repo id (e.g. 'org/repo')",
    )

    args = parser.parse_args()

    # Create user directories on first real invocation (not at import time).
    ensure_user_directories()

    env_debug = os.environ.get("TALKAT_DEBUG") == "1"
    setup_logging(verbose=args.verbose or args.debug or env_debug, quiet=args.quiet)

    if args.command == "listen":
        pm = ProcessManager("listen")
        try:
            with pm.locked(try_only=args.try_lock):
                is_running, pid = pm.is_running()
                if is_running:
                    if args.try_lock:
                        logger.error(f"talkat listen is already running (PID: {pid}); refusing.")
                        sys.exit(1)
                    logger.info(f"Stopping active recording (PID: {pid})...")
                    sys.exit(0 if pm.stop_process() else 1)
                # Claim the slot under the lock so a concurrent `talkat listen`
                # will either toggle us off or refuse to start — never start a
                # second listen alongside us.
                pm.write_pid(os.getpid())
        except LockTimeout as e:
            logger.error(str(e))
            sys.exit(1)
        from .main import run_dictation

        sys.exit(
            run_dictation(
                output_file=args.output,
                to_file=args.to_file,
                config_overrides=_overrides_from_args(args),
                postprocess=args.postprocess,
            )
        )
    elif args.command == "server":
        from .model_server import main as server_main

        server_main()
    elif args.command == "calibrate":
        from .main import run_calibrate

        sys.exit(run_calibrate())
    elif args.command == "doctor":
        from .doctor import run_doctor

        sys.exit(run_doctor())
    elif args.command == "install-service":
        from .service import install_service

        sys.exit(install_service())
    elif args.command == "uninstall-service":
        from .service import uninstall_service

        sys.exit(uninstall_service())
    elif args.command == "model":
        sys.exit(_run_model_command(args))
    elif args.command == "file":
        from .file_processor import process_audio_file_command

        sys.exit(
            process_audio_file_command(
                args.input,
                args.output,
                args.format,
                args.clipboard,
                language=args.language,
                postprocess=args.postprocess,
            )
        )
    elif args.command == "batch":
        from .file_processor import batch_process_files

        sys.exit(
            batch_process_files(
                args.files,
                args.output_dir,
                args.format,
                language=args.language,
                postprocess=args.postprocess,
            )
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
