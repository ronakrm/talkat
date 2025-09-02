#!/usr/bin/env python3

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests  # Added for HTTP calls

from .config import CODE_DEFAULTS, load_app_config, save_app_config
from .logging_config import get_logger
from .process_manager import ProcessManager, setup_signal_handlers
from .record import calibrate_microphone, stream_audio_with_vad

logger = get_logger(__name__)


def get_transcript_dir() -> Path:
    """Get or create the transcript directory."""
    config = load_app_config()
    transcript_dir_str = config.get(
        "transcript_dir", os.path.expanduser("~/.local/share/talkat/transcripts")
    )
    transcript_dir = Path(os.path.expanduser(transcript_dir_str))
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir


def save_transcript(text: str, mode: str = "short") -> Path:
    """Save transcript to a file with timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{mode}.txt"
    filepath = get_transcript_dir() / filename

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(text + "\n")

    return filepath


def copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard using wl-copy or xclip."""
    # Try wl-copy first (Wayland)
    try:
        subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Try xclip as fallback (X11)
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return False


def postprocess_transcript(
    transcript_path: Path, processor_command: str | None = None
) -> str | None:
    """
    Postprocess a transcript file using an external command or LLM interface.

    This is a placeholder for future functionality. The idea is to:
    1. Read the transcript file
    2. Pass it to a processor command (e.g., an LLM CLI tool)
    3. Return the processed output

    Example processor commands could be:
    - "llm prompt 'Clean up this transcript and format it as bullet points'"
    - "gpt-cli --system 'You are a helpful assistant' --prompt 'Summarize this text'"
    - Custom scripts that interface with local or remote LLMs

    The processor command should accept text via stdin and output to stdout.

    Future config options could include:
    - postprocess_enabled: bool
    - postprocess_command: str (the command to run)
    - postprocess_auto: bool (automatically process after dictation)
    - postprocess_prompts: dict (predefined prompts for different use cases)
    """
    if not processor_command:
        return None

    # Read the transcript
    try:
        with open(transcript_path, encoding="utf-8") as f:
            f.read()
    except OSError:
        return None

    # This would be implemented when needed:
    # try:
    #     result = subprocess.run(
    #         processor_command,
    #         input=transcript_text.encode('utf-8'),
    #         capture_output=True,
    #         shell=True,
    #         check=True
    #     )
    #     return result.stdout.decode('utf-8')
    # except subprocess.CalledProcessError:
    #     return None

    return None


def ensure_model_exists(model_path: str) -> bool:
    """Checks if the Vosk model exists and prints messages if not."""
    if not os.path.exists(model_path):
        logger.error(f"Model not found at {model_path}")
        logger.error("Please run the setup script to download the model.")
        with contextlib.suppress(FileNotFoundError):
            subprocess.run(["notify-send", "Talkat", "Model not found"], check=False)
        return False
    return True


def run_calibration_command(current_config: dict[str, Any]):
    """Runs microphone calibration and saves the threshold to the config file."""
    logger.info("Starting microphone calibration...")
    threshold = calibrate_microphone()

    config_to_save = (
        current_config.copy()
    )  # Start with current config (could be from CLI overrides)
    # Update it with the newly calibrated threshold
    config_to_save["silence_threshold"] = threshold

    save_app_config(config_to_save)
    logger.info(f"Calibration complete. Threshold set to: {threshold:.1f}")
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["notify-send", "Talkat", f"Calibration complete. Threshold: {threshold:.1f}"],
            check=False,
        )
    return 0


