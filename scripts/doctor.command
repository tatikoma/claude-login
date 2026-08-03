#!/usr/bin/env bash
# Show what is wired up and what is not.
# Double-click me in Finder, or run me from a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
./bin/claude-login doctor
printf '\n— done. Press Enter to close this window. '
read -r _
