import pyaudio
import subprocess
import numpy as np
import warnings
from typing import Optional, Tuple, Generator, Union
import collections

from .devices import find_microphone

# Suppress ALSA warnings
warnings.filterwarnings("ignore", category=Warning)

def calibrate_microphone(duration: int = 3) -> float:
    """Calibrate microphone to find appropriate threshold"""
    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000
    
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
    
    print(f"Calibrating microphone for {duration} seconds...")
    print("Please speak normally during calibration...")
    
    volumes = []
    chunks_to_read = int(duration * RATE / CHUNK)
    
    try:
        for i in range(chunks_to_read):
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.int16)
            volume = np.sqrt(np.mean(audio_data**2))
            volumes.append(volume)
            
            # Show progress
            if i % 10 == 0:
                print(f"Calibration progress: {i/chunks_to_read*100:.0f}% (current volume: {volume:.1f})")
                
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
    
    if not volumes:
        return 500.0
    
    # Calculate statistics
    max_vol = float(max(volumes))
    avg_vol = float(np.mean(volumes))
    std_vol = float(np.std(volumes))
    
    # Set threshold as average + 1 standard deviation, but at least 50% of max
    threshold = float(max(avg_vol + std_vol, max_vol * 0.5))
    
    print(f"Calibration complete!")
    print(f"  Average volume: {avg_vol:.1f}")
    print(f"  Max volume: {max_vol:.1f}")
    print(f"  Recommended threshold: {threshold:.1f}")
    
    return float(max(50.0, min(threshold, 1000.0)))  # Clamp between 50.0 and 1000.0, ensure float

