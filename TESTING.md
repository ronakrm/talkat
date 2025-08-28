# Talkat Testing Guide

This document outlines all testing requirements for the Talkat voice-to-text system. Since automated tests are not yet implemented, thorough manual testing is essential.

## Pre-Testing Setup

### 1. Environment Preparation
```bash
# Install dependencies
uv sync

# Verify audio system
pactl list sources  # Check available microphones
python -c "import pyaudio; print(pyaudio.PyAudio().get_device_count())"  # Verify PyAudio

# Check Wayland tools
which ydotool       # Should return path
pgrep ydotoold      # Should show process ID
```

### 2. Model Server Setup
```bash
# Start the model server (required for all tests)
uv run talkat server

# In another terminal, verify server is running
curl http://127.0.0.1:5555/health
```

## Core Functionality Tests

### Test 1: Basic Voice Recognition
**Purpose**: Verify basic speech-to-text functionality

```bash
# Terminal 1: Start server
uv run talkat server

# Terminal 2: Test recognition
uv run talkat listen
```

**Test Steps**:
1. Speak clearly: "Hello world, this is a test"
2. Wait for text to appear in active window
3. Verify text matches spoken words

**Expected Result**: Text appears accurately in the focused application

### Test 2: Microphone Calibration
**Purpose**: Ensure calibration sets appropriate threshold

```bash
uv run talkat calibrate
```

**Test Steps**:
1. Follow on-screen prompts
2. Remain silent when asked
3. Speak when prompted
4. Note the calculated threshold value

**Expected Result**: 
- Threshold saved to `~/.config/talkat/config.json`
- Value should be reasonable (typically 100-1000)

### Test 3: Long Dictation Mode
**Purpose**: Test continuous dictation with file saving

```bash
# Start long dictation
uv run talkat long

# Speak multiple sentences with pauses
# Press Ctrl+C to stop
```

**Expected Result**:
- Continuous transcription displayed
- File saved to `~/.local/share/talkat/transcripts/`
- Text copied to clipboard on exit

### Test 4: Background Long Dictation
**Purpose**: Test background process management

```bash
# Start in background
uv run talkat start-long

# Verify it's running
ls ~/.cache/talkat/long_dictation.pid

# Stop the process
uv run talkat stop-long

# Toggle test
uv run talkat toggle-long  # Should start
uv run talkat toggle-long  # Should stop
```

**Expected Result**: Process starts/stops correctly with PID management

### Test 5: Audio File Processing
**Purpose**: Test file transcription capabilities

```bash
# Create test audio file (or use existing)
# Record a test file with your voice recorder

# Transcribe single file
uv run talkat file test.wav

# Transcribe with output formats
uv run talkat file test.wav -f json -o output.json
uv run talkat file test.wav -f srt -o output.srt
uv run talkat file test.wav -f vtt -o output.vtt

# Batch processing
uv run talkat batch *.wav -o transcripts/
```

**Expected Result**: Accurate transcription in specified formats

## Model Backend Tests

### Test 6: Faster-Whisper Model
```bash
# Configure for Faster-Whisper
echo '{"model_type": "faster-whisper", "model_name": "base.en"}' > ~/.config/talkat/config.json

# Restart server and test
uv run talkat server
# In another terminal:
uv run talkat listen
```

### Test 7: Distil-Whisper Model (if available)
```bash
# Configure for Distil-Whisper
echo '{"model_type": "distil-whisper"}' > ~/.config/talkat/config.json

# Restart server and test
uv run talkat server
# In another terminal:
uv run talkat listen
```

### Test 8: Vosk Model (if installed)
```bash
# Download a Vosk model first
mkdir -p ~/.cache/talkat/vosk
cd ~/.cache/talkat/vosk
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
mv vosk-model-small-en-us-0.15 model-en

# Configure for Vosk
echo '{"model_type": "vosk", "model_name": "model-en"}' > ~/.config/talkat/config.json

# Restart server and test
uv run talkat server
# In another terminal:
uv run talkat listen
```

## Edge Case Tests

### Test 9: Server Not Running
**Purpose**: Verify graceful error handling

```bash
# Stop the server if running
pkill -f "talkat server"

# Try to use client
uv run talkat listen
```

**Expected Result**: Clear error message about server not running

### Test 10: No Microphone Available
**Purpose**: Test microphone error handling

```bash
# Disable microphone (system-specific)
# Or unplug USB microphone

uv run talkat listen
```

**Expected Result**: Clear error message about microphone not found

### Test 11: Long Silence Periods
**Purpose**: Test VAD timeout handling

```bash
uv run talkat listen
# Remain silent for 30+ seconds
```

**Expected Result**: Timeout with appropriate message

### Test 12: Background Noise
**Purpose**: Test noise filtering

```bash
# Play background music/noise
uv run talkat listen
# Speak over the noise
```

**Expected Result**: Speech detected despite noise (may need calibration adjustment)

### Test 13: Rapid Speech
**Purpose**: Test fast speech handling

```bash
uv run talkat listen
# Speak very quickly without pauses
```

**Expected Result**: Full transcription captured

### Test 14: Multiple Languages (Whisper models only)
**Purpose**: Test language detection

```bash
uv run talkat listen
# Speak in different languages
```

**Expected Result**: Correct transcription (Whisper auto-detects language)

## Performance Tests

