# Talkat

A voice command system with local model server for privacy and offline use. Talkat allows you to speak into any keyboard input in a Wayland-based compositor, similar to nerd-dictation but with a simpler setup.

## Features

- Local voice command processing
- Support for both Vosk and Faster-Whisper models
- Background model server for efficient resource usage
- Simple command-line interface
- Wayland integration via ydotool

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
```

2. Clone and install Talkat:
```bash
git clone https://github.com/yourusername/talkat.git
cd talkat
sudo ./setup.sh
```

The setup script will:
- Install required Python dependencies
- Set up the model server as a systemd service, and download the `base.en` `faster-whisper` model.
- Create the `talkat` command-line tool

## Usage

The model server runs automatically in the background after installation. You can use the following commands:

- Start listening for voice commands:
```bash
talkat listen
```

- Calibrate your microphone (speak during this to set the silence threshold):
```bash
talkat calibrate
```

- Check server status:
```bash
systemctl status talkat
```

- Stop the server:
```bash
systemctl stop talkat
```

- Start the server:
```bash
systemctl start talkat
```

## Configuration

The configuration file is located at `~/.config/talkat/config.json`. You can modify it to:
- Change the model type (vosk or faster-whisper)
- Adjust model parameters
- Configure audio settings
- Change the model cache location (default `~/.cache/talkat`)

Example configuration:
```json
{
    "silence_threshold": 100.0,
    "model_type": "faster-whisper",
    "model_name": "base.en",
    "faster_whisper_model_cache_dir": "/home/USERNAME/.local/share/models/faster-whisper"
}
```

## Development

To run the server manually for development:
```bash
talkat server
```

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
   