def run_listen_command(
    model_type: str,
    model_name: str,
    silence_threshold: float,
    model_cache_dir: str | None = None,
    fw_device: str = "cpu",
    fw_compute_type: str = "int8",
    fw_device_index: int | list[int] = 0,
    vosk_model_base_dir: str = "~/.local/share/vosk",  # New parameter for Vosk model base
) -> int:
    """Runs the main speech-to-text process by sending audio to a model server."""

    # Set up process management for toggle functionality
    import signal
    import threading

    pm = ProcessManager("listen")
    pm.write_pid(os.getpid())

    def cleanup_pid():
        pm.cleanup_pid_file()

    # Flag to signal recording should stop
    stop_recording = threading.Event()

    # Set up signal handler for graceful shutdown when toggled off
    def signal_handler(signum, frame):
        logger.info("\nRecording stopped by toggle command. Finishing transcription...")
        stop_recording.set()
        cleanup_pid()
        # The audio stream will stop naturally when it checks the flag

    signal.signal(signal.SIGINT, signal_handler)

    text = ""  # Initialize text variable

    # Model loading is now handled by the model_server.py
    # We only need to prepare and send the audio data.

    # current_threshold = load_threshold() # Old way
    # Use the passed silence_threshold
    current_threshold = silence_threshold
    if (
        current_threshold == CODE_DEFAULTS["silence_threshold"]
    ):  # Check if it's still the code default
        # Check if a config file value was different, or if it's just the plain default
        # This is mostly for informative message. The actual value is already correctly prioritised.
        loaded_file_conf_val = load_app_config().get("silence_threshold")
        if loaded_file_conf_val != CODE_DEFAULTS["silence_threshold"]:
            logger.info(f"Using calibrated threshold from config: {current_threshold:.1f}")
        else:
            logger.info(
                f"No calibrated threshold found in config or CLI. Using default: {current_threshold:.1f}"
            )
            logger.info("Run 'talkat calibrate' to set a custom threshold.")
    else:
        logger.info(f"Using threshold: {current_threshold:.1f} (from CLI or config)")

    try:
        # --- Streaming Implementation ---
        # record_audio_with_vad is now expected to be a generator:
        # 1. yields sample_rate (int)
        # 2. yields audio_chunk (bytes) ...
        # Stops when speech ends.
        audio_stream_generator_func = stream_audio_with_vad(
            silence_threshold=current_threshold,
            silence_duration=3.0,  # 3 seconds of silence before stopping
            debug=True,
        )

        # Get the sample rate first
        try:
            sample_rate = next(audio_stream_generator_func)
            if not isinstance(sample_rate, int):
                logger.error("record_audio_with_vad did not yield sample rate correctly.")
                # Fallback or error out if rate is not an int (e.g. None if no speech first chunk)
                # This also handles if the generator is empty (no speech detected from the start)
                logger.warning("No speech detected or audio input error.")
                with contextlib.suppress(FileNotFoundError):
                    subprocess.run(["notify-send", "Talkat", "No speech detected"], check=False)
                return 0  # Exit if no rate / no audio
        except StopIteration:  # Generator was empty
            logger.warning("No speech detected (empty audio stream).")
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(["notify-send", "Talkat", "No speech detected"], check=False)
            return 0

        logger.info(f"Speech detected. Streaming audio at {sample_rate} Hz to model server...")
        logger.info("(Run 'talkat listen' again to stop recording)")
        with contextlib.suppress(FileNotFoundError):
            subprocess.run(
                ["notify-send", "Talkat", 'Recording... Run "talkat listen" again to stop'],
                check=False,
            )

        def request_data_generator():
            # First, send metadata as a JSON string line
            metadata = {"rate": sample_rate}
            yield json.dumps(metadata).encode("utf-8") + b"\n"
            # Then, stream audio chunks from the modified record_audio_with_vad
            for audio_chunk in audio_stream_generator_func:
                if stop_recording.is_set():
                    logger.info("Stopping audio stream due to toggle command...")
                    break
                if audio_chunk:  # Ensure not None or empty bytes if VAD yields such
                    yield audio_chunk

        server_url = "http://127.0.0.1:5555/transcribe_stream"  # New streaming endpoint
        try:
            # Increased timeout for potentially long streaming and server processing
            response = requests.post(server_url, data=request_data_generator(), timeout=120)
            response.raise_for_status()

            response_json = response.json()
            text = response_json.get("text", "").strip()

        except requests.exceptions.ConnectionError:
            logger.error(f"Could not connect to the model server at {server_url}.")
            logger.error(
                "Please ensure the model server is running: python -m src.talkat.model_server"
            )
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(
                    ["notify-send", "Talkat", "Error: Model server not reachable."], check=False
                )
            return 1
        except requests.exceptions.Timeout:
            logger.error("Request to model server timed out.")
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(
                    ["notify-send", "Talkat", "Error: Model server timeout."], check=False
                )
            return 1
        except requests.exceptions.RequestException as e:
            logger.error(f"Error communicating with model server: {e}")
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(
                    ["notify-send", "Talkat", f"Server communication error: {e}"], check=False
                )
            return 1
        except json.JSONDecodeError:
            logger.error(f"Could not decode JSON response from server: {response.text}")
            return 1

        if text:
            logger.info(f"Recognized: {text}")

            # Save transcript for short mode if enabled
            config = load_app_config()
            if config.get("save_transcripts", True):
                transcript_path = save_transcript(text, mode="short")
                logger.info(f"Transcript saved to: {transcript_path}")

            try:
                subprocess.run(["ydotool", "type", "--key-delay=1", text], check=True)
                logger.info(f"Typed: {text}")
                with contextlib.suppress(FileNotFoundError):
                    subprocess.run(["notify-send", "Talkat", f"Typed: {text}"], check=False)
            except (subprocess.CalledProcessError, FileNotFoundError):
                logger.warning("ydotool not available, printing text instead:")
                print(f"TEXT: {text}")
                with contextlib.suppress(FileNotFoundError):
                    subprocess.run(["notify-send", "Talkat", f"Recognized: {text}"], check=False)
        else:
            logger.warning("No text recognized in the audio")
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(["notify-send", "Talkat", "No text recognized"], check=False)

        cleanup_pid()
        return 0

    except KeyboardInterrupt:
        logger.info("\nRecording interrupted.")
        cleanup_pid()
        return 0
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback

        traceback.print_exc()
        with contextlib.suppress(FileNotFoundError):
            subprocess.run(["notify-send", "Talkat", f"Error: {e}"], check=False)
        cleanup_pid()
        return 1


