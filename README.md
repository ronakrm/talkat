# Talkat

[![CI](https://github.com/ronakrm/talkat/actions/workflows/ci.yml/badge.svg)](https://github.com/ronakrm/talkat/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/ronakrm/talkat/graph/badge.svg)](https://codecov.io/gh/ronakrm/talkat)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Talkat is voice dictation for Wayland desktops. Press a hotkey, talk, and
your words are typed into whatever window has focus. Speech recognition runs
locally (faster-whisper or Vosk) in a background service, so your audio never
leaves your machine.

Press the hotkey and talkat starts recording. As you talk, it cuts the audio
at natural pauses, transcribes each piece in the background, and types it as
soon as it's ready. Press the hotkey again to stop: the rest is transcribed
and typed, and the transcript is saved. The only thing that can leave your
machine is transcript text, and only if you point
[AI post-processing](#ai-post-processing) at a hosted LLM.

Talkat runs on Linux with a Wayland compositor and Python 3.11+. On niri,
sway and Hyprland it also checks that focus hasn't moved before typing.

[Install](#install) ·
[Quick start](#quick-start) ·
[Usage](#usage) ·
[Configuration](#configuration) ·
[Troubleshooting](#troubleshooting)

## Install

### Arch Linux (AUR)

```bash
yay -S talkat                                   # or: paru -S talkat
sudo pacman -S --needed wl-clipboard libnotify  # optional: clipboard fallback, notifications
```

The package pulls in `python`, `portaudio` and `ydotool`. Do the
[one-time ydotool setup](#one-time-ydotool-setup), then start the model
server:

```bash
systemctl --user enable --now talkat
```

The package ships its own unit (`/usr/lib/systemd/user/talkat.service`), so
don't run `talkat install-service` on top of it.

### Other distributions

You need:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- ydotool 1.0 or newer, including the `ydotoold` daemon
- PortAudio headers and a C compiler, because PyAudio builds from source:
  - Debian/Ubuntu: `sudo apt install portaudio19-dev build-essential python3-dev`
  - Fedora: `sudo dnf install portaudio-devel gcc python3-devel`
- Optional: `wl-clipboard` (or `xclip`) for the clipboard fallback, and
  `libnotify` (`notify-send`) for status notifications

```bash
git clone https://github.com/ronakrm/talkat.git
cd talkat
./setup.sh
```

`setup.sh` installs the `talkat` command into an isolated environment with
`uv tool install`, so it lands in `~/.local/bin`. It then runs
`talkat install-service`, which writes `~/.config/systemd/user/talkat.service`
and enables and starts it. Run it as your normal user, not root. Use one
install method per machine: if the AUR package is already installed,
`setup.sh` warns you and asks before shadowing it.

### One-time ydotool setup

Talkat types through ydotool. Its daemon, `ydotoold`, needs write access to
`/dev/uinput`, and talkat's [modifier guard](#safeguards) reads key state
from `/dev/input`. The `input` group gives both:

```bash
sudo usermod -aG input "$USER"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
    | sudo tee /etc/udev/rules.d/80-uinput.rules > /dev/null
```

Reboot so the group change and the udev rule both take effect. Then keep
`ydotoold` running, either way works:

- Arch's ydotool package ships a user service:
  `systemctl --user enable --now ydotool`
- Or start it from your compositor: niri `spawn-at-startup "ydotoold"`,
  sway `exec ydotoold`, Hyprland `exec-once = ydotoold`

### Upgrade and uninstall

|           | AUR                                                                 | setup.sh                                                    |
|-----------|---------------------------------------------------------------------|-------------------------------------------------------------|
| Upgrade   | `yay -Syu talkat`, then `systemctl --user restart talkat`           | `git pull && ./setup.sh` (it restarts the service)          |
| Uninstall | `systemctl --user disable --now talkat`, then `sudo pacman -R talkat` | `talkat uninstall-service`, then `uv tool uninstall talkat` |

After an upgrade, `talkat doctor` should report the same version for the
command and the server. Uninstalling leaves your settings, models and
transcripts in `~/.config/talkat`, `~/.cache/talkat` and
`~/.local/share/talkat`; delete those to remove everything.

## Quick start

1. **Calibrate.** Stay quiet for 10 seconds while it runs:

   ```bash
   talkat calibrate
   ```

   This measures your room's noise and saves a speech threshold to
   `~/.config/talkat/config.json`. Talkat uses the threshold to find the
   pauses where it cuts your dictation. Run it again when you change
   microphones or rooms.

2. **Bind a hotkey** to `talkat listen`. The same key starts and stops
   recording, so make sure holding it down doesn't repeat the command.

   niri (inside your `binds { }` block):

   ```kdl
   Mod+Apostrophe repeat=false { spawn "talkat" "listen"; }
   Mod+Shift+Apostrophe repeat=false { spawn "talkat" "listen" "--to-file"; }
   ```

   sway:

   ```
   bindsym --no-repeat $mod+apostrophe exec talkat listen
   bindsym --no-repeat $mod+Shift+apostrophe exec talkat listen --to-file
   ```

   Hyprland (plain `bind` doesn't repeat; don't use `binde`):

   ```
   bind = SUPER, apostrophe, exec, talkat listen
   bind = SUPER SHIFT, apostrophe, exec, talkat listen --to-file
   ```

   The second binding is optional. It takes long-form notes into a file
   instead of typing (see [`--to-file`](#long-form-notes---to-file)). If you
   installed with `setup.sh` and your compositor can't find `talkat`, use
   the full path `~/.local/bin/talkat`.

3. **Dictate.** Focus a text field, press the hotkey, talk, and press it
   again. The first time the service starts it downloads the default model
   (`small.en`, a few hundred MB), so give it a minute.
   `journalctl --user -u talkat -f` shows the progress.

4. **If something's off,** run `talkat doctor` (see
   [Troubleshooting](#troubleshooting)).

## Usage

### Dictation

```bash
talkat listen   # start recording
talkat listen   # stop
```

While talkat records:

- **It types as you talk.** The recording is cut at natural pauses, and each
  piece is transcribed in the background and typed as soon as it's ready,
  usually a second or two after you finish a sentence.
- **Pauses don't stop it.** Recording ends when you toggle it off, after
  `idle_timeout` (60 s) with no speech, or at `max_recording_duration`
  (10 minutes, a safety net). While it's quiet, a notification every
  `idle_notify_interval` (30 s) reminds you it's still recording. Another
  tells you when it stops on its own.
- **It keeps a transcript** in
  `~/.local/share/talkat/transcripts/<YYYYMMDD_HHMMSS>_dictation.txt`
  (see `save_transcripts` and `transcript_dir`).

`listen` options:

| Option | Effect |
|---|---|
| `--to-file` | Long-form notes: append to a file instead of typing ([below](#long-form-notes---to-file)) |
| `-o FILE` | Write the transcript to `FILE` when you stop, instead of typing |
| `--postprocess PROFILE` | Run the transcript through an [AI post-processing](#ai-post-processing) profile before it's typed |
| `--language CODE` | Language for this run (`es`, `de`, `auto`, …) |
| `--max-recording SECONDS` | Recording cap for this run (overrides `max_recording_duration`) |
| `--http-timeout SECONDS` | Model server request timeout for this run (overrides `http_timeout`) |
| `--try-lock` | For scripts: fail instead of stopping a running recording or waiting for another talkat command |

With `-o`, `--postprocess`, or `"output_mode": "clipboard"`, the transcript
is delivered once when you stop, not typed as you talk.

#### Safeguards

Talkat never silently loses a transcript. A notification tells you whenever
text goes somewhere other than the window you were typing in.

- **Focus guard.** Text goes only to the window that had focus when you
  started. If focus moves (alt-tab, a popup), typing stops and the rest goes
  to the clipboard. This works on niri, sway and Hyprland and is off on other
  compositors. Turn it off with `"focus_guard": false`.
- **Modifier guard.** Keys that talkat types combine with keys you're
  holding, so with Super held down every typed letter becomes a compositor
  shortcut. Talkat types one key at a time and pauses while Ctrl, Shift, Alt
  or Super is held. If one stays held for 5 seconds, the rest goes to the
  clipboard. This guard needs the `input` group (see
  [ydotool setup](#one-time-ydotool-setup)).
- **Clipboard fallback.** If ydotool is missing or `ydotoold` isn't running,
  the transcript goes to the clipboard (`wl-copy`, else `xclip`). With
  neither installed, it's printed to standard output.
- **Saved audio.** If a piece still can't be transcribed after retries (the
  model server is down or keeps failing), its audio is saved under
  `~/.local/share/talkat/untranscribed/` and typing stops. Everything from
  that point goes to the clipboard, with a marker in place of the missing
  part, such as
  `[untranscribed audio: talkat file ~/.local/share/talkat/untranscribed/20260916_110407_003.wav]`.
  Run that command later to recover the text. After `max_consecutive_errors`
  (5) failures in a row, recording stops.

### Long-form notes (`--to-file`)

This is the same toggle with a different output: nothing is typed. Each
piece is appended to a transcript file as it's recognized, and the whole
transcript goes to the clipboard when you stop.

```bash
talkat listen --to-file               # ~/.local/share/talkat/transcripts/<YYYYMMDD_HHMMSS>_dictation.txt
talkat listen --to-file -o notes.txt  # append to a file you choose
```

With `--postprocess`, the cleaned-up text is saved next to the raw file (for
example `notes.processed.txt`), and that version goes to the clipboard. For
sessions with long silences, raise `idle_timeout`. `max_recording_duration`
can go up to an hour.

### Transcribing audio files

```bash
talkat file meeting.mp3                        # print the transcript
talkat file meeting.mp3 -f srt -o meeting.srt  # subtitles
talkat file memo.wav -c                        # also copy it to the clipboard
talkat batch *.wav -o transcripts/             # one output file per input
```

Input can be wav, mp3, flac or other common audio formats. Output formats
are `text` (the default), `json`, `srt` and `vtt`. Files go through the
running model server, and uploads larger than `max_upload_size_mb` (100 MB)
are refused. `--language` and `--postprocess` work here too.

### Models

```bash
talkat model list                 # downloaded models and their sizes
talkat model download medium.en   # download a model
talkat model use medium.en        # make it the default (saves model_name)
systemctl --user restart talkat   # the server loads the model when it starts
```

The faster-whisper sizes are `tiny`, `base`, `small` and `medium`, each
with an English-only `.en` variant; `large-v1`, `large-v2`, `large-v3`,
`large`, `large-v3-turbo` (also `turbo`); and `distil-small.en`,
`distil-medium.en`, `distil-large-v2` and `distil-large-v3`. Larger models
are more accurate but slower, and the `.en` models only handle English. You
can `use` a model you haven't downloaded yet: the server fetches it on its
next start. Any HuggingFace repo with a faster-whisper (CTranslate2) model
also works, for example `talkat model download org/repo`.

**Vosk.** `talkat model` only manages faster-whisper models. To use Vosk,
download a model from
[alphacephei.com/vosk/models](https://alphacephei.com/vosk/models), unpack it
into `~/.cache/talkat/models/vosk/`, and set `model_name` to the unpacked
directory's name:

```json
{ "model_type": "vosk", "model_name": "vosk-model-small-en-us-0.15" }
```

Vosk ignores `language` and the [custom vocabulary](#custom-vocabulary),
because the language is part of the model.

**GPU.** Set `"fw_device": "cuda"`, usually together with
`"fw_compute_type": "float16"`. This needs an NVIDIA GPU and the CUDA
libraries that faster-whisper requires.

### Language

The default is `en`. Change `language` in your config, or set it for one
run:

```bash
talkat listen --language es
talkat file interview.mp3 --language de
```

With `auto`, faster-whisper detects the language of each piece.
English-only models (`*.en`) ignore this setting. For other languages, switch
to a multilingual model, for example `talkat model use small` together with
`"language": "auto"`.

### Custom vocabulary

Put names, jargon and spellings that talkat keeps getting wrong in
`~/.config/talkat/dictionary.txt`, one per line. They're passed to
faster-whisper as a hint. The server reads this file when it starts, so
restart it after editing: `systemctl --user restart talkat`.

### AI post-processing

You can run the transcript through an LLM before it's delivered, to fix
grammar, format it as a list, or rewrite it as code. Talkat uses the
OpenAI-compatible chat completions API, which Ollama, llama.cpp's server,
LM Studio, vLLM, OpenRouter and OpenAI all provide. Define profiles in your
config ([schema](#ai-post-processing-profiles)) and pick one per run:

```bash
talkat listen --postprocess tidy
talkat listen --to-file --postprocess tidy   # applied once, to the whole transcript
talkat file recording.wav --postprocess tidy
talkat batch *.wav -o out/ --postprocess tidy
```

With `--postprocess`, the transcript is typed once after processing, not as
you talk. If the LLM is unreachable, returns an error, or times out, you get
the raw transcript and a notification instead. The transcript text goes to
the profile's `base_url`, so use a local server if it shouldn't leave your
machine.

## Configuration

Settings live in `~/.config/talkat/config.json` (under `$XDG_CONFIG_HOME` if
you set it). An optional `/etc/talkat/config.json` provides system-wide
defaults. Talkat applies its built-in defaults first, then `/etc`, then your
file, one key at a time, so your file only needs the settings you change:

```json
{
    "silence_threshold": 180.0,
    "model_name": "medium.en",
    "idle_timeout": 120,
    "input_device_name": "headset",
    "typing_key_hold_ms": 10
}
```

- `talkat calibrate` and `talkat model use` change only their own setting
  in your file (`silence_threshold` and `model_name`). If the file isn't
  valid JSON, they leave it untouched and tell you instead.
- Command-line flags (`--language`, `--max-recording`, `--http-timeout`)
  override the file for one run.
- Changes apply to the next `talkat` command. The exception is settings the
  model server reads when it starts: everything under
  [Model & recognition](#model--recognition) except `language`, plus
  `server_socket` and `max_upload_size_mb`. After changing those, run
  `systemctl --user restart talkat`.

**Validation.** Each setting is checked on its own:

- An invalid value, such as the wrong type or out of range, is ignored.
  Talkat falls back to the value from `/etc/talkat/config.json` or the
  built-in default, and still applies the rest of the file. Only a file that
  isn't a valid JSON object is ignored entirely.
- Unknown keys are flagged, with a suggestion for likely typos
  (`idle_timout` → `idle_timeout`).
- Numbers must be JSON numbers (`60`, not `"60"`), and switches must be
  `true` or `false`. Settings with whole-number defaults, such as counts and
  the `_ms` settings, take whole numbers only.
- Paths may start with `~`; otherwise they must be absolute. Symlinks are
  fine.

`talkat doctor` lists every ignored or unknown setting, and the log records
them too. The old `device` and `model_cache_dir` keys no longer exist; use
`fw_device`, `faster_whisper_model_cache_dir` and `vosk_model_base_dir`
instead.

### Settings reference

Durations are in seconds unless the name ends in `_ms`. Ranges are
inclusive.

#### Recording & silence

| Key | Default | Meaning |
|---|---|---|
| `silence_threshold` | `200.0` | Level that separates speech from pauses. It decides where a recording is cut into pieces, never what's sent. Set by `talkat calibrate`. 0–10000 |
| `silence_threshold_fallback` | `500.0` | Threshold `calibrate` saves if it can't measure the room. 0–10000 |
| `silence_threshold_min` | `50.0` | Lowest threshold `calibrate` will save. 0–10000 |
| `silence_threshold_max` | `5000.0` | Highest threshold `calibrate` will save. 0–10000 |
| `input_device_name` | `null` | Use the microphone whose name contains this text, ignoring case. `null` uses the system default input. `talkat -v calibrate` logs the available device names. Up to 256 characters |
| `max_recording_duration` | `600` | Hard cap on one recording. 0–3600 |
| `idle_timeout` | `60` | Stop after this long without transcribed speech. 5–86400 |
| `idle_notify_interval` | `30` | While it's quiet, remind you this often that recording is still on. 5–3600 |
| `max_consecutive_errors` | `5` | Stop after this many pieces in a row fail to transcribe (their audio is saved). 1–100 |
| `audio_normalize_gain` | `true` | Even out quiet and loud input before recognition |
| `audio_target_rms_dbfs` | `-20.0` | Target level for that normalization, in dBFS. −60–0 |
| `audio_max_gain_db` | `20.0` | Most gain normalization may add, so background noise isn't amplified. 0–60 |

#### Model & recognition

| Key | Default | Meaning |
|---|---|---|
| `model_type` | `"faster-whisper"` | `faster-whisper` or `vosk` |
| `model_name` | `"small.en"` | faster-whisper size or HuggingFace repo id, or the Vosk model's directory name. Set by `talkat model use` |
| `language` | `"en"` | 2- or 3-letter ISO 639 code (`es`, `de`, `yue`) or `auto`. Ignored by `.en` and Vosk models |
| `fw_device` | `"cpu"` | `cpu`, `cuda` or `auto` |
| `fw_compute_type` | `"int8"` | `int8`, `float16` or `float32` |
| `fw_device_index` | `0` | Which GPU to use with `cuda`. 0–100 |
| `faster_whisper_model_cache_dir` | `~/.cache/talkat/models/faster-whisper` | Where faster-whisper models are stored |
| `vosk_model_base_dir` | `~/.cache/talkat/models/vosk` | Where Vosk model directories live |
| `dictionary_file` | `~/.config/talkat/dictionary.txt` | [Custom vocabulary](#custom-vocabulary), one word or phrase per line |
| `max_segment_seconds` | `480` | Audio longer than this is split at quiet points and transcribed in parts. This mostly affects long files. 5–3600 |

The model server runs sandboxed and can only write under `~/.cache/talkat`,
`~/.local/share/talkat` and `~/.config/talkat`. If you move a model
directory elsewhere, allow the new path with `systemctl --user edit talkat`:

```ini
[Service]
ReadWritePaths=/path/to/models
```

#### Output & typing

| Key | Default | Meaning |
|---|---|---|
| `output_mode` | `"type"` | `type` types into the focused window. `clipboard` never types and copies the transcript when you stop |
| `focus_guard` | `true` | Stop typing if the focused window changes (niri, sway, Hyprland) |
| `typing_key_hold_ms` | `5` | How long each key is held down, in ms (ydotool's `--key-hold`). Lower is faster; raise it if an app drops characters. 0–100 |
| `typing_key_delay_ms` | `0` | Pause between keystrokes, in ms. Raise it if an app drops or reorders characters. 0–100 |
| `save_transcripts` | `true` | Save a transcript of each dictation |
| `transcript_dir` | `~/.local/share/talkat/transcripts` | Where transcripts are saved |
| `postprocess_profiles` | `{}` | AI post-processing profiles ([schema](#ai-post-processing-profiles)) |

#### Server & network

| Key | Default | Meaning |
|---|---|---|
| `server_socket` | `$XDG_RUNTIME_DIR/talkat/server.sock` | Unix socket that the server and the `talkat` command share |
| `http_timeout` | `120` | How long to wait for one transcription request. 0–3600 |
| `health_check_timeout` | `2` | How long to wait for the server's health check. 0–60 |
| `file_processing_timeout_base` | `30` | Minimum timeout for `talkat file` and `batch`; longer files get twice their duration. 0–3600 |
| `max_upload_size_mb` | `100` | Largest file `talkat file` and `batch` will send. 1–2048 |

#### Process timing (rarely needed)

| Key | Default | Meaning |
|---|---|---|
| `process_stop_timeout` | `300` | How long a stopping `talkat listen` waits for the last pieces to be transcribed and typed before forcing the recording to end. On a forced stop, untyped text goes to the clipboard and untranscribed audio is saved. 0–300 |
| `lock_acquire_timeout` | `1.0` | How long a command waits for another talkat command to finish. 0–300 |
| `lock_retry_interval` | `0.01` | Pause between lock attempts. 0–10 |
| `process_check_interval` | `0.1` | How often a stopping command checks whether the recording has exited. 0–10 |
| `background_process_delay` | `0.5` | Pause after force-killing a recording that won't exit. 0–60 |

### AI post-processing profiles

`postprocess_profiles` maps a profile name to its settings:

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

| Field | Required | Meaning |
|---|---|---|
| `base_url` | yes | `http://` or `https://` base URL of an OpenAI-compatible API. Talkat posts to `<base_url>/chat/completions` |
| `model` | yes | Model id sent with each request, up to 256 characters |
| `system_prompt` | yes | Instructions for the rewrite |
| `api_key_env` | no | Name of the environment variable that holds the API key |
| `timeout` | no | Seconds to wait for the LLM, more than 0 and up to 600. Default 30 |

Any other field is an error. The API key itself never goes in the config
file, only the name of the variable that holds it, so the file is safe to
share. That variable must be set where talkat runs. For a hotkey, that's
your compositor's environment, not just your shell.

## Troubleshooting

Start with `talkat doctor`. It checks:

- which talkat you're running, and whether a stale copy shadows it on PATH
  or in systemd
- the model server, and that the server and the command are the same
  version
- ydotool, the clipboard and notification tools, and the focus and modifier
  guards
- your audio devices
- your config files

It exits non-zero if anything failed.

**Text lands on the clipboard instead of being typed.** The notification
says why:

- If ydotool is missing or `ydotoold` isn't running, see
  [ydotool setup](#one-time-ydotool-setup). To test ydotool on its own,
  run `ydotool type hello` in a terminal; it should type `hello` at your
  prompt.
- If focus moved during dictation, that's the focus guard working.
  `"focus_guard": false` turns it off.
- If a modifier key stayed held, typing pauses while Ctrl, Shift, Alt or
  Super is down. Check for a stuck key.

**Characters go missing or arrive out of order.** Some apps can't keep up
with fast typing. Raise `typing_key_hold_ms` (ydotool's own default is 20)
or `typing_key_delay_ms`.

**The server isn't responding.** Check the service, read its log, and
restart it:

```bash
systemctl --user status talkat
journalctl --user -u talkat -e
systemctl --user restart talkat
curl --unix-socket "$XDG_RUNTIME_DIR/talkat/server.sock" http://talkat/health
```

The first start downloads the model, so it can take a while.

**Wrong microphone, or no audio.** The Audio section of `talkat doctor`
shows the default input and whether your `input_device_name` matches a
device. `talkat -v calibrate` logs every input device name.

**Text arrives late in big chunks, or pieces are cut mid-word.** Run
`talkat calibrate` again. A threshold set too low hears room noise as speech,
finds no pauses, and falls back to cutting every 30 seconds. One set too high
mistakes quiet speech for pauses and cuts mid-word.

**The hotkey starts and immediately stops.** Your binding repeats while the
key is held. Use `repeat=false` (niri) or `--no-repeat` (sway).

**The toggle gets confused after a crash.** If no recording is running
(`pgrep -af "talkat listen"` shows nothing), delete the stale PID file:
`rm "$XDG_RUNTIME_DIR/talkat/listen.pid"`.

**Logs and diagnostics:**

- The command and the server both log to
  `~/.local/share/talkat/logs/talkat.log`, which rotates. The server's
  output is also in `journalctl --user -u talkat`.
- For debug detail in the log, add `-v` (`talkat -v listen`, which works in
  a hotkey binding too), or set `TALKAT_DEBUG=1`.
- Each dictation writes timings, the model used, and any errors to
  `~/.local/share/talkat/diagnostics/diagnostics.latest.json`. The last 200
  records are kept.

## Development

[CONTRIBUTING.md](CONTRIBUTING.md) covers running a checkout without
disturbing your installed copy, the checks CI runs, and packaging.
Architecture notes and the release process are in [CLAUDE.md](CLAUDE.md).

## License

[MIT](LICENSE)
