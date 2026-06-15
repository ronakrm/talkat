# Changelog

All notable changes to Talkat will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
