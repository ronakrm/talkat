# Talkat

[![CI](https://github.com/ronakrm/talkat/actions/workflows/ci.yml/badge.svg)](https://github.com/ronakrm/talkat/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/ronakrm/talkat/graph/badge.svg)](https://codecov.io/gh/ronakrm/talkat)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A voice command system with local model server for privacy and offline use. Talkat allows you to speak into any keyboard input in a Wayland-based compositor, similar to nerd-dictation but with a simpler setup.

## Status

Stable as of `v1.0.0`. The CLI, on-disk layout, and wire protocol are
considered stable surface; breaking changes go through a deprecation cycle.
CI runs tests + lint + type-check on every push, the codebase is mypy-strict,
and the runtime surface is covered by ~370 automated tests. Issues and PRs
welcome — see [CHANGELOG.md](CHANGELOG.md) for what's in each release.

## System Requirements

- Linux with Wayland compositor (Sway, Niri, etc.)
- Python 3.11 or higher
- Audio input device (microphone)

## Dependencies

### System Dependencies
- `ydotool` and `ydotoold` for Wayland input simulation
- `uv` for Python package management
- `notify-send` (libnotify) for desktop notifications (optional)
- `wl-copy` (wl-clipboard) or `xclip` for clipboard support (optional)

### Python Dependencies (installed automatically into an isolated venv)
- `faster-whisper` (CTranslate2-backed, no torch required)
- `vosk`
- `numpy`
- `pyaudio`
- `flask` + `waitress` (model server, unix-socket-only)
- `httpx` (client; unix-socket transport)
- `librosa` + `soundfile` (audio file ingestion)

## Installation

Two supported paths. Talkat is a per-user tool — the model server always
runs as your user via `systemctl --user`, regardless of how the CLI is
installed.

### Path 1: Local install from a git checkout

```bash
git clone https://github.com/ronakrm/talkat.git
cd talkat
./setup.sh
```

What this does:
- `uv tool install --reinstall .` — installs the `talkat` CLI into an isolated
  venv at `~/.local/share/uv/tools/talkat/` and drops a wrapper at
  `~/.local/bin/talkat`
- `talkat install-service` — writes `~/.config/systemd/user/talkat.service`
  pointing at that interpreter, then `daemon-reload`/`enable`/`start`s it

To update after pulling: just re-run `./setup.sh`.

To uninstall:
```bash
talkat uninstall-service
uv tool uninstall talkat
```

### Path 2: AUR package (Arch Linux)

Talkat is published on the AUR as
[`talkat`](https://aur.archlinux.org/packages/talkat). Install with your
preferred AUR helper:

```bash
yay -S talkat       # or: paru -S talkat
systemctl --user enable --now talkat
```

The AUR package ships its own `/usr/lib/systemd/user/talkat.service`, so do
*not* run `talkat install-service` on top — just enable the unit directly.

To uninstall: `sudo pacman -R talkat`.

The PKGBUILD itself lives in the AUR git repo
(`ssh://aur@aur.archlinux.org/talkat.git`), not in this source tree — that's
the standard Arch packaging convention.

### Packaging — help wanted

Talkat currently ships only via the AUR (Arch) and `setup.sh` (any distro
with `uv`). Native `.deb` (Debian / Ubuntu) and `.rpm` (Fedora / openSUSE)
packages aren't in scope for v1.0.0 but would be very welcome contributions.

If you're a Debian / Ubuntu / Fedora packager and want to help, please open
an issue tagged
[`packaging`](https://github.com/ronakrm/talkat/issues?q=is%3Aissue+label%3Apackaging)
or file a new one. The bundled-venv-via-uv approach used by the AUR PKGBUILD
generalizes reasonably; the open questions are runtime model (system Python +
deb-packaged deps vs. vendored venv), repo hosting (GitHub releases vs. PPA
vs. Copr vs. OBS), and signing.

### One-time system setup for ydotool

Regardless of install path you need ydotool wired up:

```bash
# Add your user to the input group
sudo usermod -aG input $USER

# udev rule for uinput access
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
    | sudo tee /etc/udev/rules.d/80-uinput.rules > /dev/null

# Start ydotoold from your compositor:
# - Sway:  exec --no-startup-id ydotoold
# - Niri:  spawn-at-startup "ydotoold"
```

Log out + back in after changing groups.

### After install

1. Verify the service is up: `systemctl --user status talkat`
2. **Calibrate your microphone** (required for VAD to work):
   `talkat calibrate` (stay silent for 10 seconds)
3. Try `talkat listen` — focus a text editor and speak.

### Development without installing

For iterating on the code without going through `setup.sh`:

```bash
uv sync                # set up the project venv
uv run talkat server   # foreground model server (Ctrl+C to stop)

# in another terminal:
uv run talkat calibrate
uv run talkat listen
uv run talkat long
```

This bypasses the systemd unit entirely — useful when you're editing code
and want fast feedback without restarting a service.

## Usage

The model server runs automatically in the background after installation.

### First Time Setup: Calibration (Required)

**Before first use, calibrate your microphone — stay silent for 10 seconds:**

```bash
talkat calibrate
```

Calibration measures ambient noise and saves an appropriate silence
threshold to `~/.config/talkat/config.json`. Without it, voice activity
detection may not fire on your voice (threshold too high) or may trigger
on background noise (threshold too low). Recalibrate when you switch
microphones, change rooms, or detection stops working reliably.

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
- Saves transcript to `~/.local/share/talkat/transcripts/` incrementally as utterances are recognized
- Automatically copies the full transcript to clipboard on exit (unless `--no-clipboard`)
- **Auto-stops** after `long_mode_silence_timeout` seconds of continuous silence (default 60s) so it cleans up by itself if you walk away
- Hard cap on a single session: `long_mode_max_session_duration` (default 30 minutes)
- You can still stop manually with Ctrl+C (foreground) or `talkat toggle-long` (background)

### Background Long Dictation
Manage long dictation as a background process:
```bash
talkat start-long    # Start long dictation in background
talkat stop-long     # Stop background long dictation
talkat toggle-long   # Toggle: start if stopped, stop if running
```

### Create Shortcuts

Bind commands to keyboard shortcuts. For Niri:

```kdl
# Single key for toggle recording (press to start, press again to stop)
Mod+Apostrophe { spawn "bash" "-c" "talkat listen"; }

# Long dictation mode
Mod+Shift+Apostrophe { spawn "bash" "-c" "talkat long"; }

# Toggle background long dictation
Mod+Ctrl+Apostrophe { spawn "bash" "-c" "talkat toggle-long"; }
```

For Sway:

```sh
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
    "model_name": "small.en",
    "language": "en",
    "save_transcripts": true,
    "clipboard_on_long": true,
    "long_mode_silence_timeout": 60.0,
    "long_mode_max_session_duration": 1800.0,
    "transcript_dir": "~/.local/share/talkat/transcripts"
}
```

### Language

Talkat uses Whisper's language hint to pick the decoder for a given utterance.
Set it in the config file or override per-invocation with `--language`:

```bash
talkat listen --language es           # transcribe a Spanish utterance
talkat long --language fr             # long-mode dictation in French
talkat file input.wav --language de   # transcribe a German audio file
```

Use `"auto"` to have faster-whisper detect the language per utterance.

Notes:
- Vosk ignores `language` — Vosk language is baked into the model file, so
  pick a different Vosk model (e.g. `vosk-model-small-fr`) instead.
- Whisper's English-only variants (`*.en` models) are tuned for English and
  ignore the hint too. For multilingual use, pick the multilingual variant
  (e.g. `small` instead of `small.en`).
- The CLI flag overrides the config file for a single invocation. Server
  uses its own configured default when the client sends no value.

### Model management

Talkat ships with `model_name: "small.en"` as the default. To switch sizes or
download additional models, use the `model` subcommand:

```bash
# List models you've already downloaded
talkat model list

# Download a new model (faster-whisper resolves the name → HuggingFace repo)
talkat model download tiny.en
talkat model download large-v3

# Set the default model for the server to load on next start
talkat model use medium.en
systemctl --user restart talkat   # pick up the change
```

Known faster-whisper sizes: `tiny`, `tiny.en`, `base`, `base.en`, `small`,
`small.en`, `medium`, `medium.en`, `large-v1`, `large-v2`, `large-v3`, `large`,
`large-v3-turbo` / `turbo`, plus the `distil-*` variants. The `.en` suffix
means English-only — smaller and faster but won't transcribe other languages.
For multilingual dictation pair `talkat model use small` with `language: "auto"`.

For community-quantized or fine-tuned models, pass an explicit HuggingFace repo
id: `talkat model download mycorp/my-finetuned-whisper`.

Vosk model management isn't covered by this command — Vosk distributes via
`https://alphacephei.com/vosk/models/` (separate from HuggingFace), so install
those manually under your `vosk_model_base_dir`.

The model server listens on a unix socket at
`$XDG_RUNTIME_DIR/talkat/server.sock` (permissions `0600`) — local-only by
design, no network port to manage. Override with `server_socket` if you
need to.

### AI post-processing (AIPP)

Pipe transcripts through a local or hosted LLM before they hit the keyboard.
Useful for cleaning up grammar, formatting as bullet lists, rewriting as code,
etc. Disabled by default; opt in per invocation with `--postprocess <name>`.

Profiles live under `postprocess_profiles` in `~/.config/talkat/config.json`.
Talkat speaks the OpenAI-compatible `/v1/chat/completions` shape, which
transparently covers Ollama, llama.cpp server, LM Studio, vLLM, OpenRouter,
and OpenAI itself.

```json
{
    "postprocess_profiles": {
        "tidy": {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3.2:3b",
            "system_prompt": "Clean up grammar and punctuation. Keep the meaning identical. Return only the cleaned text.",
            "timeout": 30
        },
        "openai-clean": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "system_prompt": "Format the input as professional prose. Return only the rewritten text.",
            "api_key_env": "OPENAI_API_KEY"
        }
    }
}
```

Then:
```bash
talkat listen --postprocess tidy
talkat long --postprocess tidy        # applied once to the full session at end
talkat file recording.wav --postprocess tidy
talkat batch *.wav -o out/ --postprocess tidy
```

**Security note**: API keys are referenced by environment variable name
(`api_key_env`), never stored in the config file directly. The config file is
therefore safe to commit / share.

**Fail-open**: if the LLM is unreachable, returns an error, or takes too long,
talkat logs the failure, fires a notification, and types the **raw** transcript.
AIPP cannot lose your dictation.

For `long` mode, AIPP runs **once at session end** on the concatenated
transcript (preserves cross-utterance context, single LLM call). The
processed result is written alongside the raw transcript as
`<timestamp>_long.processed.txt` and copied to the clipboard. The raw file
is kept as the source of truth.

#### Verifying AIPP against a real backend

The mocked tests cover validation and fail-open semantics; for a final
smoke test against a real OpenAI-compatible server (Ollama, llama.cpp,
LM Studio, OpenRouter, …), use the `--aipp-live` opt-in:

```bash
# One-time setup (any OpenAI-compat server works; Ollama is the easiest)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:0.5b      # ~400 MB, CPU-fast

# Run only the live tests
uv run pytest --aipp-live -k aipp_live -v
```

The `aipp_live`-marked tests skip by default and skip cleanly with a
friendly message if the backend isn't reachable. Override the defaults
with `OLLAMA_BASE_URL` / `OLLAMA_MODEL` env vars to point at a different
server. CI runs these on every push against a freshly-installed Ollama
(see `.github/workflows/ci.yml::aipp-live`).

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
   - Check status: `systemctl --user status talkat`
   - Check logs: `journalctl --user -u talkat -f`
   - Restart: `systemctl --user restart talkat`
   - Probe the socket: `curl --unix-socket "${XDG_RUNTIME_DIR:-/run/user/$UID}/talkat/server.sock" http://talkat/health`
   - Verify model files are downloaded in `~/.cache/talkat/`

3. If toggle isn't working:
   - PID/lock files live at `${XDG_RUNTIME_DIR:-/run/user/$UID}/talkat/` (typically `/run/user/$UID/talkat/`)
   - Check: `ls "${XDG_RUNTIME_DIR:-/run/user/$UID}/talkat/listen.pid"`
   - Clean stale PIDs: `rm "${XDG_RUNTIME_DIR:-/run/user/$UID}"/talkat/*.pid`
   - Update: `cd talkat && git pull && ./setup.sh`

4. Audio issues:
   - Run calibration: `talkat calibrate` (remember to stay SILENT during calibration)
   - If speech isn't detected, your threshold might be too high
   - If recording triggers on background noise, recalibrate in a quieter environment
   - Check available audio devices: `pactl list sources`
   - Verify PyAudio can access your microphone
