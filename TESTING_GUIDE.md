# Talkat Testing Guide

This guide explains how to test Talkat locally to ensure functionality remains intact after changes.

## Prerequisites

1. Ensure you have a working microphone
2. Install the application in development mode:
   ```bash
   uv sync
   ```
3. Ensure ydotool is installed and ydotoold is running (for Wayland):
   ```bash
   sudo systemctl status ydotool  # or ydotoold depending on your distro
   ```

## Testing Workflow

### 1. Start the Model Server

First, always start the model server in a separate terminal:

```bash
uv run talkat server
```

Wait for the message: `Model loaded and ready!`

### 2. Test Basic Voice Recognition

#### Short Dictation Mode
```bash
# Start listening (will stop after silence)
uv run talkat listen

# Speak clearly into your microphone
# Text should be typed automatically after you stop speaking
```

#### Toggle Mode (Start/Stop with Same Command)
```bash
# First call starts recording
uv run talkat listen
# Status: "Recording... Run 'talkat listen' again to stop"

# Second call stops and transcribes
uv run talkat listen
# Should transcribe and type the recorded audio
```

### 3. Test Long Dictation Mode

#### Interactive Mode
```bash
# Start continuous dictation (Ctrl+C to stop)
uv run talkat long

# Speak multiple sentences with pauses
# Each utterance should be saved to transcript file
# Press Ctrl+C to stop and copy full transcript to clipboard
```

#### Background Mode
```bash
# Start in background
uv run talkat start-long
# Check PID file created: ls ~/.cache/talkat/long_dictation.pid

# Toggle (should stop since it's running)
uv run talkat toggle-long

# Toggle again (should start since it's stopped)
uv run talkat toggle-long

# Stop explicitly
uv run talkat stop-long
```

### 4. Test Calibration

```bash
# Calibrate microphone threshold
uv run talkat calibrate

# Stay quiet for 3 seconds, then make noise
# Should show noise level statistics and set threshold
```

### 5. Test File Processing

```bash
# Create a test audio file or use existing one
# Transcribe a single file
uv run talkat transcribe /path/to/audio.wav

# Transcribe with clipboard copy
uv run talkat transcribe /path/to/audio.mp3 --copy

# Batch process multiple files
uv run talkat transcribe file1.wav file2.mp3 --format json
```

### 6. Test Configuration

```bash
# Check current config
cat ~/.config/talkat/config.json

# Test with custom settings
uv run talkat listen --model faster-whisper --model-name base.en

# Save configuration
uv run talkat listen --model vosk --save-config
```

## Verification Checklist

### Core Functionality
- [ ] Server starts without errors
- [ ] Model loads successfully (check server output)
- [ ] Microphone is detected
- [ ] Audio recording works
- [ ] Speech is transcribed correctly
- [ ] Text is typed via ydotool
- [ ] Clipboard operations work (wl-copy/xclip)

### File Locations (FHS Compliance)
- [ ] Config saved to `~/.config/talkat/config.json`
- [ ] Transcripts saved to `~/.local/share/talkat/transcripts/`
- [ ] Logs written to `~/.local/share/talkat/logs/`
- [ ] PID files in runtime dir (check with `echo $XDG_RUNTIME_DIR`)
- [ ] Model cache in `~/.cache/talkat/models/`

### Process Management
- [ ] PID files created/removed correctly
- [ ] Toggle functionality works (second call stops recording)
- [ ] Background processes start/stop cleanly
- [ ] No zombie processes after stopping
- [ ] Signal handling (Ctrl+C) works properly

### Security Features
- [ ] Server only binds to localhost (check with `lsof -i :5555`)
- [ ] Invalid file paths are rejected
- [ ] Long text is truncated appropriately
- [ ] Special characters in text don't cause issues

### Error Handling
- [ ] Server not running: Clear error message
- [ ] No microphone: Appropriate notification
- [ ] Network timeout: Handled gracefully
- [ ] Invalid audio file: Informative error

## Common Issues and Solutions

### "Server not responding"
```bash
# Check if server is running
lsof -i :5555

# Restart server
pkill -f "talkat server"
uv run talkat server
```

### "No audio input detected"
```bash
# List audio devices
pactl list sources

# Test microphone
arecord -d 5 test.wav && aplay test.wav

# Recalibrate
uv run talkat calibrate
```

### "Text not typing"
```bash
# Check ydotoold is running
systemctl status ydotoold  # or ydotool

# Test ydotool directly
ydotool type "test"

# For X11, ensure xdotool is installed
# For Wayland, ydotool requires root or proper permissions
```

### "Toggle not working"
```bash
# Clean up stale PID files
rm ~/.cache/talkat/*.pid

# Check for running processes
ps aux | grep talkat
```

## Performance Testing

### Memory Usage
```bash
# Monitor server memory
watch -n 1 'ps aux | grep model_server'

# Long dictation session (10+ minutes)
uv run talkat long
# Check for memory leaks
```

### Response Time
```bash
# Time short utterance
time uv run talkat listen

# Should complete in < 5 seconds for short speech
```

### Concurrent Usage
```bash
# Start server
uv run talkat server

# In multiple terminals simultaneously:
uv run talkat listen
```

## Testing After Code Changes

After making changes, run this minimal test suite:

1. **Lint and Type Check**
   ```bash
   uv run ruff check src/talkat/
   uv run mypy src/talkat/
   ```

2. **Basic Functionality**
   ```bash
   # Terminal 1
   uv run talkat server

   # Terminal 2
   uv run talkat calibrate
   uv run talkat listen  # Test once
   uv run talkat listen  # Start recording
   uv run talkat listen  # Stop and transcribe
   ```

3. **File Locations**
   ```bash
   # Verify files are created in correct locations
   ls ~/.config/talkat/
   ls ~/.local/share/talkat/transcripts/
   ls $XDG_RUNTIME_DIR/talkat/
   ```

4. **Process Cleanup**
   ```bash
   # Ensure no orphaned processes
   ps aux | grep talkat
   ls ~/.cache/talkat/*.pid  # Should be empty when not running
   ```

## Automated Testing (Future)

Currently, Talkat lacks automated tests. Future testing should include:

- Unit tests for VAD algorithm
- Integration tests for client-server communication
- Mock audio input for consistent testing
- CI/CD pipeline with GitHub Actions

## Debug Mode

For troubleshooting, enable debug logging:

```bash
# Verbose output
uv run talkat -v listen

# Debug server
uv run talkat -v server

# Check logs
tail -f ~/.local/share/talkat/logs/talkat.log
```

## Reporting Issues

When reporting issues, include:

1. Debug output (`-v` flag)
2. System information:
   ```bash
   uname -a
   python --version
   pactl list sources | head -20
   ```
3. Config file: `~/.config/talkat/config.json`
4. Recent logs: `~/.local/share/talkat/logs/talkat.log`
5. Steps to reproduce the issue