def record_audio_with_vad(silence_threshold: Optional[float] = None, silence_duration: float = 2.0, debug: bool = True) -> Optional[Tuple[bytes, int]]:
    """Record with improved VAD: pre-speech padding, defined speech segments, and clear stopping."""
    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000

    # VAD Configuration
    PRE_SPEECH_PADDING_DURATION = 0.3  # Seconds of audio to keep before speech starts
    MAX_RECORDING_DURATION_SECONDS = 30 # Overall timeout for listening

    if silence_threshold is None:
        # This path should ideally not be hit if main.py provides a threshold.
        print("Warning: silence_threshold not provided to record_audio_with_vad. Using a default fallback.")
        silence_threshold = 500.0 
    
    mic_index: Optional[int] = find_microphone()
    if mic_index is None:
        print("No microphone found!")
        try:
            subprocess.run(['notify-send', 'Talkat', 'No microphone found for recording!'], check=False)
        except FileNotFoundError:
            pass
        return None
    
    p = pyaudio.PyAudio()
    stream = None
    try:
        stream = p.open(format=FORMAT, 
                       channels=CHANNELS, 
                       rate=RATE,
                       input=True, 
                       input_device_index=mic_index,
                       frames_per_buffer=CHUNK)
    except Exception as e:
        print(f"Error opening audio stream: {e}")
        if p: p.terminate()
        try:
            subprocess.run(['notify-send', 'Talkat', f'Error opening audio stream: {e}'], check=False)
        except FileNotFoundError:
            pass
        return None
    
    print(f"Listening with threshold {silence_threshold:.1f}, silence duration {silence_duration:.1f}s...")
    print("Speak now!")
    
    try:
        subprocess.run(['notify-send', 'Talkat', 'Listening... Speak now!'], check=False)
    except FileNotFoundError:
        pass # notify-send is optional
    
    recorded_audio_segments = []
    current_segment_frames = [] 

    num_pre_padding_chunks = int(PRE_SPEECH_PADDING_DURATION * RATE / CHUNK)
    pre_speech_buffer = collections.deque(maxlen=num_pre_padding_chunks)

    is_speaking = False
    silent_chunks_count = 0
    max_silent_chunks_to_stop = int(silence_duration * RATE / CHUNK)
    
    max_total_chunks = int(MAX_RECORDING_DURATION_SECONDS * RATE / CHUNK)
    total_chunks_processed = 0
    
    try:
        while total_chunks_processed < max_total_chunks:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
                total_chunks_processed += 1
            except IOError as e: # More specific exception for stream read errors
                if e.errno == pyaudio.paInputOverflowed:
                    if debug: print("Input overflowed. Skipping frame.")
                    continue # Skip this chunk and continue
                print(f"Error reading audio: {e}")
                break # Critical read error

            audio_data = np.frombuffer(data, dtype=np.int16)
            # Handle empty audio_data if read fails or returns empty
            if audio_data.size == 0:
                if debug: print("Empty audio data received.")
                continue

            volume = np.sqrt(np.mean(audio_data.astype(np.float32)**2)) # Use float32 for mean calculation to avoid overflow

            if debug and total_chunks_processed % 10 == 0: # Print more frequently for debugging if needed
                 print(f"Chunk {total_chunks_processed}: Vol: {volume:.1f} (Thr: {silence_threshold:.1f}) SilentChunks: {silent_chunks_count}/{max_silent_chunks_to_stop} Speaking: {is_speaking}")

            if volume > silence_threshold:
                if not is_speaking: # Transition to speaking
                    if debug: print(f"Speech detected! Volume: {volume:.1f}")
                    is_speaking = True
                    current_segment_frames.extend(list(pre_speech_buffer)) # Add pre-buffered audio
                    # pre_speech_buffer.clear() # Clear it after use for this segment
                
                current_segment_frames.append(data) # Add current data chunk
                silent_chunks_count = 0 # Reset silence counter
            else: # volume <= silence_threshold (silence or low noise)
                if is_speaking:
                    # Still considered speaking, but it's a silent part of it.
                    current_segment_frames.append(data) # Continue recording this silence
                    silent_chunks_count += 1
                    if silent_chunks_count > max_silent_chunks_to_stop:
                        if debug: print("Silence duration exceeded after speech, segment finished.")
                        recorded_audio_segments.append(b''.join(current_segment_frames))
                        current_segment_frames = []
                        is_speaking = False # Reset for potential next utterance, though app breaks
                        break # Stop after the first full utterance for this app's design
                else:
                    # Still not speaking, keep adding to pre_speech_buffer
                    pre_speech_buffer.append(data)
        
        if total_chunks_processed >= max_total_chunks:
            if debug: print("Maximum recording duration reached.")

    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        if p:
            p.terminate()

    if debug: print(f"Recording loop finished. Processed {total_chunks_processed} chunks.")

    # If recording was active and current_segment_frames has data (e.g. due to timeout),
    # finalize this last segment.
    if current_segment_frames:
        if debug: print("Finalizing current (potentially incomplete) speech segment.")
        recorded_audio_segments.append(b''.join(current_segment_frames))

    if not recorded_audio_segments:
        if debug: print(f"No speech segments recorded.")
        try:
            subprocess.run(['notify-send', 'Talkat', 'No speech detected.'], check=False)
        except FileNotFoundError:
            pass
        return None

    final_audio_data = b''.join(recorded_audio_segments)
    
    if not final_audio_data: # Should be redundant given the check above
        if debug: print(f"Final audio data is empty.") # Should not happen
        return None

    if debug: print(f"Recorded {len(final_audio_data)} bytes of audio.")
    return final_audio_data, RATE

