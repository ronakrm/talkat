# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Talkat is a voice-to-text dictation system for Wayland Linux compositors. It runs a local speech recognition server and allows users to dictate text into any application using voice commands.

## Development Commands

```bash
# Install dependencies
uv sync

# Run from source
uv run talkat listen    # Start voice capture client (short mode)
uv run talkat long      # Start long dictation mode
uv run talkat server    # Start model server
uv run talkat calibrate # Calibrate microphone

# Install system-wide (requires sudo)
sudo ./setup.sh

# Check systemd service
systemctl status talkat

# Long dictation options
uv run talkat long --no-clipboard  # Don't copy to clipboard
uv run talkat long --no-save-transcripts  # Don't save transcripts
```

## Architecture

The project uses a client-server architecture:

1. **Model Server** (`model_server.py`): Runs as systemd service on port 5555, loads speech recognition models (Vosk or Faster-Whisper), provides HTTP endpoints for transcription
2. **Client** (`main.py`): Captures audio, detects voice activity, streams to server, types recognized text via ydotool
3. **Voice Activity Detection** (`record.py`): Implements VAD with calibration, pre-speech padding, and silence detection

## Key Files

- `cli.py`: CLI entry point with `listen`, `long`, `server`, and `calibrate` commands
- `config.py`: Manages configuration at `~/.config/talkat/config.json`
- `setup.sh`: Installation script that sets up systemd service and command wrapper
- Models cached at: `~/.cache/talkat/`
- Transcripts saved at: `~/.local/share/talkat/transcripts/`

## New Features

### Long Dictation Mode
- `talkat long`: Continuous dictation mode that saves to file instead of typing
- Transcripts are saved with timestamp filenames (e.g., `20250611_134500_long.txt`)
- Full transcript is copied to clipboard on exit (Ctrl+C)
- All transcripts (both short and long mode) are saved to `~/.local/share/talkat/transcripts/`

### Configuration Options
```json
{
    "clipboard_on_long": true,      // Copy to clipboard after long dictation
    "save_transcripts": true,        // Save all transcripts to files
    "transcript_dir": "~/.local/share/talkat/transcripts"
}
```

## Testing

No automated tests currently exist. Manual testing involves:
1. Starting the server: `uv run talkat server`
2. Running the client: `uv run talkat listen`
3. Speaking to test transcription accuracy

## Dependencies

- Python 3.12+
- System: ydotool, ydotoold, uv
- Python: faster-whisper, vosk, pyaudio, flask, numpy