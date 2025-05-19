# talkat
Talk into any keyboard input in a Wayland-based Compositor

Basically nerd-dictation competitor, but I couldn't get their wacky config setup to work and wanted `uv`.

## Install
Make sure you have `ydotool` installed and the daemon `ytoold` running.
```
uv sync
```

Download models:
```
mkdir -p ~/.local/share/vosk/
cd ~/.local/share/vosk/
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
cp -r vosk-model-small-en-us-0.15/ model-en/
```

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
Will autostop when detecting 2 secs of silence, TODO factor to config.
```
talkat listen
```

