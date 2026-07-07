#!/bin/bash
# Run talkat from this checkout, isolated from the installed daily-driver.
#
# TALKAT_RUNTIME_DIR moves the unix socket, PID files, and locks into a
# separate talkat-dev/ runtime dir, so a dev server/client pair never
# collides with the packaged service your desktop hotkeys use. Everything
# else (config, models, transcripts) is shared with the regular install.
#
#   ./dev.sh server     # dev model server on its own socket
#   ./dev.sh listen     # dev client -> dev server
#   ./dev.sh doctor     # environment report as the dev build sees it
#
# To point the dev client at the *installed* server instead, run
# `uv run talkat ...` directly (no isolation).

set -e
cd "$(dirname "$0")"

export TALKAT_RUNTIME_DIR="${TALKAT_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/talkat-dev}"

exec uv run talkat "$@"
