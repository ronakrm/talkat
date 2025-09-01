# CLAUDE.md

This file provides comprehensive guidance to Claude Code (claude.ai/code) when working with the Talkat codebase.

## Project Overview

Talkat is a voice-to-text dictation system for Wayland Linux compositors. It runs a local speech recognition server and allows users to dictate text into any application using voice commands.

## Architecture

### Client-Server Design
```
┌─────────────┐       HTTP        ┌──────────────┐
│   Client    │◄──────────────────►│ Model Server │
│  (main.py)  │   /transcribe      │(model_server)│
│             │   /transcribe_stream│              │
└──────┬──────┘                    └──────┬───────┘
       │                                   │
   Audio Input                      Speech Models
   (pyaudio)                    (Faster-Whisper/Vosk)
       │                                   
   VAD + Stream                            
   (record.py)                             
       │
   Text Output
   (ydotool)
       │
   Toggle Control
   (PID tracking)
```

### Core Components

1. **Model Server** (`model_server.py`)
   - Flask HTTP server on port 5555
   - Loads and manages speech recognition models
   - Provides batch and streaming transcription endpoints
   - Supports Faster-Whisper (default) and Vosk models

2. **Client** (`main.py`)
   - Captures audio from microphone
   - Implements Voice Activity Detection (VAD)
   - Streams audio to server for transcription
   - Types recognized text via ydotool (Wayland)
   - **Toggle support**: PID file tracking for start/stop with same command
   - Graceful interruption handling via signals

3. **Voice Activity Detection** (`record.py`)
   - Sophisticated VAD with pre-speech padding (0.3s)
   - Dynamic silence threshold calibration
   - Streaming generator pattern for real-time processing
   - Configurable silence duration detection

4. **CLI Router** (`cli.py`)
   - Command routing and argument parsing
   - PID file management for toggle functionality
   - Background process management for long dictation
   - Process lifecycle handling

5. **Configuration** (`config.py`)
   - Hierarchical config: defaults → file → CLI args
   - JSON config at `~/.config/talkat/config.json`
   - Model cache at `~/.cache/talkat/`
   - Transcripts at `~/.local/share/talkat/transcripts/`
   - PID files at `~/.cache/talkat/*.pid`

## Development Workflow

### Setup Development Environment
```bash
# Clone repository
git clone <repo>
cd talkat

# Install uv if not present
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Run development server
uv run talkat server

# In another terminal, test client
uv run talkat listen
```

### Testing Workflow
```bash
# Start server in one terminal
uv run talkat server

# Test short dictation mode with toggle
uv run talkat listen  # Starts recording
uv run talkat listen  # Stops recording and transcribes

# Test long dictation mode
uv run talkat long

# Test calibration
uv run talkat calibrate

# Test background long dictation
uv run talkat start-long
uv run talkat stop-long
uv run talkat toggle-long
```

### Installation Modes

#### System-wide Installation (requires sudo)
```bash
sudo ./setup.sh
# or explicitly:
sudo ./setup.sh --system
```

#### User Installation (no sudo required)
```bash
./setup.sh --user
```

**Note**: After making changes to the code, always run `./setup.sh` (with appropriate mode) to update the installed version.

## Code Patterns and Conventions

### Type Hints
- Use comprehensive type hints for all function signatures
- Prefer specific types over `Any`
- Use `Optional[T]` for nullable types
- Use `Union[T1, T2]` for multiple types
- Import types from `typing` module

Example:
```python
from typing import Optional, List, Dict, Generator, Tuple

def process_audio(
    data: bytes,
    sample_rate: int,
    threshold: Optional[float] = None
) -> Tuple[str, float]:
    ...
```

### Error Handling
- Use specific exception types
- Always clean up resources in finally blocks
- Log errors with context (avoid bare prints)
- Provide fallback behavior where appropriate

Example:
```python
try:
    response = session.post(url, data=data)
    response.raise_for_status()
except requests.ConnectionError as e:
    logger.error(f"Failed to connect to server: {e}")
    return None
except requests.Timeout as e:
    logger.error(f"Request timed out: {e}")
    return None
finally:
    session.close()
```

