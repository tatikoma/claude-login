#!/usr/bin/env bash
# First-time setup: install checks, accounts, sharing.
# Double-click me in Finder, or run me from a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
./bin/claude-login setup
printf '\n— done. Press Enter to close this window. '
read -r _
