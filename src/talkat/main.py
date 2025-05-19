#!/usr/bin/env python3

import json
import vosk
import subprocess
import os
import sys
import argparse
from typing import Optional

from .record import record_audio_with_vad, calibrate_microphone

# Configuration for threshold storage
CONFIG_DIR = os.path.expanduser("~/.config/talkat")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_THRESHOLD = 500.0

def save_threshold(threshold: float):
    """Saves the calibration threshold to a config file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump({'silence_threshold': threshold}, f)
    print(f"Threshold {threshold:.1f} saved to {CONFIG_FILE}")

def load_threshold() -> Optional[float]:
    """Loads the calibration threshold from a config file."""
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        loaded_threshold = config.get('silence_threshold')
        if loaded_threshold is not None:
            return float(loaded_threshold)
        return None
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"Error loading threshold from {CONFIG_FILE}: {e}")
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

def run_calibration_command():
    """Runs microphone calibration and saves the threshold."""
    print("Starting microphone calibration...")
    threshold = calibrate_microphone()
    save_threshold(threshold)
    print(f"Calibration complete. Threshold set to: {threshold:.1f}")
    try:
        subprocess.run(['notify-send', 'Talkat', f'Calibration complete. Threshold: {threshold:.1f}'], check=False)
    except FileNotFoundError:
        pass
    return 0

def run_listen_command():
    """Runs the main speech-to-text process."""
    model_path = os.path.expanduser("~/.local/share/vosk/model-en")
    if not ensure_model_exists(model_path):
        return 1

    print("Loading model...")
    model = vosk.Model(model_path)
    rec = vosk.KaldiRecognizer(model, 16000)

    current_threshold = load_threshold()
    if current_threshold is None:
        print(f"No calibrated threshold found. Using default: {DEFAULT_THRESHOLD:.1f}")
        print(f"Run 'talkat calibrate' to set a custom threshold.")
        current_threshold = DEFAULT_THRESHOLD
    else:
        print(f"Using calibrated threshold: {current_threshold:.1f}")
    
    try:
        result = record_audio_with_vad(silence_threshold=current_threshold, debug=True)
        if not result:
            print("No speech detected")
            try:
                subprocess.run(['notify-send', 'Talkat', 'No speech detected'], check=False)
            except FileNotFoundError:
                pass
            return 0
        
        audio_data, rate = result
        print("Processing speech...")
        try:
            subprocess.run(['notify-send', 'Talkat', 'Processing...'], check=False)
        except FileNotFoundError:
            pass
        
        # Process audio in chunks
        chunk_size = 4000
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            if rec.AcceptWaveform(chunk):
                pass
        
        # Get final result
        final_result_json = rec.FinalResult()
        text_result = json.loads(final_result_json)
        text = text_result.get('text', '').strip()
        
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
    parser = argparse.ArgumentParser(description="Talkat: Speech-to-text utility.")
    # Make 'command' optional, default to 'listen'
    parser.add_argument('command', nargs='?', default='listen', choices=['listen', 'calibrate'], 
                        help='Command to run: "listen" (default) or "calibrate".')

    args = parser.parse_args()

    if args.command == 'calibrate':
        return run_calibration_command()
    elif args.command == 'listen':
        return run_listen_command()
    else:
        # This case should not be reached if choices are set correctly
        parser.print_help()
        return 1
            
if __name__ == "__main__":
    sys.exit(main())
