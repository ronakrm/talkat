#!/bin/bash
# Install talkat from this git checkout, then enable the user systemd service.
#
# This installs talkat as an isolated uv tool (so it doesn't interfere with
# your project python environments) and writes a user systemd unit that
# starts the model server on login.
#
# Arch users: prefer the AUR package (`yay -S talkat`) — it ships a
# system-wide user-systemd unit at /usr/lib/systemd/user/talkat.service.

set -e

if [ "$EUID" -eq 0 ]; then
    echo "Don't run this script as root — talkat is a per-user dictation tool."
    echo "Run it as your normal user; it installs into your home directory."
    exit 1
fi

if ! command -v uv &> /dev/null; then
    echo "uv not found. Install it first:"
    echo "  https://docs.astral.sh/uv/getting-started/installation/"
    echo "  or:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# A system-packaged talkat (e.g. the AUR package) and a uv-tool install don't
# mix well: ~/.local/bin shadows /usr/bin, and the user systemd unit shadows
# the packaged one — the result is a stale dev copy silently becoming the
# daily driver. If a packaged install exists, this script is almost never
# what you want; ./dev.sh runs the checkout without touching your install.
if [ -e /usr/lib/systemd/user/talkat.service ] || [ -x /usr/bin/talkat ]; then
    echo "WARNING: a system-packaged talkat is already installed (/usr/bin/talkat)."
    echo "Proceeding will SHADOW it with a per-user copy of this checkout, on PATH"
    echo "and in systemd. For development, use ./dev.sh instead — it runs the"
    echo "checkout against an isolated socket and leaves your install alone."
    read -r -p "Shadow the packaged install anyway? [y/N] " reply
    case "$reply" in
        [yY]*) ;;
        *) echo "Aborted. Try: ./dev.sh server"; exit 1 ;;
    esac
fi

echo "Installing talkat with uv tool install..."
uv tool install --reinstall .

# Find the binary uv just installed so we don't depend on the caller's PATH.
TALKAT_BIN="$(uv tool dir)/talkat/bin/talkat"
if [ ! -x "$TALKAT_BIN" ]; then
    # Fallbacks for older uv layouts.
    TALKAT_BIN="$(command -v talkat || true)"
fi
if [ -z "$TALKAT_BIN" ] || [ ! -x "$TALKAT_BIN" ]; then
    echo "Could not locate the installed talkat binary. Check 'uv tool list'."
    exit 1
fi

case ":$PATH:" in
    *:"$HOME/.local/bin":*) ;;
    *)
        echo ""
        echo "Note: ~/.local/bin is not on your PATH. Add it via:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "Installing systemd user service..."
"$TALKAT_BIN" install-service

echo ""
echo "Done. Useful commands:"
echo "  talkat listen                       # toggle short dictation"
echo "  talkat calibrate                    # set the silence threshold"
echo "  systemctl --user status talkat      # check service"
echo "  systemctl --user restart talkat     # restart after upgrades"
echo "  talkat uninstall-service            # remove the service"
echo "  uv tool uninstall talkat            # remove the CLI"