### Test 15: Memory Usage
```bash
# Monitor memory while running
htop  # or top

# Start server
uv run talkat server

# Note baseline memory
# Run long dictation for 10+ minutes
uv run talkat long

# Check for memory leaks
```

**Expected Result**: Stable memory usage, no significant growth

### Test 16: CPU Usage
```bash
# Monitor CPU during transcription
htop  # Watch CPU percentage

uv run talkat listen
# Speak continuously
```

**Expected Result**: 
- CPU spikes during transcription
- Returns to baseline when idle

### Test 17: Network Latency
```bash
# Add artificial latency (requires root)
sudo tc qdisc add dev lo root netem delay 100ms

# Test transcription
uv run talkat listen

# Remove latency
sudo tc qdisc del dev lo root
```

**Expected Result**: Still functional with slight delay

## Installation Tests

### Test 18: Fresh System-wide Installation
```bash
# Remove existing installation
sudo rm -rf /opt/talkat
sudo rm /usr/local/bin/talkat
sudo rm /etc/systemd/system/talkat.service

# Fresh install
sudo ./setup.sh

# Verify service
systemctl status talkat

# Test client
talkat listen
```

### Test 19: User Installation
```bash
# Install without sudo
./setup.sh --user

# Verify service
systemctl --user status talkat

# Test client
~/.local/bin/talkat listen
```

### Test 20: Update Existing Installation
```bash
# Make a change to code
echo "# test comment" >> src/talkat/main.py

# Run update
sudo ./setup.sh  # or ./setup.sh --user

# Verify service restarted
systemctl status talkat
```

## Integration Tests

### Test 21: Clipboard Integration
```bash
# Test clipboard copy
uv run talkat long
# Speak some text
# Press Ctrl+C

# Paste somewhere to verify clipboard
wl-paste  # or xclip -o
```

### Test 22: Notification System
```bash
# Ensure notification daemon is running

uv run talkat start-long
# Should see "Long dictation started" notification

uv run talkat stop-long
# Should see "Long dictation stopped" notification
```

### Test 23: Transcript Saving
```bash
uv run talkat long
# Speak several sentences
# Press Ctrl+C

# Check saved files
ls -la ~/.local/share/talkat/transcripts/
cat ~/.local/share/talkat/transcripts/*_long.txt
```

## Stress Tests

### Test 24: Very Long Audio Files
```bash
# Create or obtain a 30+ minute audio file
uv run talkat file long_audio.wav
```

**Expected Result**: Completes without timeout or memory issues

### Test 25: Concurrent Requests
```bash
# Start server
uv run talkat server

# In multiple terminals simultaneously:
uv run talkat listen
```

**Expected Result**: Server handles requests (may queue)

### Test 26: Continuous Operation
```bash
# Run long dictation for extended period
uv run talkat long
# Leave running for 1+ hours with periodic speech
```

**Expected Result**: No crashes, stable performance

## Checklist Summary

### Basic Functionality
- [ ] Voice recognition works
- [ ] Text appears in active window
- [ ] Microphone calibration saves threshold
- [ ] Long dictation saves transcripts
- [ ] Background mode starts/stops correctly

### File Processing
- [ ] Single file transcription works
- [ ] Multiple format outputs (json, srt, vtt)
- [ ] Batch processing completes
- [ ] Large files process successfully

### Error Handling
- [ ] Server connection errors show clear message
- [ ] Missing microphone detected gracefully
- [ ] Timeout handling works
- [ ] Invalid input rejected appropriately

### Models
- [ ] Faster-Whisper model works
- [ ] Distil-Whisper model works (if configured)
- [ ] Vosk model works (if configured)
- [ ] Model switching successful

### System Integration
- [ ] Clipboard copy works
- [ ] Notifications appear
- [ ] Transcripts save correctly
- [ ] Service management works

### Performance
- [ ] Memory usage stable
- [ ] CPU usage reasonable
- [ ] No memory leaks in long sessions
- [ ] Handles background noise

### Installation
- [ ] System-wide installation works
- [ ] User installation works
- [ ] Update process works
- [ ] Service auto-starts

## Reporting Issues

When reporting issues, please include:

1. **System Information**:
   ```bash
   uname -a
   python --version
   uv --version
   echo $XDG_SESSION_TYPE  # Should be wayland or x11
   ```

2. **Error Messages**: Full error output from terminal

3. **Configuration**:
   ```bash
   cat ~/.config/talkat/config.json
   ```

4. **Steps to Reproduce**: Exact commands and actions

5. **Expected vs Actual Behavior**: What should happen vs what happened

6. **Server Logs**: Output from `uv run talkat server` terminal

## Automated Testing (Future)

Future automated tests should cover:

1. **Unit Tests**:
   - VAD algorithm accuracy
   - Audio processing functions
   - Configuration management
   - Model backend interfaces

2. **Integration Tests**:
   - Client-server communication
   - File I/O operations
   - System command execution

3. **Performance Tests**:
   - Transcription speed benchmarks
   - Memory usage profiling
   - Concurrent request handling

4. **End-to-End Tests**:
   - Full workflow automation
   - Multiple model comparison
   - Error recovery scenarios

To implement automated tests, consider using:
- `pytest` for test framework
- `pytest-mock` for mocking
- `pytest-benchmark` for performance tests
- `hypothesis` for property-based testing
- Mock audio data for consistent testing