# New function for streaming with VAD
def stream_audio_with_vad(
    silence_threshold: Optional[float] = None, 
    silence_duration: float = 2.0, 
    debug: bool = True,
    chunk_size_ms: int = 30, # VAD works well with 10, 20, or 30ms frames
    max_duration: Optional[float] = 30.0  # None for unlimited
) -> Generator[Union[int, bytes], None, None]:
    """Record audio with VAD and yield it in chunks as a generator.
    First yields the sample rate (int), then yields audio data (bytes).
    """
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000 # Standard sample rate
    
    # Calculate chunk samples based on ms, ensure it's an integer for PyAudio
    CHUNK_SAMPLES = int(RATE * chunk_size_ms / 1000) 

    # VAD Configuration
    PRE_SPEECH_PADDING_DURATION = 0.3  # Seconds of audio to keep before speech starts
    MAX_RECORDING_DURATION_SECONDS = max_duration if max_duration is not None else float('inf')

    if silence_threshold is None:
        print("Warning: silence_threshold not provided to stream_audio_with_vad. Using a default fallback.")
        silence_threshold = 500.0 
    
    mic_index: Optional[int] = find_microphone()
    if mic_index is None:
        print("No microphone found for streaming!")
        try:
            subprocess.run(['notify-send', 'Talkat', 'No microphone found for streaming!'], check=False)
        except FileNotFoundError:
            pass
        return # End generator if no mic
    
    p = pyaudio.PyAudio()
    stream = None
    try:
        stream = p.open(format=FORMAT, 
                       channels=CHANNELS, 
                       rate=RATE,
                       input=True, 
                       input_device_index=mic_index,
                       frames_per_buffer=CHUNK_SAMPLES) # Use CHUNK_SAMPLES
    except Exception as e:
        print(f"Error opening audio stream for streaming: {e}")
        if p: p.terminate()
        try:
            subprocess.run(['notify-send', 'Talkat', f'Error opening audio stream: {e}'], check=False)
        except FileNotFoundError:
            pass
        return # End generator
    
    # First, yield the sample rate
    yield RATE

    print(f"Streaming with threshold {silence_threshold:.1f}, silence duration {silence_duration:.1f}s...")
    if debug: print("Speak now for streaming!")
    
    try:
        subprocess.run(['notify-send', 'Talkat', 'Streaming... Speak now!'], check=False)
    except FileNotFoundError:
        pass
    
    num_pre_padding_chunks = int(PRE_SPEECH_PADDING_DURATION * RATE / CHUNK_SAMPLES)
    pre_speech_buffer = collections.deque(maxlen=num_pre_padding_chunks)

    is_speaking = False
    silent_chunks_count = 0
    max_silent_chunks_to_stop = int(silence_duration * RATE / CHUNK_SAMPLES)
    
    if MAX_RECORDING_DURATION_SECONDS == float('inf'):
        max_total_chunks = float('inf')
    else:
        max_total_chunks = int(MAX_RECORDING_DURATION_SECONDS * RATE / CHUNK_SAMPLES)
    total_chunks_processed = 0
    speech_has_started_and_padded = False
    
    try:
        while total_chunks_processed < max_total_chunks:
            try:
                data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
                total_chunks_processed += 1
            except IOError as e:
                if e.errno == pyaudio.paInputOverflowed:
                    if debug: print("Input overflowed during streaming. Skipping frame.")
                    continue
                print(f"Error reading audio for streaming: {e}")
                break 

            audio_data_np = np.frombuffer(data, dtype=np.int16)
            if audio_data_np.size == 0:
                if debug: print("Empty audio data received during streaming.")
                continue

            volume = np.sqrt(np.mean(audio_data_np.astype(np.float32)**2))

            if debug and total_chunks_processed % (int(1000/chunk_size_ms) // 2) == 0: # Log roughly every 0.5s
                 print(f"Stream chunk {total_chunks_processed}: Vol: {volume:.1f} (Thr: {silence_threshold:.1f}) Silent: {silent_chunks_count}/{max_silent_chunks_to_stop} Speaking: {is_speaking}")

            if volume > silence_threshold:
                if not is_speaking: # Transition to speaking
                    if debug: print(f"Speech detected for streaming! Volume: {volume:.1f}")
                    is_speaking = True
                    # Yield pre-buffered audio first
                    for pre_chunk in list(pre_speech_buffer):
                        yield pre_chunk
                    pre_speech_buffer.clear() # Clear after yielding
                    speech_has_started_and_padded = True
                
                yield data # Yield current speech data chunk
                silent_chunks_count = 0
            else: # volume <= silence_threshold
                if is_speaking:
                    # Still considered speaking, but it's a silent part of it.
                    yield data # Yield this silence as part of the speech
                    silent_chunks_count += 1
                    if silent_chunks_count > max_silent_chunks_to_stop:
                        if debug: print("Silence duration exceeded after speech, stopping stream.")
                        break # Stop streaming after this utterance
                elif not speech_has_started_and_padded: # Only buffer if we haven't started speech & padding yet
                    # Still not speaking, keep adding to pre_speech_buffer
                    pre_speech_buffer.append(data)
        
        if total_chunks_processed >= max_total_chunks:
            if debug: print("Maximum recording duration reached for stream.")

    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        if p:
            p.terminate()
        if debug: print(f"Streaming loop finished. Processed {total_chunks_processed} chunks.")
