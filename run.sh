#!/usr/bin/env bash
# Comfortable wrapper: builds the image with host UID/GID and runs the TUI.
# Any extra args are forwarded as the container command, e.g.:
#   ./run.sh                                # interactive TUI
#   ./run.sh python tui.py --download-all
#   ./run.sh python tui.py --bake-credentials
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p audiobooks downloads
mkdir -p "${HOME}/.config/jnoveldl"

# Bash exposes $UID but doesn't export it (and it's readonly, so we can't
# reassign). Mark it exported and set GID from `id`.
export UID
export GID="$(id -g)"

exec docker compose run --rm --build jnoveldl "$@"
