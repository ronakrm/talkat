"""Improved calibration function for record.py"""

import numpy as np
import pyaudio
import subprocess
from typing import List, Optional

# These would normally come from imports
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000

def calibrate_microphone(duration: int = 10) -> float:
    """Calibrates the microphone to determine an appropriate silence threshold using background noise analysis."""
    
    # Import find_microphone from the same module (when integrated)
    from talkat.devices import find_microphone
    
    # Show notification if possible
    try:
        subprocess.run(
            ['notify-send', 'Talkat Calibration', f'Measuring background noise for {duration} seconds. Please remain quiet.'],
            check=False,
            capture_output=True
        )
    except FileNotFoundError:
        pass
    
    print("\n" + "="*60)
    print("MICROPHONE CALIBRATION - Background Noise Analysis")
    print("="*60)
    print(f"Please remain QUIET during calibration ({duration} seconds).")
    print("Measuring ambient noise levels...")
    print("-"*60)
    
    mic_index: Optional[int] = find_microphone()
    if mic_index is None:
        print("No microphone found during calibration, using default threshold.")
        return 500.0  # Default fallback as float
    
    p = pyaudio.PyAudio()
    
    try:
        stream = p.open(format=FORMAT, 
                       channels=CHANNELS, 
                       rate=RATE,
                       input=True, 
                       input_device_index=mic_index,
                       frames_per_buffer=CHUNK)
    except Exception as e:
        print(f"Error opening audio stream for calibration: {e}")
        p.terminate()
        return 500.0
    
    volumes: List[float] = []
    chunks_to_read: int = int(duration * RATE / CHUNK)
    
    try:
        for i in range(chunks_to_read):
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.int16)
            # Use float32 to avoid overflow, matching runtime calculation
            volume = np.sqrt(np.mean(audio_data.astype(np.float32)**2))
            volumes.append(volume)
            
            # Show progress bar
            progress = (i + 1) / chunks_to_read
            bar_length = 40
            filled = int(bar_length * progress)
            bar = '█' * filled + '░' * (bar_length - filled)
            print(f"\rProgress: [{bar}] {progress*100:.0f}% | Current: {volume:6.1f}", end='', flush=True)
                
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
        print()  # New line after progress bar
    
    if not volumes:
        return 500.0
    
    # Calculate statistics using percentiles for better noise floor estimation
    volumes_array = np.array(volumes)
    
    # Use 90th percentile as the noise floor (ignoring occasional spikes)
    noise_floor: float = float(np.percentile(volumes_array, 90))
    
    # Also get other percentiles for context
    p50: float = float(np.percentile(volumes_array, 50))  # Median
    p75: float = float(np.percentile(volumes_array, 75))
    p95: float = float(np.percentile(volumes_array, 95))
    p99: float = float(np.percentile(volumes_array, 99))
    max_vol: float = float(np.max(volumes_array))
    min_vol: float = float(np.min(volumes_array))
    
    # Set threshold as 2.5x the 90th percentile noise floor
    # This gives good separation between background noise and speech
    threshold: float = noise_floor * 2.5
    
    # But ensure it's at least some minimum value
    threshold = max(threshold, 50.0)
    
    print("\n" + "-"*60)
    print("CALIBRATION RESULTS:")
    print("-"*60)
    print(f"  Background Noise Analysis:")
    print(f"    Min volume:         {min_vol:8.1f}")
    print(f"    50th percentile:    {p50:8.1f} (median)")
    print(f"    75th percentile:    {p75:8.1f}")
    print(f"    90th percentile:    {noise_floor:8.1f} ← NOISE FLOOR")
    print(f"    95th percentile:    {p95:8.1f}")
    print(f"    99th percentile:    {p99:8.1f}")
    print(f"    Max volume:         {max_vol:8.1f}")
    print(f"\n  Recommended threshold: {threshold:8.1f}")
    print(f"  (2.5x noise floor for reliable speech detection)")
    print("="*60)
    
    # Show notification with result
    try:
        subprocess.run(
            ['notify-send', 'Calibration Complete', f'Threshold set to {threshold:.0f}'],
            check=False,
            capture_output=True
        )
    except FileNotFoundError:
        pass
    
    return float(max(50.0, min(threshold, 1000.0)))  # Clamp between 50.0 and 1000.0, ensure float