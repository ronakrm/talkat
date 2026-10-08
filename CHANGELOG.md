# Changelog

All notable changes to Talkat will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] - 2026-10-08

Typing is about five times faster, with the pace now configurable, and
`config.json` is harder to break: one bad value no longer costs you the
whole file, and `talkat calibrate` / `talkat model use` can no longer wipe
it. Nothing to migrate, but restart the service after upgrading
(`systemctl --user restart talkat`) and run `talkat doctor`: it now lists
any setting in your config that talkat ignores, and why. A few values 2.0
let through, like quoted numbers and relative paths, are refused now.

### Added
- **`typing_key_hold_ms`** (default 5, 0–100) and **`typing_key_delay_ms`**
  (default 0, 0–100) set the typing pace. If an app drops or doubles
  characters, raise the hold (2.0 held each key 20 ms) or add a few ms of
  delay between keys.
- **`talkat doctor`** lists the config files it read and every setting in
  them that's ignored, with the reason and the value used instead.
- `CONTRIBUTING.md` for working on talkat: isolated runs with `./dev.sh`,
  the checks CI runs, and the live AIPP tests.

### Changed
- **Typing ~5× faster** — ~32 ms → ~7 ms per character. Each keystroke is
  held 5 ms instead of ydotool's default 20, and talkat no longer polls for
  each ydotool call to finish. Still one keystroke per call, so the focus
  and modifier guards work exactly as before.
- **Each setting is validated on its own.** An invalid value falls back to
  the `/etc/talkat/config.json` value or the built-in default, and the rest
  of the file still applies; only a file that can't be parsed as a JSON
  object is skipped whole. Unknown keys are reported, with a did-you-mean
  for typos, and removed keys name their replacement.
- **Stricter values**: numbers must be JSON numbers (`"60"` and `true` are
  refused), whole-number settings reject fractions, `max_upload_size_mb` is
  limited to 1–2048, and path settings must be absolute or start with `~`
  (a relative path used to resolve against each process's working
  directory).
- **`talkat calibrate` and `talkat model use`** change only their own key
  in `~/.config/talkat/config.json`, and refuse to rewrite a file they
  can't parse — calibrate then prints the threshold to set by hand.
- README rewritten around installing and using talkat, with a reference
  for every setting.

### Fixed
- A path setting that was a symlink or contained `..` (say, a model cache
  symlinked onto a bigger disk) crashed every command. Both are accepted
  now.
- `talkat calibrate` and `talkat model use` rewrote the whole user config:
  they copied `/etc` values into it and, if the file had been rejected as
  invalid, dropped everything else in it.

### Removed
- Dead config keys `device`, `model_cache_dir` and `pre_speech_padding`;
  nothing read them. A file that still sets one gets a warning, naming the
  replacement where there is one.

## [2.0.0] - 2026-10-08

Dictation is now **one route with one hotkey**: `talkat listen` toggles it on
and off. The microphone stays open for the whole recording, speech is cut at
natural pauses and transcribed in the background, and text is typed as you
talk. A half-hour of notes is just a long recording — `long`, `start-long`,
`stop-long` and `toggle-long` are gone, and `listen --to-file` covers what
long mode was for. Also fixes recordings cut off at 30 s and typed text
firing compositor shortcuts.

### Migration from 1.x
- **Hotkeys** — one binding does it all, and it needs `repeat=false` or
  holding the key re-triggers it:
  ```kdl
  Mod+Apostrophe repeat=false { spawn "talkat" "listen"; }              # niri
  Mod+Shift+Apostrophe repeat=false { spawn "talkat" "listen" "--to-file"; }
  ```
  ```sh
  bindsym $mod+apostrophe exec talkat listen                            # sway
  ```
  A `talkat long` binding becomes `talkat listen --to-file`; a
  `toggle-long` binding can simply go.
- **Scripts** — `talkat long` → `talkat listen --to-file`;
  `start-long` / `stop-long` / `toggle-long` → `talkat listen` (it
  toggles). `--silence-duration`, `--no-clipboard` and `--background` are
  gone.
- **Config** — nothing to do: keys this release doesn't know are dropped
  from `~/.config/talkat/config.json` with a log line. If you had tuned
  `long_mode_silence_timeout` or `long_mode_max_session_duration`, use
  `idle_timeout` and `max_recording_duration` instead. Re-run
  `talkat calibrate` if dictation pauses aren't being found — the
  threshold's only job now is telling the segmenter where to cut.
