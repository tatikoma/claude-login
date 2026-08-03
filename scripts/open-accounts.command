#!/usr/bin/env bash
# Open the Claude app under every chosen account.
# Double-click me in Finder, or run me from a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
./bin/claude-login app open
printf '\n— done. Press Enter to close this window. '
read -r _
