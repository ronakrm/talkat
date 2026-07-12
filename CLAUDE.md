# CLAUDE.md

This file provides comprehensive guidance to Claude Code (claude.ai/code) when working with the Talkat codebase.

## Project Overview

Talkat is a voice-to-text dictation system for Wayland Linux compositors. It runs a local speech recognition server and allows users to dictate text into any application using voice commands.

## Architecture

### Client-Server Design
```
┌─────────────┐       HTTP        ┌──────────────┐
│   Client    │◄──────────────────►│ Model Server │
│  (main.py)  │   /transcribe      │(model_server)│
│             │   /transcribe_stream│              │
└──────┬──────┘                    └──────┬───────┘
       │                                   │
   Audio Input                      Speech Models
   (pyaudio)                    (Faster-Whisper/Vosk)
       │
   VAD + Stream
   (record.py)
       │
   Text Output
   (ydotool)
       │
   Toggle Control
   (PID tracking)
```

### Core Components

1. **Model Server** (`model_server.py`)
   - Flask app served by `waitress` over a unix domain socket at
     `$XDG_RUNTIME_DIR/talkat/server.sock` (perms 0600)
   - Loads and manages speech recognition models
   - Provides streaming + file transcription endpoints
   - Supports Faster-Whisper (default) and Vosk models

2. **Client** (`main.py`)
   - Captures audio from microphone and streams it to the server
   - Types recognized text via ydotool (Wayland), with a focus guard and
     clipboard fallback — the transcript is never silently lost
   - **Toggle support**: PID file tracking for start/stop with same command
   - Graceful interruption handling via signals

3. **Audio capture** (`record.py`, `devices.py`)
   - `AudioSession` streams **every** chunk from the moment the mic opens;
     the calibrated threshold only decides when the utterance is over
     (silence auto-stop). Client-side gating of what gets *sent* is exactly
     how utterance beginnings used to get clipped — don't reintroduce it.
   - Device index is resolved on the same PyAudio instance that opens the
     stream (PortAudio snapshots topology per instance; PipeWire churns
     indices), with one retry on a fresh instance.
   - `input_device_name` config pins the capture device by name substring.

4. **CLI Router** (`cli.py`)
   - Command routing and argument parsing
   - PID file management for toggle functionality
   - Background process management for long dictation
   - Heavy imports (file processing) are lazy — `talkat listen` is on the
     hotkey hot path

5. **Configuration** (`config.py`)
   - Hierarchical config: defaults → `/etc/talkat/config.json` →
     `~/.config/talkat/config.json` → CLI args
   - `save_app_config` persists **only** values that differ from
     `CODE_DEFAULTS` and drops dead keys — never freeze defaults into the
     user's file
   - Model cache at `~/.cache/talkat/`
   - Transcripts at `~/.local/share/talkat/transcripts/`
   - PID and lock files at `$XDG_RUNTIME_DIR/talkat/` (typically `/run/user/$UID/talkat/`), with a fallback to `~/.cache/talkat/runtime/` when `XDG_RUNTIME_DIR` is unavailable
   - `TALKAT_RUNTIME_DIR` env var relocates the whole runtime dir (socket +
     PIDs + locks) — the dev-isolation mechanism behind `./dev.sh`

6. **Focus guard** (`focus.py`)
   - Queries the focused window over compositor IPC (niri, Hyprland, sway)
   - `listen` captures the window at recording start and refuses to type if
     focus changed — transcript goes to the clipboard instead
   - All failure paths return `None` = "guard off", never "focus changed"

7. **Environment self-check** (`doctor.py`)
   - `talkat doctor`: install origin, PATH/systemd shadowing, server health
     + version skew, audio devices, ydotoold/clipboard/notification tooling
   - First thing to run when behavior looks stale or inconsistent

## Development Workflow

### The dual-install model (IMPORTANT)

Daily use and development are deliberately separate installs:

- **Daily driver**: the AUR package (`/usr/bin/talkat` + packaged systemd
  unit `/usr/lib/systemd/user/talkat.service`). Desktop hotkeys resolve
  `talkat` through PATH and hit this.