- **Mixed versions work** either way: a 2.0 client against a 1.x server
  loses only the cross-segment context (the server ignores the new `prompt`
  field), and a 1.x client against a 2.0 server is unchanged. `talkat
  doctor` reports the skew. The AUR package ships both, so restart the
  service after upgrading: `systemctl --user restart talkat`.

### Removed
- **`talkat long`, `start-long`, `stop-long`, `toggle-long`** — breaking.
  `talkat listen` is the whole interface; migrate hotkeys:
  `talkat long` → `talkat listen --to-file`, and a `toggle-long` binding can
  simply go (one `talkat listen` binding already toggles). Bind it with
  `repeat=false` so holding the key can't re-trigger it.
- Config keys `silence_duration`, `clipboard_on_long`,
  `long_mode_silence_timeout`, `long_mode_max_session_duration` and
  `long_mode_max_consecutive_errors`, and the `--silence-duration`,
  `--no-clipboard` and `--background` flags. Replaced by `idle_timeout`,
  `idle_notify_interval` and `max_consecutive_errors`. Keys a release no
  longer knows are dropped from the user's config with a log line, so a
  stale config file is harmless.
- `AudioSession`'s silence auto-stop and level tracking: capture decides
  nothing about speech any more (the segmenter decides where to cut, the
  server's VAD what to trim). `ProcessManager.toggle` and
  `start_background_process` went with the background long mode.

### Fixed
- **`talkat listen` cut dictation off at 30 s.** The `max_recording_duration`
  default ended the recording silently, mid-sentence: everything said after
  it was lost, and the transcript started typing while the user was still
  talking. The default is now 600 s — a safety net for a mic left open, not
  a usage limit — and a recording that ends on its own says so at once.
- **Typed text triggered compositor shortcuts.** ydotoold's virtual keyboard
  shares the seat's modifier state, so holding Super while a transcript was
  being typed turned its letters into shortcuts (on niri: Space opened the
  launcher, T a terminal, H/L moved focus). Typing now sends one keystroke
  per `ydotool` call and checks the physical keyboard (evdev `EVIOCGKEY`)
  before each, pausing while any modifier is held.
- **Long recordings lost speech between utterances.** The old long mode
  stopped reading the stream while each utterance was transcribed, then
  closed and reopened the microphone, and hard-cut utterances at 60 s —
  often mid-word.
- **A pause to think ended a recording.** The 3 s post-speech stop is gone:
  silence no longer stops anything, and a quiet session announces itself
  instead of looking dead.
- `talkat listen --max-recording` was ignored: capture re-read the config
  files instead of using the merged config.
- Stop signals could SIGKILL ydotool mid-keystroke (leaving the key held
  down) and lose the transcript: the second signal raised
  `KeyboardInterrupt` inside `subprocess.run`. Handlers no longer raise.

### Changed
- **Dictation sessions** (`session.py`, `segmenter.py`): one open microphone
  per recording; the stream is cut at the first ≥0.4 s pause after 3 s of
  audio (or at the quietest moment before 30 s of continuous speech) and
  each segment is transcribed in order while recording continues, with the
  previous segment's text sent as context. On a 3-minute read-speech sample
  the segmented transcript matched whole-file transcription to 0.2% of
  words. Segments always concatenate back to the captured audio.
- **Text is typed as you talk**: each segment is typed as soon as it's
  transcribed, about 1–2 s after you finish a sentence. `--to-file`,
  `--postprocess`, `-o` and `output_mode: clipboard` still deliver once, at
  the end.
- **How a recording ends**: you toggle it off; or `idle_timeout` (60 s with
  no transcribed speech) stops it, having said so every
  `idle_notify_interval` (30 s) while it was quiet; or
  `max_recording_duration` (10 min) is reached; or
  `max_consecutive_errors` (5) segments in a row can't be transcribed.
- **Untranscribable audio is never lost**: a segment is retried (0.5/2/5 s
  backoff; one attempt once the server is known to be failing), then saved
  as a WAV under `~/.local/share/talkat/untranscribed/`, with an
  `[untranscribed audio: talkat file <path>]` marker in the transcript.
  Typing stops at the gap and the rest goes to the clipboard.
- Whatever can't be typed — focus moved (now re-checked at every word), a
  modifier stayed held for 5 s, typing stopped or failed — goes to the
  clipboard, and the notification says so.
- Stop signals: the first ends the recording (everything recorded is still
  transcribed and delivered); the second aborts — typing stops between
  keystrokes, no new requests start, untranscribed audio is saved — within
  `stop_process`'s one-second SIGKILL window.
- `/transcribe_stream` accepts an optional `prompt` (preceding transcript)
  in its metadata, placed after the dictionary words in Whisper's
  `initial_prompt`. Older servers ignore the field.
- `process_stop_timeout` default 20 s → 300 s, covering a max-length
  recording delivered at once (`--postprocess`); the stop wait still returns
  the moment the process exits.
- `ydotool type` is called with `--escape=0` (a lone `\` is typed, not
  swallowed), and non-ASCII characters are never passed to it (ydotool
  1.0.x reads outside its keymap for them; they were dropped before too).
- `TranscriptionClient` moved to `talkat.client` and now transcribes a
  buffer (`transcribe_audio`) rather than recording one utterance itself.
- `talkat calibrate` still sets `silence_threshold`, but it now only tells
  the segmenter where your pauses are.
- Diagnostics records describe the session: segments, untranscribed
  segments, cut reasons, stop reason, idle stop, session seconds.

### Added
- `talkat listen --to-file`: appends each piece to the transcript file as
  it's recognized and copies the whole transcript to the clipboard at the
  end, typing nothing. `-o PATH` chooses the file.
- `talkat doctor` reports whether the modifier guard is active (it needs
  read access to `/dev/input`, i.e. the `input` group).

## [1.1.1] - 2026-07-12

Hotfix: toggle-stop was losing the transcript — present since v1.0.0, but
masked on the dev machine by the stale-install shadowing that 1.1.0's
`talkat doctor` was built to catch.

### Fixed
- **Toggle-stop lost the transcript.** The signal handler raised
  `KeyboardInterrupt` on the *first* SIGINT, tearing down the in-flight
  streaming transcription ("Recording interrupted.") before the graceful
  stop-event path could finish it. Now the first signal ends the capture
  loop cleanly (within one ~32 ms chunk), the request completes, and the
  text is delivered; a second signal — pressing stop again, or
  `stop_process`'s SIGTERM escalation — force-aborts a blocked wait (the
  hung-server escape hatch the raise was originally added for).
  Regression-tested with real signals against a real UDS server
  (`tests/test_toggle_signal.py`).
- `process_stop_timeout` default raised 5 s → 20 s: after the stop signal a
  listen process legitimately spends time on ASR (~¼ of the audio length)
  plus typing, and hitting the old ceiling escalated to SIGTERM — which now
  means force-abort. The stop poll still returns the instant the process
  exits.
- Test hygiene: `test_listen.py` replaced `talkat.main.TranscriptionClient`
  with a stub and never restored it, poisoning any later test file that
  needed the real client.

## [1.1.0] - 2026-07-06

Stability release: fixes the cut-off-utterance-beginnings class of bugs, the
PipeWire device-index race, and a transcript-loss bug on punctuation-heavy
text; adds a focus guard, `talkat doctor`, and a clean dev-vs-installed
separation story.

### Added
- **`talkat doctor`** — environment self-check: talkat version + install
  origin, duplicate `talkat` binaries on PATH (shadowing), systemd unit
  shadowing (user unit vs packaged unit), server health + client/server
  version skew, socket, audio devices, ydotoold/clipboard/notification
  tooling, focus-guard status, active config layers.
- **Focus guard**: `talkat listen` captures the focused window (niri /
  Hyprland / sway IPC) when recording starts and refuses to type if focus
  changed by transcription end — the transcript goes to the clipboard with a
  notification instead of splattering into the wrong window. Config
  `focus_guard` (default `true`).
- `output_mode` config: `"type"` (default) or `"clipboard"` (never type).
- `input_device_name` config: pin the capture device by case-insensitive
  name substring instead of using the system default.
- **Dev isolation**: `TALKAT_RUNTIME_DIR` env override relocates the server
  socket, PID files, and locks together; `./dev.sh` wraps `uv run talkat`
  with a `talkat-dev/` runtime dir so a checkout under test can never
  toggle, stop, or out-bind the installed daily-driver service.
- `/health` now reports the server package version; the client and `doctor`
  use it to flag stale servers after upgrades.
- Long-form segmentation: audio longer than `max_segment_seconds` (default
  8 min) is split at energy minima before ASR, bounding peak memory and
  avoiding Whisper long-form position-embedding failures (the "500 errors
  on long recordings" known bug). Applies to both backends.
- Server-side RMS gain normalization for quiet microphones, config-gated via
  `audio_normalize_gain` / `audio_target_rms_dbfs` / `audio_max_gain_db`.
  Boost-only, capped, peak-protected; applied gain is reported per request.
- Per-run diagnostics JSON at `~/.local/share/talkat/diagnostics/`
  (`diagnostics.latest.json` + timestamped copies, capped at 200 records)
  with duration, realtime factor, applied gain, model, and errors.

### Changed
- **Audio streams from the moment the mic opens.** The calibrated threshold
  no longer gates what is *sent* — it only decides when the utterance is
  over. Combined with the server-side VAD filter this eliminates clipped
  utterance beginnings. The now-obsolete `pre_speech_padding` config key is
  ignored (dropped from saved configs automatically).
- The "Recording…" notification fires only once the microphone stream is
  actually open — it is now safe to start speaking as soon as the toast
  appears. Previously it fired ~0.5 s early, during device setup.
- Microphone resolution and stream-open now happen on a single PyAudio
  instance (PortAudio topology snapshot), with one retry on a fresh
  instance — fixes `[Errno -9998] Invalid number of channels` caused by
  PipeWire device-index churn between enumeration and open.
- `save_app_config` persists only values that differ from the code defaults
  and drops keys unknown to the current version. Previously `talkat
  calibrate` froze every default into `config.json`, permanently opting the
  user out of future default improvements.
- `talkat listen` no longer imports the file-processing stack at startup
  (snappier hotkey response); device listing moved to DEBUG; httpx request
  logging silenced.
- `setup.sh` warns (and asks) before shadowing a system-packaged install,
  and points to `./dev.sh` for development.
- Repo `talkat.service` (installed by the Arch package) repaired: broken
  `%i` ExecStart → `/usr/bin/talkat server`, placeholder documentation URL
  fixed, hardening synced with the `install-service` unit.
- `calibrate_microphone` uses the same fixed 16 kHz/mono parameters as
  recording (the undocumented `audio_chunk_size`/`audio_channels`/
  `audio_sample_rate` config reads are gone).

### Fixed
- **Transcript loss on punctuation**: `validate_command` rejected any argv
  element containing `$ ( ) { } < > | & ;` — including the transcript being
  passed to `ydotool type` / `notify-send`. Dictating "$20" or "(roughly)"
  crashed the typing path uncaught. Arguments are inert data under
  `shell=False`; only the executable name is checked now.
- **Typing failures no longer lose the transcript**: any failure in the
  typing path (ydotoold not running, ydotool missing, crash mid-type,
  focus-guard divert) falls back to the clipboard, then stdout — with a
  notification saying where the text went.
- `notify-send` failures can no longer crash dictation.
- Misleading "Speech detected. Streaming audio to model server..." log line
  that printed before recording had even started.
- Unbounded growth of the diagnostics directory (now pruned to the newest
  200 records).

## [1.0.0] - 2026-06-15

First stable release. The CLI, on-disk layout, and wire protocol are now
considered stable surface; future breaking changes will go through a
deprecation cycle.

### Added
- AI post-processing (AIPP): pipe transcripts through any OpenAI-compatible
  endpoint (Ollama, llama.cpp server, LM Studio, vLLM, OpenAI, etc.) before
  they hit `ydotool`. Opt-in via `--postprocess <profile>`. Named profiles in
  config; API keys referenced by env-var name only. Fail-open contract — a
  broken backend never loses the user's transcript.
- `--language` CLI flag on `listen`, `long`, `file`, `batch`, and a `language`
  config key. Threaded through the wire protocol so per-request overrides
  beat the server-side default.
- `talkat model {list, download, use}` subcommand for faster-whisper model
  management. Resolves friendly names (`small.en`, `large-v3`) to HuggingFace
  repos and downloads idempotently.
- `--max-recording`, `--silence-duration`, `--http-timeout` CLI overrides on
  `listen` and `long`.
- `--try-lock` on `listen`, `start-long`, `stop-long`, `toggle-long` to
  fail-fast instead of waiting on the process lock.
- Layered config merge: `CODE_DEFAULTS` → `/etc/talkat/config.json` →
  `~/.config/talkat/config.json` → CLI overrides. Each layer partially
  overrides the previous instead of wholesale-shadowing.
- `TranscriptionBackend` Protocol in `backends.py` — adding a new ASR engine
  is now one Protocol implementation + one factory registration.
- CI workflows: `.github/workflows/ci.yml` (pytest on Python 3.11–3.14, ruff
  format + lint, mypy, Codecov upload, opt-in live AIPP tests against Ollama)
  and `.github/workflows/aur-build.yml` (inline PKGBUILD against PR HEAD,
  makepkg in an archlinux:base-devel container, install + smoke-test, namcap
  info-only).
- Comprehensive test suite: ~370 tests covering config, security, process
  manager, VAD, long mode, file processor, CLI dispatch, language plumbing,
  AIPP, model manager, model server, and integration paths over real
  Flask + waitress on UDS.
- `py.typed` marker — downstream type checkers now see Talkat as typed.

### Changed
- Server warm-up: a dummy inference runs at the end of `initialize()` so the
  first real request hits a hot model.
- Long-mode transcript memory is now bounded — utterances are streamed to
  disk and the final clipboard copy reads the file back, instead of
  accumulating in a list.
- Long-mode circuit breaker: aborts with a notification after
  `long_mode_max_consecutive_errors` (default 5) consecutive server errors.
- ALSA / JackD / PortAudio init noise is now silenced via an fd-level
  `os.dup2` context manager instead of the ctypes-libasound hack.
- PID file management refactored around `flock` — `ProcessManager.locked()`
  is the lock primitive; PID writes are atomic and the child is killed if
  the write fails.
- Signal handler now both sets the stop event and raises `KeyboardInterrupt`
  so blocking httpx calls unblock immediately on SIGINT/SIGTERM.
- Server `MAX_CONTENT_LENGTH` enforced (default 100 MB); 413 returns a JSON
  body; client stat-checks file size before uploading.
- Systemd user-service hardening applied to both `talkat.service` and the
  `talkat install-service` path: `ProtectHome=read-only`, `PrivateTmp`,
  `ProtectSystem=strict`, etc.
- `stop_process` SIGINT grace window now uses `time.monotonic()` instead of
  `time.time()` so NTP slew can't break the 5s window.
- Dependency upper bounds added in `pyproject.toml` so packaged builds
  don't silently pick up breaking majors.
- Python 3.14 supported across the dependency tree (proactive cp314 sweep).
- mypy-strict (`disallow_untyped_defs = true`) across the codebase.

### Fixed
- Symlink check in `validate_file_path` — `.resolve()` was running before
  `.is_symlink()`, silently following symlinks. Reordered so the symlink
  block actually fires.
- Clipboard fallback (wl-copy → xclip) deduped — `main.copy_to_clipboard`
  and the file-processor copy path were duplicated. Both now route through
  `clipboard.py`.
- `VERSION` file removed — `pyproject.toml` is now the sole source of truth.

### Removed
- Stale `VERSION` file (drifted from `pyproject.toml`).
- Old PKGBUILD moved out of this source tree; lives in the AUR git repo per
  Arch convention.

## [0.2.0] - 2026-05-19

### Added
- `talkat install-service` / `uninstall-service` subcommands — manage the
  user systemd unit without hand-editing files.
- Long-mode auto-stop on extended silence or max session duration.

### Changed
- Server moved from TCP 5555 to a Unix domain socket at
  `$XDG_RUNTIME_DIR/talkat/server.sock` (perms 0600), served by waitress.
- Install path is now `uv tool install` into an isolated venv under
  `~/.local/share/uv/tools/talkat/`, not a system-wide install.
- Long-mode notifications collapsed to one start + one stop instead of one
  per utterance.
- `listen` and `long` give in-flight transcription time to finish before
  exiting on stop.

### Removed
- Unused `torch`, `transformers`, `accelerate` deps — faster-whisper
  (CTranslate2) doesn't need them.

## [0.1.0] - 2025-10-21

Initial AUR package release.

### Added
- Voice-to-text dictation for Wayland Linux compositors.
- Faster-Whisper (default) and Vosk speech recognition backends.
- Listen mode (single utterance with toggle support) and long mode
  (continuous).
- Background long mode (`start-long` / `stop-long` / `toggle-long`).
- Microphone calibration for automatic silence threshold detection.
- Audio file transcription (`.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`) +
  batch processing.
- Custom dictionary support (vocabulary hints for faster-whisper).
- Configurable settings via JSON config at `~/.config/talkat/config.json`.
- Comprehensive input validation and security hardening.
- FHS / XDG-compliant file layout.
- Logging framework replacing scattered `print` calls.
- Desktop integration: `.desktop` file, systemd user service, libnotify
  notifications.
- Clipboard integration via `wl-copy` (Wayland) with `xclip` fallback.
- AUR packaging with uv-based dependency management.
