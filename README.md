# Talkat

A voice command system with local model server for privacy and offline use. Talkat allows you to speak into any keyboard input in a Wayland-based compositor, similar to nerd-dictation but with a simpler setup.

## ⚠️ Work in Progress

**This project is highly experimental and under active development.** Many components are hacky implementations that work but need refinement. Expect:
- Rough edges and incomplete features
- Minimal error handling in some areas
- Code that prioritizes "working" over "elegant"
- Frequent breaking changes
- Untested edge cases

Contributions, bug reports, and patience are all welcome!

## System Requirements

- Linux with Wayland compositor (Sway, Niri, etc.)
- Python 3.12 or higher
- Audio input device (microphone)
- Root access for server setup

## Dependencies

### System Dependencies
- `ydotool` and `ydotoold` for Wayland input simulation
- `notify-send` for notifications (optional)
- `uv` for Python package management
- `wl-copy` (wl-clipboard) or `xclip` for clipboard support (optional)

### Python Dependencies
- faster-whisper
- numpy
- pyaudio
- vosk
- flask
- requests

## Installation

1. First, set up ydotool for Wayland input simulation:
```bash
# Add your user to the input group
sudo usermod -aG input $USER

# Create udev rules for uinput access
echo '## Give ydotoold access to the uinput device
## Solution by https://github.com/ReimuNotMoe/ydotool/issues/25#issuecomment-535842993
KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
' | sudo tee /etc/udev/rules.d/80-uinput.rules > /dev/null

# Add ydotoold to your Wayland compositor startup
# For Sway, add to your config:
# exec --no-startup-id ydotoold
# For Niri, add to your config:
# spawn-at-startup "ydotoold"
```

2. Clone and install Talkat:
```bash
git clone https://github.com/yourusername/talkat.git
cd talkat

# Choose installation type:
sudo ./setup.sh           # System-wide installation (requires sudo)
./setup.sh --user         # User installation (no sudo required)
```

The setup script will:
- Install required Python dependencies
- Set up the model server as a systemd service, and download the `base.en` `faster-whisper` model.
- Create the `talkat` command-line tool

### Updating/Upgrading

To update talkat with the latest changes from the repository:
```bash
cd talkat
git pull
sudo ./setup.sh
```

The setup script automatically handles both fresh installations and updates.

## Usage

The model server runs automatically in the background after installation. You can use the following commands:

### Short Dictation Mode (with Toggle)
Start or stop listening for voice commands:
```bash
talkat listen  # First call: starts recording
talkat listen  # Second call: stops recording and transcribes
```

**Toggle Feature**: Running `talkat listen` while already recording will stop the current recording and process the transcription. This makes it perfect for keyboard shortcuts - press once to start, press again to stop.

The transcribed text is automatically typed to the current application and saved to a transcript file.

### Long Dictation Mode
Continuous dictation that saves to file without typing to screen:
```bash
talkat long              # Saves to file and copies to clipboard on exit
talkat long --no-clipboard   # Saves to file only
```

Long dictation mode:
- Continues recording until you press Ctrl+C
- Saves transcript to `~/.local/share/talkat/transcripts/`
- Automatically copies full transcript to clipboard when stopped (unless --no-clipboard is used)
- No timeout - perfect for long-form dictation, note-taking, or transcription

### Background Long Dictation
Manage long dictation as a background process:
```bash
talkat start-long    # Start long dictation in background
talkat stop-long     # Stop background long dictation
talkat toggle-long   # Toggle: start if stopped, stop if running
```

### Microphone Calibration
```bash
talkat calibrate
```

**Important**: During calibration, you should **remain silent**. The calibration process measures the ambient noise level in your environment (background noise, fan noise, etc.) to set an appropriate threshold for detecting when you're actually speaking. Do NOT speak during the 10-second calibration period.

Run calibration when:
- First setting up Talkat
- Moving to a different environment
- Experiencing issues with speech detection
- Background noise levels change significantly

- Check server status:
```bash
systemctl status talkat
```

### Create Shortcuts
Bind commands to keyboard shortcuts, e.g., for Niri:
```
# Single key for toggle recording (press to start, press again to stop)
Mod+Apostrophe { spawn "bash" "-c" "talkat listen"; }

# Long dictation mode
Mod+Shift+Apostrophe { spawn "bash" "-c" "talkat long"; }

# Toggle background long dictation
Mod+Ctrl+Apostrophe { spawn "bash" "-c" "talkat toggle-long"; }
```

For Sway, use similar bindings with `bindsym`:
```
bindsym $mod+apostrophe exec bash -c "talkat listen"
bindsym $mod+Shift+apostrophe exec bash -c "talkat long"
```

## Configuration

The configuration file is located at `~/.config/talkat/config.json`. You can modify it to:
- Change the model type (vosk or faster-whisper)
- Adjust model parameters
- Configure audio settings
- Change the model cache location (default `~/.cache/talkat`)
- Configure transcript saving and clipboard behavior

Example configuration:
```json
{
    "silence_threshold": 100.0,
    "model_type": "faster-whisper",
    "model_name": "large.v3",
    "save_transcripts": true,
    "clipboard_on_long": true,
    "transcript_dir": "~/.local/share/talkat/transcripts"
}
```

### Transcript Features
- All transcripts (both short and long mode) are saved to `~/.local/share/talkat/transcripts/`
- Short mode: saves as `YYYYMMDD_HHMMSS_short.txt`
- Long mode: saves as `YYYYMMDD_HHMMSS_long.txt`
- Disable transcript saving: set `"save_transcripts": false`
- Change transcript location: set `"transcript_dir": "/your/custom/path"`

## How It Works

1. The model server runs as a systemd service in the background, preloading the speech recognition model for faster startup
2. When you run `talkat listen`, it:
   - Checks if a recording is already active (toggle feature)
   - If no recording: starts recording from your microphone
   - If recording active: stops the recording and processes transcription
   - Streams audio to the server for transcription
   - Uses ydotool to simulate keyboard input with the transcribed text
3. The server supports both Vosk and Faster-Whisper models
4. Voice Activity Detection (VAD) automatically detects when you start and stop speaking
   - Uses calibrated threshold to distinguish speech from background noise
   - Pre-buffers audio to avoid cutting off the beginning of speech
   - Detects silence periods to know when you've stopped talking

## Troubleshooting

1. If ydotool isn't working:
   - Make sure `ydotoold` is running
   - Check that your user is in the input group
   - Verify the udev rules are installed
   - Log out and back in after adding yourself to the input group

2. If the model server isn't starting:
   - Check systemd logs: `journalctl -u talkat` (system-wide) or `journalctl --user -u talkat` (user installation)
   - Verify model files are downloaded in `~/.cache/talkat/`
   - Check configuration file permissions

3. If toggle isn't working:
   - Check if PID file exists: `ls ~/.cache/talkat/listen.pid`
   - Ensure you're using the latest version: `cd talkat && git pull && ./setup.sh`
   - Try cleaning up stale PID files: `rm ~/.cache/talkat/*.pid`

4. Audio issues:
   - Run calibration: `talkat calibrate` (remember to stay SILENT during calibration)
   - If speech isn't detected, your threshold might be too high
   - If recording triggers on background noise, recalibrate in a quieter environment
   - Check available audio devices: `pactl list sources`
   - Verify PyAudio can access your microphone
   