def run_long_dictation_command(
    model_type: str,
    model_name: str,
    silence_threshold: float,
    model_cache_dir: str | None = None,
    fw_device: str = "cpu",
    fw_compute_type: str = "int8",
    fw_device_index: int | list[int] = 0,
    vosk_model_base_dir: str = "~/.local/share/vosk",
    clipboard: bool = True,
):
    """Runs long dictation mode with continuous speech recognition."""
    # Set up process management for long dictation
    pm = ProcessManager("long_dictation")
    pm.write_pid(os.getpid())

    def cleanup_pid():
        pm.cleanup_pid_file()

    # Set up proper signal handling
    setup_signal_handlers(cleanup_func=cleanup_pid)

    # Use the passed silence_threshold
    current_threshold = silence_threshold
    if current_threshold == CODE_DEFAULTS["silence_threshold"]:
        loaded_file_conf_val = load_app_config().get("silence_threshold")
        if loaded_file_conf_val != CODE_DEFAULTS["silence_threshold"]:
            logger.info(f"Using calibrated threshold from config: {current_threshold:.1f}")
        else:
            logger.info(
                f"No calibrated threshold found in config or CLI. Using default: {current_threshold:.1f}"
            )
            logger.info("Run 'talkat calibrate' to set a custom threshold.")
    else:
        logger.info(f"Using threshold: {current_threshold:.1f} (from CLI or config)")

    # Create a single transcript file for this session
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript_filename = f"{timestamp}_long.txt"
    transcript_dir = get_transcript_dir()
    transcript_path = transcript_dir / transcript_filename

    logger.info("Starting long dictation mode. Press Ctrl+C to stop.")
    logger.info(f"Transcript directory: {transcript_dir}")
    logger.info(f"Transcript will be saved to: {transcript_path}")

    # Create empty file to ensure it exists
    transcript_path.touch()

    if clipboard:
        logger.info("Transcript will be copied to clipboard when finished.")

    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["notify-send", "Talkat", "Long dictation mode started. Press Ctrl+C to stop."],
            check=False,
        )

    full_transcript = []
    session = requests.Session()

    try:
        while True:  # Continue until interrupted
            # Use the existing stream_audio_with_vad for each utterance
            audio_stream_generator_func = stream_audio_with_vad(
                silence_threshold=0,  # No silence threshold for long mode
                debug=False,  # Less verbose for continuous mode
                max_duration=600.0,  # 10 minute timeout for long mode
            )

            # Get the sample rate first
            try:
                sample_rate = next(audio_stream_generator_func)
                if not isinstance(sample_rate, int):
                    logger.error("stream_audio_with_vad did not yield sample rate correctly.")
                    continue  # Try again for next utterance
            except StopIteration:
                # No speech detected in this cycle, wait a bit and try again
                time.sleep(0.1)
                continue

            # Collect audio data for this utterance
            def request_data_generator(rate, stream_gen):
                # First, send metadata as a JSON string line
                metadata = {"rate": rate}
                yield json.dumps(metadata).encode("utf-8") + b"\n"
                # Then, stream audio chunks
                for audio_chunk in stream_gen:
                    if audio_chunk:
                        yield audio_chunk

            server_url = "http://127.0.0.1:5555/transcribe_stream"
            try:
                response = session.post(
                    server_url,
                    data=request_data_generator(sample_rate, audio_stream_generator_func),
                    timeout=120,
                )
                response.raise_for_status()

                response_json = response.json()
                text = response_json.get("text", "").strip()

                if text:
                    logger.info(f"Recognized: {text}")
                    full_transcript.append(text)

                    # Save to file immediately
                    with open(transcript_path, "a", encoding="utf-8") as f:
                        f.write(text + " ")

                    # Don't type in long dictation mode
                    logger.debug("(Saved to transcript)")

            except requests.exceptions.ConnectionError:
                logger.error(f"Could not connect to the model server at {server_url}.")
                logger.error("Please ensure the model server is running: talkat server")
                with contextlib.suppress(FileNotFoundError):
                    subprocess.run(
                        ["notify-send", "Talkat", "Error: Model server not reachable."], check=False
                    )
                return 1
            except requests.exceptions.RequestException as e:
                logger.error(f"Error communicating with model server: {e}")
                continue  # Try next utterance
            except json.JSONDecodeError:
                logger.error("Could not decode JSON response from server")
                continue  # Try next utterance

    except KeyboardInterrupt:
        logger.info("\nLong dictation mode stopped.")

        # Close the session cleanly
        session.close()

        # Join all transcript parts
        full_text = " ".join(full_transcript)

        if full_text and clipboard:
            if copy_to_clipboard(full_text):
                logger.info("Transcript copied to clipboard!")
                with contextlib.suppress(FileNotFoundError):
                    subprocess.run(
                        ["notify-send", "Talkat", "Transcript copied to clipboard"], check=False
                    )
            else:
                logger.warning("Could not copy to clipboard (wl-copy or xclip not available)")

        logger.info(f"Full transcript saved to: {transcript_path}")
        logger.info(f"Total words: {len(full_text.split())}")

        with contextlib.suppress(FileNotFoundError):
            subprocess.run(
                [
                    "notify-send",
                    "Talkat",
                    f"Long dictation stopped. Saved to {transcript_filename}",
                ],
                check=False,
            )

        cleanup_pid()
        return 0
    except Exception as e:
        logger.error(f"Error in long dictation mode: {e}")
        import traceback

        traceback.print_exc()
        session.close()
        cleanup_pid()
        return 1
    finally:
        session.close()
        cleanup_pid()


