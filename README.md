# talkat
Talk into any keyboard input in a Wayland-based Compositor.

Non-streaming right now, but works pretty well.

Basically nerd-dictation competitor, but I couldn't get their wacky config setup to work and wanted `uv`.

## Dependencies
- `uv`.
- `ydotool`, `ydotoold` daemon running.
- `notify-send` and maybe `mako` or something else like it.

## Install

```
cd ~/.local/share/
git clone ...
cd talkat
uv sync
```

Download models.

Vosk:
```
mkdir -p ~/.local/share/vosk/
cd ~/.local/share/vosk/
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
cp -r vosk-model-small-en-us-0.15/ model-en/
```

Faster-whisper: easiest to just run the script with models set (e.g., CLI or config).

This will create the script that you can run from anywhere:
```
sh setup.sh
```

# Usage
## Calibrate
Speak during this to calibrate the mic. Right now its not great, for me I just manually set to 100.
```
talkat calibrate
```

## Listen
Will autostop when detecting 2 secs of silence.
```
talkat listen
```

## Configure
All parameters can be overriden by either CLI (priority) or `~/.config/talkat/config.json`. e.g.,:
```
{
    "silence_threshold": 100.0,
    "model_type": "faster-whisper",
    "model_name": "medium",
    "faster_whisper_model_cache_dir": "/home/USERNAME/.local/share/models/faster-whisper"
}
```
