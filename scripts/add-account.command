#!/usr/bin/env bash
# Sign a new account in and open a clean Claude window for it.
# Double-click me in Finder, or run me from a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
./bin/claude-login add --use
printf '\n— done. Press Enter to close this window. '
read -r _
