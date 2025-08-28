#!/usr/bin/env python3
"""Test script to verify volume calculation consistency."""

import numpy as np
import pyaudio
import time

CHUNK = 4000
RATE = 16000

def test_volume_calculations():
    """Test different volume calculation methods to show the difference."""
    
    p = pyaudio.PyAudio()
    
    # Find microphone
    mic_index = None
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info['maxInputChannels'] > 0:
            mic_index = i
            print(f"Using microphone: {info['name']}")
            break
    
    if mic_index is None:
        print("No microphone found!")
        return
    
    stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=RATE,
        input=True,
        input_device_index=mic_index,
        frames_per_buffer=CHUNK
    )
    
    print("\nSpeak to test volume calculations (5 seconds)...")
    print("=" * 60)
    print(f"{'Time':<6} {'Old Method':<12} {'New Method':<12} {'Difference':<12}")
    print("-" * 60)
    
    start_time = time.time()
    while time.time() - start_time < 5:
        data = stream.read(CHUNK, exception_on_overflow=False)
        audio_data = np.frombuffer(data, dtype=np.int16)
        
        # Old method (can overflow)
        try:
            old_volume = np.sqrt(np.mean(audio_data**2))
        except:
            old_volume = 0.0
        
        # New method (no overflow)
        new_volume = np.sqrt(np.mean(audio_data.astype(np.float32)**2))
        
        # Show the difference
        elapsed = time.time() - start_time
        diff = new_volume - old_volume
        print(f"{elapsed:5.1f}s {old_volume:11.1f} {new_volume:11.1f} {diff:+11.1f}")
    
    stream.stop_stream()
    stream.close()
    p.terminate()
    
    print("\nNote: Large differences indicate overflow in the old method.")
    print("The new method (float32) should be used for consistency.")

if __name__ == "__main__":
    test_volume_calculations()