### Resource Management
- Use context managers for file/network operations
- Clean up audio streams properly
- Manage subprocess lifecycle carefully
- Handle signals gracefully (SIGINT, SIGTERM)

### Audio Processing Patterns
```python
# Generator pattern for streaming
def stream_audio() -> Generator[bytes, None, None]:
    stream = pyaudio.PyAudio().open(...)
    try:
        while True:
            data = stream.read(CHUNK)
            yield data
    finally:
        stream.stop_stream()
        stream.close()

# VAD with pre-buffering
pre_buffer = collections.deque(maxlen=PRE_SPEECH_FRAMES)
for chunk in audio_stream:
    if voice_detected:
        yield from pre_buffer  # Yield buffered audio
        yield chunk
    else:
        pre_buffer.append(chunk)
```

## File Structure
```
talkat/
├── src/talkat/
│   ├── __init__.py      # Package marker
│   ├── cli.py           # CLI entry point and command routing
│   ├── config.py        # Configuration management
│   ├── devices.py       # Audio device discovery
│   ├── record.py        # VAD and audio recording
│   ├── model_server.py  # Flask HTTP server
│   ├── main.py          # Client orchestration
│   └── file_processor.py # Audio file transcription
├── setup.sh             # Installation script
├── pyproject.toml       # Project configuration
├── CLAUDE.md           # This file
└── README.md           # User documentation
```

## Common Tasks

### Adding a New Command
1. Add parser in `cli.py`
2. Implement handler function in appropriate module
3. Update CLAUDE.md and README.md
4. Run `./setup.sh` to update the installed version

### Adding a New Model Backend
1. Create model loader in `model_server.py`
2. Add configuration option in `config.py`
3. Implement transcription method
4. Add model download logic

### Improving VAD
1. Modify `stream_audio_with_vad()` in `record.py`
2. Adjust pre-speech padding and silence detection
3. Test with various audio environments

## Testing Checklist

### Manual Testing Required
Since no automated tests exist, these need manual verification:

1. **Basic Functionality**
   - [ ] Server starts successfully
   - [ ] Client connects to server
   - [ ] Audio capture works
   - [ ] Transcription produces text
   - [ ] Text is typed correctly
   - [ ] Toggle functionality works (second call stops recording)

2. **Modes**
   - [ ] Listen mode (single utterance)
   - [ ] Listen mode toggle (start/stop with same command)
   - [ ] Long mode (continuous)
   - [ ] Background long mode (start-long/stop-long)
   - [ ] Toggle-long mode
   - [ ] Calibration mode

3. **Edge Cases**
   - [ ] Server not running
   - [ ] Microphone not available
   - [ ] Network issues
   - [ ] Long silence periods
   - [ ] Very long utterances
   - [ ] Rapid speech
   - [ ] Background noise

4. **Installation**
   - [ ] Fresh system-wide install
   - [ ] Fresh user install
   - [ ] Update existing installation
   - [ ] Service management

## Performance Considerations

### Memory Usage
- Audio buffers grow with recording length
- Pre-speech buffer uses circular buffer (deque)
- Model stays loaded in server memory
- Consider streaming for large files

### Network Optimization
- Use session pooling for multiple requests
- Stream audio instead of batch for long recordings
- Implement compression for network transfer
- Consider WebSocket for bidirectional streaming

### Model Performance
- Faster-Whisper: Better accuracy, higher resource usage
- Vosk: Lower resource usage, faster, less accurate
- Consider model quantization for speed
- Implement model warm-up on server start

## Security Considerations

1. **Network Security**
   - Server binds to localhost only (127.0.0.1)
   - No authentication (local use only)
   - Consider adding token-based auth for network use

2. **File Permissions**
   - Config files are user-readable only
   - Transcripts saved with user permissions
   - Service runs as user, not root

3. **Input Validation**
   - Validate audio format and sample rate
   - Sanitize file paths
   - Limit request sizes