- **Development**: this checkout, run via `./dev.sh` — which is
  `uv run talkat` plus a `TALKAT_RUNTIME_DIR` override pointing at
  `$XDG_RUNTIME_DIR/talkat-dev/`. The dev server binds its own socket and
  the dev client uses its own PID/lock files, so testing the checkout can
  never toggle, stop, or out-bind the installed service.

Do NOT run `setup.sh` on a machine that has the AUR package — the uv-tool
install shadows `/usr/bin/talkat` on PATH and its user systemd unit shadows
the packaged unit, and the shadowing copy silently goes stale. (This
happened; `talkat doctor` now detects it, and `setup.sh` warns.) `setup.sh`
remains the install path for non-Arch systems only.

To get code changes into daily use: tag a release and bump the AUR package
(see Installation below), don't re-run `setup.sh`.

### Setup Development Environment
```bash
git clone <repo>
cd talkat
uv sync

# Dev server + client, isolated from any installed talkat:
./dev.sh server     # terminal 1
./dev.sh listen     # terminal 2
./dev.sh doctor     # environment report as the dev build sees it
```

### Automated tests
```bash
uv run pytest                 # full suite (~430 tests, no mic/model needed)
uv run pytest tests/test_vad.py -q
uv run mypy src/talkat/       # strict typing is enforced in CI
uvx ruff@0.1.14 format --check . && uvx ruff@0.1.14 check .  # CI-pinned ruff
```

### Manual testing workflow
```bash
# All through dev.sh so the installed service is untouched:
./dev.sh listen       # toggle: run again to stop
./dev.sh long
./dev.sh calibrate
./dev.sh start-long; ./dev.sh stop-long; ./dev.sh toggle-long
```

### Installation

Talkat is a per-user dictation tool. Two supported install paths:

#### Packaged install (Arch / AUR) — the daily driver on this machine
Install via an AUR helper (`yay -S talkat`). The package installs
`/usr/bin/talkat` and ships `/usr/lib/systemd/user/talkat.service`
(the repo's `talkat.service`). Enable with:
```bash
systemctl --user enable --now talkat
```

The PKGBUILD lives in the AUR git repo
(`ssh://aur@aur.archlinux.org/talkat.git`), not in this source tree — that's
standard Arch packaging convention. Release flow: bump `version` in
`pyproject.toml` + stamp `CHANGELOG.md` → tag `vX.Y.Z` here → in the AUR
clone, bump `pkgver`, run `updpkgsums`, regenerate `.SRCINFO`, push →
`yay -Syu talkat` → `systemctl --user restart talkat` → `talkat doctor`
to confirm client and server report the new version.

#### Local install from a git checkout (non-Arch systems)
```bash
./setup.sh
```
Runs `uv tool install --reinstall .` (creates an isolated venv under
`~/.local/share/uv/tools/talkat/`) and then `talkat install-service` to write
`~/.config/systemd/user/talkat.service` pointing at that venv's interpreter.
Refuses (with a prompt) when a system-packaged talkat exists, because the
uv-tool copy would shadow it.

To uninstall:
```bash
talkat uninstall-service
uv tool uninstall talkat
```

**After code changes**: use `./dev.sh` for testing (no reinstall needed).
On non-Arch machines where `setup.sh` IS the install, re-run it to update;
it restarts the running service.

## Code Patterns and Conventions

### Type Hints
- mypy strict (`disallow_untyped_defs`) is enforced in CI — annotate everything
- Use modern syntax: `float | None`, `list[str]`, `dict[str, Any]` (not
  `Optional`/`List`/`Dict`)
- Import collection ABCs from `collections.abc` (`Iterator`, `Callable`, ...)

Example:
```python
from collections.abc import Iterator

def process_audio(
    data: bytes,
    sample_rate: int,
    threshold: float | None = None,
) -> tuple[str, float]:
    ...
```

### Error Handling
- Use specific exception types
- Always clean up resources in finally blocks
- Log errors with context (avoid bare prints)
- Provide fallback behavior where appropriate

Example:
```python
try:
    response = session.post(url, data=data)
    response.raise_for_status()
except requests.ConnectionError as e:
    logger.error(f"Failed to connect to server: {e}")
    return None
except requests.Timeout as e:
    logger.error(f"Request timed out: {e}")
    return None
finally:
    session.close()
```

### Resource Management
- Use context managers for file/network operations
- Clean up audio streams properly
- Manage subprocess lifecycle carefully
- Handle signals gracefully (SIGINT, SIGTERM)

### Audio Processing Patterns
```python
# AudioSession owns the PyAudio lifecycle; iterate it for chunks.
with AudioSession(threshold=200.0) as session:
    for chunk in session:      # every chunk from mic-open onward
        send(chunk)
# Iteration ends on: post-speech silence > silence_duration,
# max_duration reached, or stop_event set.
```

Key invariant: the threshold decides when to STOP, never what to SEND.
Anything that drops audio client-side before the server sees it will clip
utterance beginnings for quiet speakers — the server-side VAD filter is the
component responsible for trimming silence.

## File Structure
```
talkat/
├── src/talkat/
│   ├── __init__.py       # Package marker
│   ├── cli.py            # CLI entry point and command routing
│   ├── main.py           # Client orchestration (listen/long, delivery)
│   ├── record.py         # AudioSession (capture + silence auto-stop), calibration
│   ├── devices.py        # Audio device discovery/pinning
│   ├── focus.py          # Focused-window queries (niri/Hyprland/sway IPC)
│   ├── model_server.py   # Flask + waitress server over unix socket
│   ├── backends.py       # TranscriptionBackend Protocol + faster-whisper/Vosk
│   ├── audio_utils.py    # Gain normalization, long-form segmentation
│   ├── model_manager.py  # talkat model {list,download,use}
│   ├── file_processor.py # Audio file/batch transcription client
│   ├── postprocess.py    # AIPP: LLM cleanup via OpenAI-compatible endpoint
│   ├── config.py         # Layered config load/save (save prunes defaults)
│   ├── paths.py          # XDG paths + TALKAT_RUNTIME_DIR override
│   ├── process_manager.py# flock-based locks, PID files, background procs
│   ├── security.py       # Input validation, safe subprocess wrapper
│   ├── clipboard.py      # wl-copy → xclip fallback
│   ├── diagnostics.py    # Per-run diagnostics JSON (+retention)
│   ├── doctor.py         # talkat doctor environment self-check
│   ├── logging_config.py # Logging setup (console + rotating file)
│   └── service.py        # install/uninstall the user systemd unit
├── tests/                # pytest suite (~430 tests; no mic/model needed)
├── dev.sh                # Run the checkout against an isolated runtime dir
├── setup.sh              # uv-tool install (non-Arch systems)
├── talkat.service        # Unit shipped by the Arch package
├── pyproject.toml        # Project configuration
├── CLAUDE.md             # This file
└── README.md             # User documentation
```

## Common Tasks

### Adding a New Command
1. Add parser in `cli.py` (keep heavy imports lazy in the dispatch branch)
2. Implement handler function in appropriate module
3. Add tests; update CLAUDE.md and README.md
4. Verify with `./dev.sh <command>` — don't reinstall

### Adding a New Model Backend
1. Implement the `TranscriptionBackend` Protocol in `backends.py`
2. Register it in `create_backend`
3. Add configuration option in `config.py` (+ validation in `security.py`)
4. Add model download logic

### Changing capture / silence-stop behavior
1. Modify `AudioSession.__iter__` in `record.py`
2. Preserve the invariant: threshold decides when to stop, never what to send
3. Update `tests/test_vad.py`; test with quiet speakers and noisy rooms

## Testing Checklist

### Automated tests

`uv run pytest` covers config, security, VAD/AudioSession, listen/long
modes, focus guard, doctor, process manager, file processor, CLI dispatch,
AIPP, and integration paths over a real Flask+waitress UDS server. CI runs
the suite on Python 3.11–3.14 plus ruff (pinned 0.1.14) and strict mypy.

### Manual verification (audio hardware paths)
Things the suite can't cover — worth a manual pass before a release:

1. **Basic Functionality**
   - [ ] Server starts successfully
   - [ ] Client connects to server
   - [ ] Audio capture works
   - [ ] Transcription produces text
   - [ ] Text is typed correctly
   - [ ] Toggle functionality works (second call stops recording)

2. **Modes**
   - [ ] Listen mode (single utterance)
   - [ ] Listen mode toggle (start/stop with same command)
   - [ ] Long mode (continuous)
   - [ ] Background long mode (start-long/stop-long)
   - [ ] Toggle-long mode
   - [ ] Calibration mode

3. **Edge Cases**
   - [ ] Server not running
   - [ ] Microphone not available
   - [ ] Network issues
   - [ ] Long silence periods
   - [ ] Very long utterances
   - [ ] Rapid speech
   - [ ] Background noise

4. **Installation**
   - [ ] Fresh system-wide install
   - [ ] Fresh user install
   - [ ] Update existing installation
   - [ ] Service management

## Performance Considerations

### Memory Usage
- Audio buffers grow with recording length
- Pre-speech buffer uses circular buffer (deque)
- Model stays loaded in server memory
- Consider streaming for large files

### Network Optimization
- Use session pooling for multiple requests
- Stream audio instead of batch for long recordings
- Implement compression for network transfer
- Consider WebSocket for bidirectional streaming

### Model Performance
- Faster-Whisper: Better accuracy, higher resource usage
- Vosk: Lower resource usage, faster, less accurate
- Consider model quantization for speed
- Implement model warm-up on server start

## Security Considerations

1. **Network Security**
   - Server binds to localhost only (127.0.0.1)
   - No authentication (local use only)
   - Consider adding token-based auth for network use

2. **File Permissions**
   - Config files are user-readable only
   - Transcripts saved with user permissions
   - Service runs as user, not root

3. **Input Validation**
   - Validate audio format and sample rate
   - Sanitize file paths
   - Limit request sizes

## Future Improvements

See `docs/future-work.md` for the considered-and-deferred list (multi-engine
cross-check is the headline deferred design).

### High Priority
1. ~~Add toggle functionality for listen mode~~ ✅
2. ~~Comprehensive type hints~~ ✅ (mypy strict in CI)
3. ~~Proper logging framework~~ ✅ (`logging_config.py`, rotating file log)
4. ~~Automated tests~~ ✅ (~430 tests + CI)
5. ~~Support audio file input (.wav, .mp3)~~ ✅
6. ~~Never lose a transcript on delivery failure~~ ✅ (clipboard fallback)
7. Keep the mic open across utterances in long mode (today it reopens per
   utterance; speech during the reopen gap is lost)

### Medium Priority
1. Multi-engine ASR cross-check (see docs/future-work.md sketch)
2. WebSocket / bidirectional streaming with partial results
3. Custom wake word detection
4. Speaker diarization for meetings

### Low Priority
1. GUI configuration tool
2. Cloud model support
3. Integration with other desktop environments

## Dependencies

### Python Packages
- `faster-whisper` - Primary ASR engine
- `vosk` - Alternative ASR engine
- `pyaudio` - Audio I/O
- `numpy` - Array operations
- `flask` + `waitress` - HTTP server over unix socket
- `httpx` - HTTP client (unix-socket transport)
- `librosa` + `soundfile` - Audio file processing

(Exact version bounds live in `pyproject.toml` — that's the source of truth.)

### System Requirements
- `ydotool` - Wayland input automation
- `wl-copy` or `xclip` - Clipboard operations
- `notify-send` - Desktop notifications
- `systemd` - Service management
- `uv` - Python package manager

## Debugging Tips

### Known Issues and Design Notes

Resolved former known-bugs (kept here so they aren't re-reported):
- ~~500 errors on long recordings~~ → long-form segmentation
  (`audio_utils.segment_long_audio`, `max_segment_seconds`)
- ~~PID file races~~ → `flock`-based `ProcessManager.locked()` + atomic PID
  writes
- ~~Device selection falls back without proper error~~ → same-instance
  resolution + retry + `input_device_name` pin; failures raise
  `AudioSessionError` with a notification
- ~~Cut-off utterance beginnings~~ → stream-from-open (threshold only stops)
- ~~Transcript lost when typing fails~~ → clipboard/stdout fallback chain

Still true / watch out for:
1. **Signal handling is subtle** — the FIRST SIGINT/SIGTERM only sets the
   stop event: the capture loop notices within one ~32 ms chunk, the
   streaming request completes, and the transcript is delivered. A SECOND
   signal raises `KeyboardInterrupt` to force-abort a blocked wait (hung
   server — PEP 475 would otherwise swallow the signal until
   `http_timeout`). Raising on the *first* signal was the v1.0.0 bug that
   made every toggle-stop log "Recording interrupted." and lose the
   transcript. `tests/test_toggle_signal.py` pins this with real signals;
   still re-test toggle on real hardware when touching it.
2. **Long mode reopens the mic between utterances** — speech during that
   ~0.3 s gap is lost (tracked in Future Improvements).
3. **ALSA/JACK init noise** is fd-level suppressed
   (`_suppress_native_stderr`), so genuine PortAudio warnings are also
   hidden inside that block.
4. **Memory** — unverified suspicion of slow growth in a long-running
   server with certain models; no reproduction yet.

### Common Issues

0. **Anything looks stale or inconsistent** → `talkat doctor` first. It
   catches the big one: a stale uv-tool install (PATH) or user systemd unit
   shadowing the AUR package, i.e. you're not running the code you think.

1. **"Server not responding"**
   - Check: `systemctl --user status talkat`
   - Check socket: `ls -la "${XDG_RUNTIME_DIR:-/run/user/$UID}/talkat/server.sock"`
   - Probe: `curl --unix-socket "${XDG_RUNTIME_DIR:-/run/user/$UID}/talkat/server.sock" http://talkat/health`
   - Try: `systemctl --user restart talkat`

2. **"No audio input"**
   - Check: `pactl list sources`
   - Try: `talkat calibrate`
   - Pin a device: `"input_device_name"` in config

3. **"Text not typing"**
   - Check: ydotoold is running (`talkat doctor` shows the socket)
   - If focus moved mid-dictation the focus guard diverts to the clipboard
     by design (`focus_guard: false` disables)
   - Test: `ydotool type "test"`

4. **"Toggle not working"**
   - Check PID file: `ls "${XDG_RUNTIME_DIR:-/run/user/$UID}/talkat/listen.pid"`
   - Clean up stale PIDs: `rm "${XDG_RUNTIME_DIR:-/run/user/$UID}"/talkat/*.pid`

### Debug Mode
```bash
talkat -v listen        # DEBUG logging (console + file)
talkat --debug listen   # DEBUG + background process output to log files
TALKAT_DEBUG=1 talkat listen   # env-var equivalent
# Rotating log file: ~/.local/share/talkat/logs/talkat.log
```

### Performance Profiling
```python
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()
# ... code to profile ...
profiler.disable()
stats = pstats.Stats(profiler).sort_stats('cumulative')
stats.print_stats()
```

## Contributing Guidelines

1. **Code Style**
   - Follow PEP 8
   - Use type hints
   - Add docstrings for public functions
   - Keep functions under 50 lines

2. **Testing**
   - Test manually before committing
   - Document test scenarios
   - Check edge cases

3. **Documentation**
   - Update CLAUDE.md for architectural changes
   - Update README.md for user-facing changes
   - Add inline comments for complex logic

4. **Commit Messages**
   - Use descriptive commit messages
   - Reference issues if applicable
   - Include Co-Authored-By for AI assistance
