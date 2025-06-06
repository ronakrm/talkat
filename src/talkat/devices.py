import pyaudio
from typing import Optional

def list_audio_devices():
    """List all available audio devices"""
    p = pyaudio.PyAudio()
    print("\nAvailable audio devices:")
    for i in range(p.get_device_count()):
        device_info = p.get_device_info_by_index(i)
        if int(device_info['maxInputChannels']) > 0:
            print(f"  {i}: {device_info['name']} (inputs: {device_info['maxInputChannels']})")
    p.terminate()

def find_microphone() -> Optional[int]:
    """Find the best microphone device"""
    p = pyaudio.PyAudio()
    
    # List devices for debugging
    list_audio_devices()
    
    # Try to find the default input device
    try:
        default_device = p.get_default_input_device_info()
        print(f"Default input device: {default_device['name']} (index: {default_device['index']})")
        p.terminate()
        return int(default_device['index'])
    except Exception:
        # If no default, try to find any input device
        for i in range(p.get_device_count()):
            device_info = p.get_device_info_by_index(i)
            if int(device_info['maxInputChannels']) > 0:
                print(f"Using input device: {device_info['name']} (index: {i})")
                p.terminate()
                return i
        p.terminate()
        return None
