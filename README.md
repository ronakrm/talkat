# Talkat

A voice command system with local model server for privacy and offline use. Talkat allows you to speak into any keyboard input in a Wayland-based compositor, similar to nerd-dictation but with a simpler setup.

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
sudo ./setup.sh # sudo for setting up system daemons/processes
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

### Short Dictation Mode
Start listening for voice commands (types to screen and saves transcript):
```bash
talkat listen
```

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

### Other Commands
- Calibrate your microphone (speak during this to set the silence threshold):
```bash
talkat calibrate
```

- Check server status:
```bash
systemctl status talkat
```

### Create Shortcuts
Bind commands to keyboard shortcuts, e.g., for Niri:
```
Mod+Apostrophe { spawn "bash" "-c" "talkat listen"; }
Mod+Shift+Apostrophe { spawn "bash" "-c" "talkat long"; }
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
   - Connects to the local model server
   - Starts recording from your microphone
   - Streams audio to the server for transcription
   - Uses ydotool to simulate keyboard input with the transcribed text
3. The server supports both Vosk and Faster-Whisper models.

## Troubleshooting

1. If ydotool isn't working:
   - Make sure `ydotoold` is running
   - Check that your user is in the input group
   - Verify the udev rules are installed

2. If the model server isn't starting:
   - Check systemd logs: `journalctl -u talkat`
   - Verify model files are downloaded
   - Check configuration file permissions
   
