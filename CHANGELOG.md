# Changelog

All notable changes to Talkat will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-01-02

### Added
- Initial release prepared for AUR (Arch User Repository)
- Core voice-to-text dictation functionality
- Support for Faster-Whisper and Vosk speech recognition models
- Listen mode for single utterances with toggle support
- Long dictation mode for continuous recording
- Background process management with proper PID tracking
- Microphone calibration for automatic silence threshold detection
- Audio file transcription support (.wav, .mp3, .m4a, .flac, .ogg)
- Model server with HTTP API for transcription
- Configurable settings via JSON config file
- Desktop integration with .desktop file
- Systemd service support for auto-start
- Comprehensive logging framework
- Process management with proper locking and signal handling
- Support for both Wayland (ydotool) and X11 (xdotool) environments
- Clipboard integration (wl-copy/xclip)
- Desktop notifications via libnotify

### Changed
- Replaced print statements with proper logging framework
- Improved PID file management with proper locking mechanism
- Fixed ALSA warning suppression to be more specific
- Made server URL and timeout values configurable
- Relaxed Python version constraints for better compatibility

### Fixed
- Fixed race conditions in PID file management
- Improved signal handling for graceful shutdown
- Better resource cleanup for audio streams
- Fixed overflow handling in audio recording

### Removed
- Removed duplicate and dead code files
- Removed unnecessary development test scripts

## [Unreleased]

### Planned
- Comprehensive test suite
- Man pages for all commands
- Full FHS compliance for file locations
- Enhanced input validation and security
- Code quality tools integration (linting, formatting)
- Type hints throughout codebase
- WebSocket support for real-time streaming
- Multiple language support
- Custom wake word detection
- GUI configuration tool