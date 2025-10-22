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

## Quick Start

Choose your workflow:
- **Development/Testing**: Use `uv run` for quick iteration without installation
- **User Installation**: Install to `~/.local/bin` (no sudo required)
- **System Installation**: Install to `/usr/local/bin` (requires sudo)
- **AUR Package Testing**: Test the full package build process

### Development and Testing (No Installation)

If you're developing or testing Talkat without installing it:

```bash
# Clone the repository
git clone https://github.com/yourusername/talkat.git
cd talkat

# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Start the server in one terminal
uv run talkat server

# In another terminal, test the client
uv run talkat listen           # Toggle recording (press once to start, again to stop)
uv run talkat calibrate        # Calibrate microphone (stay silent!)
uv run talkat long             # Long dictation mode (Ctrl+C to stop)
uv run talkat toggle-long      # Toggle background long dictation
```

**Important**: When testing with `uv run`:
- The server runs in the foreground, not as a systemd service
- You need to manually start the server in a separate terminal before using the client
- Use this for development iteration and testing changes

### Installation (End Users)

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

#### User Installation (Recommended)
No sudo required, installs to `~/.local/bin`:
```bash
git clone https://github.com/yourusername/talkat.git
cd talkat
./setup.sh --user
```

Make sure `~/.local/bin` is in your PATH (most distros do this by default).

#### System-wide Installation
Requires sudo, installs to `/usr/local/bin`:
```bash
git clone https://github.com/yourusername/talkat.git
cd talkat
sudo ./setup.sh
```

The setup script will:
- Install required Python dependencies via `uv`
- Set up the model server as a systemd service (user service for `--user`, system service for system install)
- Download the `base.en` faster-whisper model to `~/.cache/talkat/`
- Create the `talkat` command-line tool

**Important**: After installation, the server runs automatically as a systemd service. You don't need to manually start it.

**Check server status**:
- User installation: `systemctl --user status talkat`
- System installation: `systemctl status talkat`

### Updating After Code Changes

If you've made changes to the code and want to update your installation:

```bash
cd talkat

# For user installation (recommended):
./setup.sh --user

# For system-wide installation:
sudo ./setup.sh
```

The setup script automatically handles both fresh installations and updates. Always run it after pulling changes or modifying code.

### Testing AUR Package Build

If you're testing the AUR package build process (for maintainers or contributors):

```bash
# Step 1: Build source distribution
cd talkat
uv build --sdist  # Creates dist/talkat-*.tar.gz

# Step 2: Prepare PKGBUILD
cd pkgtest  # Or wherever your test PKGBUILD is
# Edit PKGBUILD to update pkgver if the version changed
updpkgsums  # Updates checksums based on the new tarball

# Step 3: Test the package build
makepkg -f  # Force rebuild, creates talkat-*.pkg.tar.zst

# Step 4: Test installation
makepkg -si  # Build and install (use -i for install)
# OR if already built:
sudo pacman -U talkat-*.pkg.tar.zst

# Step 5: Verify installation
systemctl --user status talkat  # Check service status
talkat calibrate                # Test calibration
talkat listen                   # Test recording
```

**Important Notes**:
- AUR packages use systemd **user services** (`systemctl --user`), not system services
- The service starts automatically on boot (user login)
- The `talkat` command is installed to `/usr/bin` (via pacman)
- Models are cached in `~/.cache/talkat/`
- Configuration is in `~/.config/talkat/`

**To uninstall after testing**:
```bash
sudo pacman -R talkat
systemctl --user stop talkat  # Stop the service first if needed
```

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
# User installation (recommended):
systemctl --user status talkat

# System installation:
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
   - Check systemd logs:
     - User installation: `journalctl --user -u talkat`
     - System installation: `journalctl -u talkat`
   - Check status:
     - User installation: `systemctl --user status talkat`
     - System installation: `systemctl status talkat`
   - Restart service:
     - User installation: `systemctl --user restart talkat`
     - System installation: `sudo systemctl restart talkat`
   - Verify model files are downloaded in `~/.cache/talkat/`
   - Check configuration file permissions

3. If toggle isn't working:
   - Check if PID file exists: `ls ~/.cache/talkat/listen.pid`
   - Ensure you're using the latest version:
     - User installation: `cd talkat && git pull && ./setup.sh --user`
     - System installation: `cd talkat && git pull && sudo ./setup.sh`
   - Try cleaning up stale PID files: `rm ~/.cache/talkat/*.pid`

4. Audio issues:
   - Run calibration: `talkat calibrate` (remember to stay SILENT during calibration)
   - If speech isn't detected, your threshold might be too high
   - If recording triggers on background noise, recalibrate in a quieter environment
   - Check available audio devices: `pactl list sources`
   - Verify PyAudio can access your microphone
