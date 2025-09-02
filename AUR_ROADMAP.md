# AUR Release Roadmap for Talkat

## Overview
This document outlines the comprehensive plan to prepare Talkat for release on the Arch User Repository (AUR). The work is organized into phases, with Phase 1 being critical for initial release and subsequent phases improving quality and maintainability.

## Phase 1: Critical AUR Blockers (Must Have)
These items must be completed before initial AUR submission.

### 1.1 Clean Up Codebase
- [ ] Remove dead code files:
  - `src/talkat/record_improved.py` (duplicate calibration)
  - `src/talkat/model_server_v2.py` (unused enhanced server)
  - `src/talkat/long_dictation.py` (unused enhanced dictation)
  - `test_volume.py` (development test script)
  - `uinput.sh` (unused script)
- [ ] Consolidate duplicate functionality
- [ ] Remove debug print statements

### 1.2 Implement Proper Logging
- [ ] Add Python logging configuration
- [ ] Replace all print() statements with logger calls
- [ ] Add log levels (DEBUG, INFO, WARNING, ERROR)
- [ ] Configure log output location
- [ ] Add --verbose and --quiet CLI flags

### 1.3 Fix Critical Technical Debt
- [ ] Replace PID file management with proper process management
- [ ] Fix signal handling for graceful shutdown
- [ ] Handle ALSA warnings properly instead of suppressing all warnings
- [ ] Fix audio stream resource cleanup
- [ ] Remove hardcoded URLs and timeouts

### 1.4 Create AUR Package Files
- [ ] Create PKGBUILD with proper dependencies
- [ ] Generate .SRCINFO
- [ ] Create talkat.desktop file for application menu
- [ ] Create systemd service template
- [ ] Add LICENSE file (if missing)
- [ ] Create install script for AUR

### 1.5 Write Essential Documentation
- [ ] Create man pages for all commands:
  - talkat(1) - main command
  - talkat-server(1)
  - talkat-listen(1)
  - talkat-long(1)
  - talkat-calibrate(1)
- [ ] Create CHANGELOG.md
- [ ] Update README for AUR installation

### 1.6 Fix Dependency Management
- [ ] Remove strict Python version constraint (allow 3.12+)
- [ ] Document optional dependencies
- [ ] Simplify PyTorch CPU installation for AUR
- [ ] Test with latest versions of all dependencies

### 1.7 Ensure FHS Compliance
- [ ] Move installation from /opt to /usr/share
- [ ] Use proper XDG directories for config/cache/data
- [ ] Fix service file paths
- [ ] Update setup.sh for proper locations

## Phase 2: Security & Stability (Should Have)
Important for production use but not blocking initial release.

### 2.1 Input Validation
- [ ] Validate audio format and sample rate
- [ ] Sanitize file paths
- [ ] Add request size limits
- [ ] Validate configuration values

### 2.2 Error Handling Improvements
- [ ] Replace generic exception handlers with specific ones
- [ ] Add proper error messages with context
- [ ] Implement retry logic for transient failures
- [ ] Add graceful degradation

### 2.3 Resource Management
- [ ] Use context managers for all resources
- [ ] Ensure proper cleanup in all error paths
- [ ] Add memory usage monitoring
- [ ] Implement connection pooling

### 2.4 Type Safety
- [ ] Add comprehensive type hints to all functions
- [ ] Configure mypy for type checking
- [ ] Add type stubs for external libraries
- [ ] Document type expectations

## Phase 3: Quality & Testing (Nice to Have)
Improves maintainability and developer experience.

### 3.1 Test Suite
- [ ] Create unit tests for core functionality
- [ ] Add integration tests for server-client communication
- [ ] Create end-to-end tests
- [ ] Add test fixtures and mock data
- [ ] Achieve >80% code coverage

### 3.2 Code Quality Tools
- [ ] Configure black for code formatting
- [ ] Set up pylint/flake8 for linting
- [ ] Add pre-commit hooks
- [ ] Configure mypy for type checking
- [ ] Add isort for import sorting

### 3.3 CI/CD Pipeline
- [ ] Set up GitHub Actions
- [ ] Add automated testing on PR
- [ ] Add code quality checks
- [ ] Add security scanning
- [ ] Automate AUR package updates

### 3.4 Architecture Improvements
- [ ] Create service abstraction layer
- [ ] Refactor monolithic functions
- [ ] Implement dependency injection
- [ ] Add plugin architecture for models
- [ ] Create proper interfaces

## Phase 4: Enhanced Features (Future)
Post-release improvements.

### 4.1 Performance
- [ ] Add performance profiling
- [ ] Implement caching strategies
- [ ] Optimize model loading
- [ ] Add metrics collection

### 4.2 User Experience
- [ ] Create GUI configuration tool
- [ ] Add internationalization
- [ ] Improve error messages
- [ ] Add progress indicators

### 4.3 Advanced Features
- [ ] WebSocket support for streaming
- [ ] Multiple language support
- [ ] Custom wake words
- [ ] Speaker diarization

## Implementation Order

### Week 1: Foundation
1. Clean up dead code (1.1)
2. Implement logging (1.2)
3. Fix critical hacks (1.3)

### Week 2: Packaging
1. Create PKGBUILD (1.4)
2. Fix dependencies (1.6)
3. Ensure FHS compliance (1.7)

### Week 3: Documentation & Polish
1. Write man pages (1.5)
2. Add input validation (2.1)
3. Improve error handling (2.2)

### Week 4: Testing & Release
1. Create basic test suite (3.1)
2. Set up code quality tools (3.2)
3. Final testing and AUR submission

## Success Criteria

### Minimum Viable Release
- [ ] Installs cleanly from AUR
- [ ] All commands work without errors
- [ ] No hardcoded paths or values
- [ ] Proper logging instead of prints
- [ ] Man pages available
- [ ] Service files work correctly

### Production Ready
- [ ] >80% test coverage
- [ ] No critical security issues
- [ ] Proper error handling throughout
- [ ] Type hints on all public APIs
- [ ] CI/CD pipeline running
- [ ] Documentation complete

## Risk Mitigation

### High Risk Items
1. **PID file race conditions** - Could cause data loss or hung processes
   - Mitigation: Implement proper locking or use systemd for process management

2. **Audio resource leaks** - Could exhaust system resources
   - Mitigation: Strict resource management with context managers

3. **Security vulnerabilities** - Could expose system to attacks
   - Mitigation: Input validation, sanitization, principle of least privilege

### Medium Risk Items
1. **Dependency conflicts** - AUR package might not install
   - Mitigation: Test on clean Arch systems, use virtual environments

2. **Performance issues** - Might be too slow for practical use
   - Mitigation: Profile and optimize critical paths

3. **Compatibility issues** - Might not work on all Wayland compositors
   - Mitigation: Test on major compositors (Sway, Hyprland, GNOME, KDE)

## Notes

- Priority is on stability and correctness over features
- Keep backward compatibility where possible
- Document all breaking changes in CHANGELOG
- Test each phase thoroughly before moving to next
- Get community feedback early and often

## Tracking

Progress on this roadmap is tracked in:
- Todo list (in-app)
- Git commits on `prepare-for-aur` branch
- GitHub issues (when created)
- This document (check off items as completed)