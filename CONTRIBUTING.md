# Contributing to Talkat

Issues and pull requests are welcome. [CLAUDE.md](CLAUDE.md) documents the
architecture, conventions and design invariants. Read it before you change
capture, segmentation, typing or signal handling: those areas have rules
that the tests pin down.

## Set up

```bash
git clone https://github.com/ronakrm/talkat.git
cd talkat
uv sync
```

## Run your checkout without touching your install

```bash
./dev.sh server      # model server on an isolated socket (stays in the foreground)
./dev.sh listen      # in another terminal; run it again to stop
./dev.sh calibrate
./dev.sh doctor      # environment report as the dev build sees it
```

`dev.sh` is `uv run talkat` with `TALKAT_RUNTIME_DIR` pointed at
`$XDG_RUNTIME_DIR/talkat-dev/`. That moves the unix socket, PID files and
locks, so a dev server and client never collide with the installed service,
and your desktop hotkeys keep working through the installed copy while you
test. Config, models and transcripts are shared with the installed copy. To
point a dev client at the *installed* server instead, run
`uv run talkat listen` directly.

Don't run `setup.sh` on a machine that has the AUR package. The uv-tool copy
it installs shadows `/usr/bin/talkat` on PATH and the packaged unit in
systemd, then quietly goes stale. `talkat doctor` detects this.

## Checks

CI runs these on every push, with the tests on Python 3.11–3.14:

```bash
uv run pytest                     # no microphone or model needed
uv run mypy src/                  # strict typing
uvx ruff@0.1.14 format --check .  # ruff is pinned to the version CI uses
uvx ruff@0.1.14 check .
```

The suite can't exercise real audio hardware. The Testing Checklist in
CLAUDE.md lists what to verify by hand before a release.

### AI post-processing against a live backend

The mocked tests cover validation and fail-open behavior. For a final smoke
test against a real OpenAI-compatible server (Ollama, llama.cpp, LM Studio,
OpenRouter, …), opt in with `--aipp-live`:

```bash
# One-time setup. Any OpenAI-compatible server works; Ollama is the easiest.
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:0.5b      # ~400 MB, fast on CPU

# Run only the live tests
uv run pytest --aipp-live -k aipp_live -v
```

The `aipp_live`-marked tests are skipped by default, and skip with a
message if the backend isn't reachable. Point them at a different server
with the `OLLAMA_BASE_URL` and `OLLAMA_MODEL` environment variables. CI runs
them on every push against a fresh Ollama install (the `aipp-live` job in
`.github/workflows/ci.yml`).

## Releases

A release bumps `version` in `pyproject.toml`, stamps `CHANGELOG.md`, tags
`vX.Y.Z`, and then bumps the AUR package. CLAUDE.md has the full sequence
(Installation → Packaged install). The PKGBUILD lives in the AUR git repo
(`ssh://aur@aur.archlinux.org/talkat.git`), not in this tree, as is
standard for Arch packaging.

## Packaging: help wanted

Talkat ships through the AUR and through `setup.sh` on any distribution with
`uv`. Native `.deb` (Debian, Ubuntu) and `.rpm` (Fedora, openSUSE) packages
would be very welcome. If you'd like to help, open an issue tagged
[`packaging`](https://github.com/ronakrm/talkat/issues?q=is%3Aissue+label%3Apackaging).
The AUR PKGBUILD's approach of bundling a venv built with uv should carry
over reasonably well. The open questions are:

- the runtime model: system Python with distro-packaged dependencies, or a
  vendored venv
- hosting: GitHub releases, a PPA, Copr or OBS
- signing