## Future Improvements

### High Priority
1. ~~Add toggle functionality for listen mode~~ ✅ Completed
2. Add comprehensive type hints throughout
3. Implement proper logging framework
4. Add automated tests
5. ~~Support audio file input (.wav, .mp3)~~ ✅ Completed
6. Improve error messages and user feedback

### Medium Priority
1. WebSocket support for real-time streaming
2. Multiple language support
3. Custom wake word detection
4. Punctuation and capitalization models
5. Speaker diarization for meetings

### Low Priority
1. GUI configuration tool
2. Cloud model support
3. Mobile app companion
4. Integration with other desktop environments

## Dependencies

### Python Packages
- `faster-whisper>=1.1.1` - Primary ASR engine
- `vosk>=0.3.45` - Alternative ASR engine
- `pyaudio>=0.2.14` - Audio I/O
- `numpy>=2.2.6` - Array operations
- `flask>=2.0` - HTTP server
- `requests>=2.20` - HTTP client
- `transformers>=4.36.0` - For Distil-Whisper models
- `torch` - CPU-only version by default (see CPU_OPTIMIZATION.md)
- `librosa>=0.10.1` - Audio file processing
- `soundfile>=0.12.1` - Audio file I/O

**Note**: PyTorch is installed as CPU-only by default to reduce size and improve compatibility. GPU users can override this if needed.

### System Requirements
- `ydotool` - Wayland input automation
- `wl-copy` or `xclip` - Clipboard operations
- `notify-send` - Desktop notifications
- `systemd` - Service management
- `uv` - Python package manager

## Debugging Tips

### Known Issues and Hacks

#### Major Hacks/Workarounds
1. **PID file management** - Basic file-based approach, should use proper IPC
2. **Signal handling** - Interrupt handling is fragile  
3. **Audio stream cleanup** - Not always properly cleaned up on errors
4. **Hardcoded timeouts** - Many timeouts should be configurable
5. **ALSA warnings** - Suppressed rather than properly handled
6. **Error messages** - Often generic, need more context

#### Known Bugs
1. **Server errors on long recordings** - 500 errors after max duration
2. **PID file race conditions** - Possible if commands run too quickly
3. **Audio device selection** - Falls back to default without proper error
4. **Memory leaks** - Possible in long-running server with certain models

### Common Issues

1. **"Server not responding"**
   - Check: `systemctl status talkat` (system) or `systemctl --user status talkat` (user)
   - Check: `lsof -i :5555`
   - Try: `systemctl restart talkat` (system) or `systemctl --user restart talkat` (user)

2. **"No audio input"**
   - Check: `pactl list sources`
   - Try: `uv run talkat calibrate`
   - Verify: PyAudio installation

3. **"Text not typing"**
   - Check: ydotoold is running
   - Verify: Wayland compositor compatibility
   - Test: `ydotool type "test"`

4. **"Toggle not working"**
   - Check PID file: `ls ~/.cache/talkat/listen.pid`
   - Clean up stale PIDs: `rm ~/.cache/talkat/*.pid`
   - Update installation: `./setup.sh --user` or `sudo ./setup.sh`

### Debug Mode
```python
# Enable debug output
DEBUG = True  # In relevant module

# Or via environment
DEBUG=1 uv run talkat listen
```

### Performance Profiling
```python
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()
# ... code to profile ...
profiler.disable()
stats = pstats.Stats(profiler).sort_stats('cumulative')
stats.print_stats()
```

## Contributing Guidelines

1. **Code Style**
   - Follow PEP 8
   - Use type hints
   - Add docstrings for public functions
   - Keep functions under 50 lines

2. **Testing**
   - Test manually before committing
   - Document test scenarios
   - Check edge cases

3. **Documentation**
   - Update CLAUDE.md for architectural changes
   - Update README.md for user-facing changes
   - Add inline comments for complex logic

4. **Commit Messages**
   - Use descriptive commit messages
   - Reference issues if applicable
   - Include Co-Authored-By for AI assistance