def main(mode="listen"):
    # Load config (defaults updated by file)
    initial_config = load_app_config()

    parser = argparse.ArgumentParser(
        description="Talkat: Speech-to-text utility.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Set default command based on mode
    if mode == "calibrate":
        default_command = "calibrate"
    elif mode == "long":
        default_command = "long"
    else:
        default_command = "listen"
    parser.add_argument(
        "command",
        nargs="?",
        default=default_command,
        choices=["listen", "calibrate", "long"],
        help="Command to run.",
    )

    # Set defaults for argparse from the loaded configuration
    parser.add_argument(
        "--model_type",
        type=str,
        default=initial_config.get("model_type"),
        choices=["vosk", "faster-whisper"],
        help="Type of speech recognition model to use.",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default=initial_config.get("model_name"),
        help='Name of the model. For Vosk, e.g., "model-en". For faster-whisper, e.g., "tiny.en".',
    )
    parser.add_argument(
        "--silence_threshold",
        type=float,
        default=initial_config.get("silence_threshold"),
        help="Silence threshold for VAD.",
    )
    parser.add_argument(
        "--vosk_model_base_dir",
        type=str,
        default=initial_config.get("vosk_model_base_dir"),
        help="Base directory for Vosk models.",
    )

    # Faster-whisper specific arguments
    parser.add_argument(
        "--faster_whisper_model_cache_dir",
        type=str,
        default=initial_config.get("faster_whisper_model_cache_dir"),
        help="Optional: Custom cache directory for faster-whisper models.",
    )
    parser.add_argument(
        "--fw_device",
        type=str,
        default=initial_config.get("fw_device"),
        help='Device for faster-whisper (e.g., "cpu", "cuda").',
    )
    parser.add_argument(
        "--fw_compute_type",
        type=str,
        default=initial_config.get("fw_compute_type"),
        help='Compute type for faster-whisper (e.g., "int8", "float16").',
    )

    def parse_device_index_str(value_str: str) -> int | list[int]:
        if not value_str.strip():
            raise argparse.ArgumentTypeError(
                f"fw_device_index string cannot be empty if provided. Got '{value_str}'"
            )
        if "," in value_str:
            try:
                return [int(x.strip()) for x in value_str.split(",")]
            except ValueError:
                raise argparse.ArgumentTypeError(
                    f"Invalid format for multi-value fw_device_index: '{value_str}'. Must be comma-separated integers."
                ) from None
        else:
            try:
                return int(value_str)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    f"Invalid integer for fw_device_index: '{value_str}'."
                ) from None

    parser.add_argument(
        "--fw_device_index",
        type=parse_device_index_str,  # Converts CLI string to int or List[int]
        default=initial_config.get("fw_device_index"),  # Actual default value, not a string
        help='Device index for faster-whisper (e.g., 0 or "0,1" for multi-GPU).',
    )

    # Transcript and clipboard arguments
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="Disable clipboard copy for long dictation mode.",
    )
    parser.add_argument(
        "--no-save-transcripts", action="store_true", help="Disable saving transcripts to files."
    )
    parser.add_argument(
        "--transcript-dir",
        type=str,
        default=initial_config.get("transcript_dir"),
        help="Directory to save transcripts.",
    )

    args = parser.parse_args()

    # After parsing, args contains the final values following CLI > File > Code_Default hierarchy
    # For calibration, we pass the resolved 'args' to carry over any CLI overrides to be saved.
    # However, run_calibration_command should ideally work with the config state prior to *its own* CLI args.
    # Let's adjust: run_calibration_command should operate on the initial_config, update it, and save.
    # Any CLI args for *calibration itself* (if we had them) would be different.
    # The current `args` reflects the final state for *running commands*.

    if args.command == "calibrate":
        # For saving, we want to preserve other settings from config/defaults,
        # and only update the threshold.
        # `initial_config` has defaults + file. CLI args for general settings are in `args`.
        # We want to save a config that reflects the state if `listen` was run with these CLI args.
        config_for_saving = initial_config.copy()
        config_for_saving.update(vars(args))  # Apply CLI overrides to what we'll save
        # Remove 'command' as it's not a persistent config
        if "command" in config_for_saving:
            del config_for_saving["command"]

        return run_calibration_command(config_for_saving)  # Pass the effectively resolved config
    elif args.command == "listen":
        return run_listen_command(
            model_type=args.model_type,
            model_name=args.model_name,
            silence_threshold=args.silence_threshold,
            model_cache_dir=args.faster_whisper_model_cache_dir,
            fw_device=args.fw_device,
            fw_compute_type=args.fw_compute_type,
            fw_device_index=args.fw_device_index,
            vosk_model_base_dir=args.vosk_model_base_dir,
        )
    elif args.command == "long":
        # Determine clipboard setting
        clipboard_enabled = initial_config.get("clipboard_on_long", True) and not args.no_clipboard

        return run_long_dictation_command(
            model_type=args.model_type,
            model_name=args.model_name,
            silence_threshold=args.silence_threshold,
            model_cache_dir=args.faster_whisper_model_cache_dir,
            fw_device=args.fw_device,
            fw_compute_type=args.fw_compute_type,
            fw_device_index=args.fw_device_index,
            vosk_model_base_dir=args.vosk_model_base_dir,
            clipboard=clipboard_enabled,
        )
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
