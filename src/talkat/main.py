#!/usr/bin/env python3

import json
import subprocess
import os
import sys
import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Union, List

import vosk
from faster_whisper import WhisperModel
import numpy as np
import requests # Added for HTTP calls
import base64   # Added for encoding audio data

from .record import record_audio_with_vad, calibrate_microphone, stream_audio_with_vad
from .config import CODE_DEFAULTS, load_app_config, save_app_config

def get_transcript_dir() -> Path:
    """Get or create the transcript directory."""
    config = load_app_config()
    transcript_dir_str = config.get('transcript_dir', os.path.expanduser("~/.local/share/talkat/transcripts"))
    transcript_dir = Path(os.path.expanduser(transcript_dir_str))
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir

def save_transcript(text: str, mode: str = "short") -> Path:
    """Save transcript to a file with timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{mode}.txt"
    filepath = get_transcript_dir() / filename
    
    with open(filepath, 'a', encoding='utf-8') as f:
        f.write(text + '\n')
    
    return filepath

def copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard using wl-copy or xclip."""
    # Try wl-copy first (Wayland)
    try:
        subprocess.run(['wl-copy'], input=text.encode('utf-8'), check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    # Try xclip as fallback (X11)
    try:
        subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode('utf-8'), check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    return False

def postprocess_transcript(transcript_path: Path, processor_command: Optional[str] = None) -> Optional[str]:
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
        with open(transcript_path, 'r', encoding='utf-8') as f:
            transcript_text = f.read()
    except IOError:
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
        print(f"Model not found at {model_path}")
        print("Please run the setup script to download the model.")
        try:
            subprocess.run(['notify-send', 'Talkat', 'Model not found'], check=False)
        except FileNotFoundError:
            pass
        return False
    return True

def run_calibration_command(current_config: Dict[str, Any]):
    """Runs microphone calibration and saves the threshold to the config file."""
    print("Starting microphone calibration...")
    threshold = calibrate_microphone()
    
    config_to_save = current_config.copy() # Start with current config (could be from CLI overrides)
    # Update it with the newly calibrated threshold
    config_to_save['silence_threshold'] = threshold 
    
    save_app_config(config_to_save)
    print(f"Calibration complete. Threshold set to: {threshold:.1f}")
    try:
        subprocess.run(['notify-send', 'Talkat', f'Calibration complete. Threshold: {threshold:.1f}'], check=False)
    except FileNotFoundError:
        pass
    return 0

def run_listen_command(
    model_type: str, 
    model_name: str, 
    silence_threshold: float,
    model_cache_dir: Optional[str] = None,
    fw_device: str = "cpu",
    fw_compute_type: str = "int8",
    fw_device_index: Union[int, List[int]] = 0,
    vosk_model_base_dir: str = "~/.local/share/vosk" # New parameter for Vosk model base
):
    """Runs the main speech-to-text process by sending audio to a model server."""
    
    text = "" # Initialize text variable
    
    # Model loading is now handled by the model_server.py
    # We only need to prepare and send the audio data.

    # current_threshold = load_threshold() # Old way
    # Use the passed silence_threshold
    current_threshold = silence_threshold
    if current_threshold == CODE_DEFAULTS['silence_threshold']: # Check if it's still the code default
        # Check if a config file value was different, or if it's just the plain default
        # This is mostly for informative message. The actual value is already correctly prioritised.
        loaded_file_conf_val = load_app_config().get('silence_threshold')
        if loaded_file_conf_val != CODE_DEFAULTS['silence_threshold']:
             print(f"Using calibrated threshold from config: {current_threshold:.1f}")
        else:
            print(f"No calibrated threshold found in config or CLI. Using default: {current_threshold:.1f}")
            print(f"Run 'talkat calibrate' to set a custom threshold.")
    else:
         print(f"Using threshold: {current_threshold:.1f} (from CLI or config)")
    
    try:
        # --- Streaming Implementation --- 
        # record_audio_with_vad is now expected to be a generator:
        # 1. yields sample_rate (int)
        # 2. yields audio_chunk (bytes) ...
        # Stops when speech ends.
        audio_stream_generator_func = stream_audio_with_vad(
            silence_threshold=current_threshold, 
            silence_duration=3.0,  # 3 seconds of silence before stopping
            debug=True
        )

        # Get the sample rate first
        try:
            sample_rate = next(audio_stream_generator_func)
            if not isinstance(sample_rate, int):
                print("Error: record_audio_with_vad did not yield sample rate correctly.")
                # Fallback or error out if rate is not an int (e.g. None if no speech first chunk)
                # This also handles if the generator is empty (no speech detected from the start)
                print("No speech detected or audio input error.")
                try:
                    subprocess.run(['notify-send', 'Talkat', 'No speech detected'], check=False)
                except FileNotFoundError:
                    pass
                return 0 # Exit if no rate / no audio
        except StopIteration: # Generator was empty
            print("No speech detected (empty audio stream).")
            try:
                subprocess.run(['notify-send', 'Talkat', 'No speech detected'], check=False)
            except FileNotFoundError:
                pass
            return 0

        print(f"Speech detected. Streaming audio at {sample_rate} Hz to model server...")
        try:
            subprocess.run(['notify-send', 'Talkat', 'Streaming to server...'], check=False)
        except FileNotFoundError:
            pass

        def request_data_generator():
            # First, send metadata as a JSON string line
            metadata = {"rate": sample_rate}
            yield json.dumps(metadata).encode('utf-8') + b'\n'
            # Then, stream audio chunks from the modified record_audio_with_vad
            for audio_chunk in audio_stream_generator_func:
                if audio_chunk: # Ensure not None or empty bytes if VAD yields such
                    yield audio_chunk
        
        server_url = "http://127.0.0.1:5555/transcribe_stream" # New streaming endpoint
        try:
            # Increased timeout for potentially long streaming and server processing
            response = requests.post(server_url, data=request_data_generator(), timeout=120) # type: ignore[arg-type]
            response.raise_for_status()
            
            response_json = response.json()
            text = response_json.get('text', '').strip()
            
        except requests.exceptions.ConnectionError:
            print(f"Error: Could not connect to the model server at {server_url}.")
            print("Please ensure the model server is running: python -m src.talkat.model_server")
            try:
                subprocess.run(['notify-send', 'Talkat', 'Error: Model server not reachable.'], check=False)
            except FileNotFoundError:
                pass
            return 1
        except requests.exceptions.Timeout:
            print(f"Error: Request to model server timed out.")
            try:
                subprocess.run(['notify-send', 'Talkat', 'Error: Model server timeout.'], check=False)
            except FileNotFoundError:
                pass
            return 1
        except requests.exceptions.RequestException as e:
            print(f"Error communicating with model server: {e}")
            try:
                subprocess.run(['notify-send', 'Talkat', f'Server communication error: {e}'], check=False)
            except FileNotFoundError:
                pass
            return 1
        except json.JSONDecodeError:
            print(f"Error: Could not decode JSON response from server: {response.text}")
            return 1

        if text:
            print(f"Recognized: {text}")
            
            # Save transcript for short mode if enabled
            config = load_app_config()
            if config.get('save_transcripts', True):
                transcript_path = save_transcript(text, mode="short")
                print(f"Transcript saved to: {transcript_path}")
            
            try:
                subprocess.run(['ydotool', 'type', '--key-delay=1', text], check=True)
                print(f"Typed: {text}")
                try:
                    subprocess.run(['notify-send', 'Talkat', f'Typed: {text}'], check=False)
                except FileNotFoundError:
                    pass
            except (subprocess.CalledProcessError, FileNotFoundError):
                print("ydotool not available, printing text instead:")
                print(f"TEXT: {text}")
                try:
                    subprocess.run(['notify-send', 'Talkat', f'Recognized: {text}'], check=False)
                except FileNotFoundError:
                    pass
        else:
            print("No text recognized in the audio")
            try:
                subprocess.run(['notify-send', 'Talkat', 'No text recognized'], check=False)
            except FileNotFoundError:
                pass
            
        return 0
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        try:
            subprocess.run(['notify-send', 'Talkat', f'Error: {e}'], check=False)
        except FileNotFoundError:
            pass
        return 1

def run_long_dictation_command(
    model_type: str, 
    model_name: str, 
    silence_threshold: float,
    model_cache_dir: Optional[str] = None,
    fw_device: str = "cpu",
    fw_compute_type: str = "int8",
    fw_device_index: Union[int, List[int]] = 0,
    vosk_model_base_dir: str = "~/.local/share/vosk",
    clipboard: bool = True
):
    """Runs long dictation mode with continuous speech recognition."""
    # Clean up PID file on exit
    from pathlib import Path
    PID_FILE = Path.home() / ".cache" / "talkat" / "long_dictation.pid"
    
    def cleanup_pid():
        try:
            if PID_FILE.exists():
                pid_text = PID_FILE.read_text().strip()
                if pid_text and os.getpid() == int(pid_text):
                    PID_FILE.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
    
    # Use the passed silence_threshold
    current_threshold = silence_threshold
    if current_threshold == CODE_DEFAULTS['silence_threshold']:
        loaded_file_conf_val = load_app_config().get('silence_threshold')
        if loaded_file_conf_val != CODE_DEFAULTS['silence_threshold']:
             print(f"Using calibrated threshold from config: {current_threshold:.1f}")
        else:
            print(f"No calibrated threshold found in config or CLI. Using default: {current_threshold:.1f}")
            print(f"Run 'talkat calibrate' to set a custom threshold.")
    else:
         print(f"Using threshold: {current_threshold:.1f} (from CLI or config)")
    
    # Create a single transcript file for this session
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript_filename = f"{timestamp}_long.txt"
    transcript_dir = get_transcript_dir()
    transcript_path = transcript_dir / transcript_filename
    
    print(f"Starting long dictation mode. Press Ctrl+C to stop.")
    print(f"Transcript directory: {transcript_dir}")
    print(f"Transcript will be saved to: {transcript_path}")
    
    # Create empty file to ensure it exists
    transcript_path.touch()
    
    if clipboard:
        print("Transcript will be copied to clipboard when finished.")
    
    try:
        subprocess.run(['notify-send', 'Talkat', 'Long dictation mode started. Press Ctrl+C to stop.'], check=False)
    except FileNotFoundError:
        pass
    
    full_transcript = []
    session = requests.Session()
    
    try:
        while True:  # Continue until interrupted
            # Use the existing stream_audio_with_vad for each utterance
            audio_stream_generator_func = stream_audio_with_vad(
                silence_threshold=0,  # No silence threshold for long mode
                debug=False,  # Less verbose for continuous mode
                max_duration=600.0  # 10 minute timeout for long mode
            )

            # Get the sample rate first
            try:
                sample_rate = next(audio_stream_generator_func)
                if not isinstance(sample_rate, int):
                    print("Error: stream_audio_with_vad did not yield sample rate correctly.")
                    continue  # Try again for next utterance
            except StopIteration:
                # No speech detected in this cycle, wait a bit and try again
                time.sleep(0.1)
                continue

            # Collect audio data for this utterance
            def request_data_generator():
                # First, send metadata as a JSON string line
                metadata = {"rate": sample_rate}
                yield json.dumps(metadata).encode('utf-8') + b'\n'
                # Then, stream audio chunks
                for audio_chunk in audio_stream_generator_func:
                    if audio_chunk:
                        yield audio_chunk
            
            server_url = "http://127.0.0.1:5555/transcribe_stream"
            try:
                response = session.post(server_url, data=request_data_generator(), timeout=120)
                response.raise_for_status()
                
                response_json = response.json()
                text = response_json.get('text', '').strip()
                
                if text:
                    print(f"Recognized: {text}")
                    full_transcript.append(text)
                    
                    # Save to file immediately
                    with open(transcript_path, 'a', encoding='utf-8') as f:
                        f.write(text + ' ')
                    
                    # Don't type in long dictation mode
                    print("(Saved to transcript)")
                
            except requests.exceptions.ConnectionError:
                print(f"Error: Could not connect to the model server at {server_url}.")
                print("Please ensure the model server is running: talkat server")
                try:
                    subprocess.run(['notify-send', 'Talkat', 'Error: Model server not reachable.'], check=False)
                except FileNotFoundError:
                    pass
                return 1
            except requests.exceptions.RequestException as e:
                print(f"Error communicating with model server: {e}")
                continue  # Try next utterance
            except json.JSONDecodeError:
                print(f"Error: Could not decode JSON response from server")
                continue  # Try next utterance
                
    except KeyboardInterrupt:
        print("\nLong dictation mode stopped.")
        
        # Close the session cleanly
        session.close()
        
        # Join all transcript parts
        full_text = ' '.join(full_transcript)
        
        if full_text and clipboard:
            if copy_to_clipboard(full_text):
                print("Transcript copied to clipboard!")
                try:
                    subprocess.run(['notify-send', 'Talkat', 'Transcript copied to clipboard'], check=False)
                except FileNotFoundError:
                    pass
            else:
                print("Could not copy to clipboard (wl-copy or xclip not available)")
        
        print(f"Full transcript saved to: {transcript_path}")
        print(f"Total words: {len(full_text.split())}")
        
        try:
            subprocess.run(['notify-send', 'Talkat', f'Long dictation stopped. Saved to {transcript_filename}'], check=False)
        except FileNotFoundError:
            pass
        
        cleanup_pid()
        return 0
    except Exception as e:
        print(f"Error in long dictation mode: {e}")
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

    parser = argparse.ArgumentParser(description="Talkat: Speech-to-text utility.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    # Set default command based on mode
    if mode == "calibrate":
        default_command = 'calibrate'
    elif mode == "long":
        default_command = 'long'
    else:
        default_command = 'listen'
    parser.add_argument('command', nargs='?', default=default_command, choices=['listen', 'calibrate', 'long'], 
                        help='Command to run.')

    # Set defaults for argparse from the loaded configuration
    parser.add_argument('--model_type', type=str, 
                        default=initial_config.get('model_type'),
                        choices=['vosk', 'faster-whisper'],
                        help='Type of speech recognition model to use.')
    parser.add_argument('--model_name', type=str, 
                        default=initial_config.get('model_name'),
                        help='Name of the model. For Vosk, e.g., "model-en". For faster-whisper, e.g., "tiny.en".')
    parser.add_argument('--silence_threshold', type=float,
                        default=initial_config.get('silence_threshold'),
                        help='Silence threshold for VAD.')
    parser.add_argument('--vosk_model_base_dir', type=str,
                        default=initial_config.get('vosk_model_base_dir'),
                        help='Base directory for Vosk models.')
    
    # Faster-whisper specific arguments
    parser.add_argument('--faster_whisper_model_cache_dir', type=str, 
                        default=initial_config.get('faster_whisper_model_cache_dir'),
                        help='Optional: Custom cache directory for faster-whisper models.')
    parser.add_argument('--fw_device', type=str, 
                        default=initial_config.get('fw_device'),
                        help='Device for faster-whisper (e.g., "cpu", "cuda").')
    parser.add_argument('--fw_compute_type', type=str, 
                        default=initial_config.get('fw_compute_type'),
                        help='Compute type for faster-whisper (e.g., "int8", "float16").')

    def parse_device_index_str(value_str: str) -> Union[int, List[int]]:
        if not value_str.strip():
            raise argparse.ArgumentTypeError(f"fw_device_index string cannot be empty if provided. Got '{value_str}'")
        if ',' in value_str:
            try:
                return [int(x.strip()) for x in value_str.split(',')]
            except ValueError:
                raise argparse.ArgumentTypeError(f"Invalid format for multi-value fw_device_index: '{value_str}'. Must be comma-separated integers.")
        else:
            try:
                return int(value_str)
            except ValueError:
                raise argparse.ArgumentTypeError(f"Invalid integer for fw_device_index: '{value_str}'.")

    parser.add_argument('--fw_device_index', 
                        type=parse_device_index_str, # Converts CLI string to int or List[int]
                        default=initial_config.get('fw_device_index'), # Actual default value, not a string
                        help='Device index for faster-whisper (e.g., 0 or "0,1" for multi-GPU).')
    
    # Transcript and clipboard arguments
    parser.add_argument('--no-clipboard', action='store_true',
                        help='Disable clipboard copy for long dictation mode.')
    parser.add_argument('--no-save-transcripts', action='store_true',
                        help='Disable saving transcripts to files.')
    parser.add_argument('--transcript-dir', type=str,
                        default=initial_config.get('transcript_dir'),
                        help='Directory to save transcripts.')


    args = parser.parse_args()

    # After parsing, args contains the final values following CLI > File > Code_Default hierarchy
    # For calibration, we pass the resolved 'args' to carry over any CLI overrides to be saved.
    # However, run_calibration_command should ideally work with the config state prior to *its own* CLI args.
    # Let's adjust: run_calibration_command should operate on the initial_config, update it, and save.
    # Any CLI args for *calibration itself* (if we had them) would be different.
    # The current `args` reflects the final state for *running commands*.

    if args.command == 'calibrate':
        # For saving, we want to preserve other settings from config/defaults,
        # and only update the threshold.
        # `initial_config` has defaults + file. CLI args for general settings are in `args`.
        # We want to save a config that reflects the state if `listen` was run with these CLI args.
        config_for_saving = initial_config.copy()
        config_for_saving.update(vars(args)) # Apply CLI overrides to what we'll save
        # Remove 'command' as it's not a persistent config
        if 'command' in config_for_saving:
            del config_for_saving['command']
            
        return run_calibration_command(config_for_saving) # Pass the effectively resolved config
    elif args.command == 'listen':
        return run_listen_command(
            model_type=args.model_type,
            model_name=args.model_name,
            silence_threshold=args.silence_threshold,
            model_cache_dir=args.faster_whisper_model_cache_dir,
            fw_device=args.fw_device,
            fw_compute_type=args.fw_compute_type,
            fw_device_index=args.fw_device_index,
            vosk_model_base_dir=args.vosk_model_base_dir
        )
    elif args.command == 'long':
        # Determine clipboard setting
        clipboard_enabled = initial_config.get('clipboard_on_long', True) and not args.no_clipboard
        
        return run_long_dictation_command(
            model_type=args.model_type,
            model_name=args.model_name,
            silence_threshold=args.silence_threshold,
            model_cache_dir=args.faster_whisper_model_cache_dir,
            fw_device=args.fw_device,
            fw_compute_type=args.fw_compute_type,
            fw_device_index=args.fw_device_index,
            vosk_model_base_dir=args.vosk_model_base_dir,
            clipboard=clipboard_enabled
        )
    else:
        parser.print_help()
        return 1
            
if __name__ == "__main__":
    sys.exit(main())
