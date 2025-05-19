#!/usr/bin/env python3

import json
import subprocess
import os
import sys
import argparse
from typing import Optional, Dict, Any, Union, List

import vosk
from faster_whisper import WhisperModel
import numpy as np
import requests # Added for HTTP calls
import base64   # Added for encoding audio data

from .record import record_audio_with_vad, calibrate_microphone, stream_audio_with_vad

# Configuration for threshold storage
CONFIG_DIR = os.path.expanduser("~/.config/talkat")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 1. CODE DEFAULTS
CODE_DEFAULTS: Dict[str, Any] = {
    "silence_threshold": 500.0,
    "model_type": "vosk",
    "model_name": "model-en",
    "faster_whisper_model_cache_dir": None,
    "fw_device": "cpu",
    "fw_compute_type": "int8",
    "fw_device_index": 0,  # As per user's latest change: model_kwargs["device_index"] = 0
    "vosk_model_base_dir": "~/.local/share/vosk", # New default for Vosk model base
}

def load_app_config() -> Dict[str, Any]:
    """Loads the application configuration from a JSON file.
    Merges with code defaults, file values taking precedence.
    """
    config = CODE_DEFAULTS.copy()
    if os.path.exists(CONFIG_FILE):
        print(f"Loading config from {CONFIG_FILE}...")
        try:
            with open(CONFIG_FILE, 'r') as f:
                file_config = json.load(f)
            config.update(file_config)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"Error loading config from {CONFIG_FILE}: {e}. Using defaults.", file=sys.stderr)
    else:
        print(f"No config file found at {CONFIG_FILE}. Using defaults.")
    return config

def save_app_config(config_dict: Dict[str, Any]):
    """Saves the application configuration to a JSON file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_dict, f, indent=4)
        print(f"Configuration saved to {CONFIG_FILE}")
    except IOError as e:
        print(f"Error saving config to {CONFIG_FILE}: {e}", file=sys.stderr)

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
            silence_threshold=current_threshold, debug=True
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
            try:
                subprocess.run(['ydotool', 'type', text], check=True)
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

def main():
    # Load config (defaults updated by file)
    initial_config = load_app_config()

    parser = argparse.ArgumentParser(description="Talkat: Speech-to-text utility.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    parser.add_argument('command', nargs='?', default='listen', choices=['listen', 'calibrate'], 
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
    else:
        parser.print_help()
        return 1
            
if __name__ == "__main__":
    sys.exit(main())
