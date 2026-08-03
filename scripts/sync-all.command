#!/usr/bin/env bash
# Share skills, MCP servers and chats across accounts.
# Double-click me in Finder, or run me from a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
./bin/claude-login sync-all
printf '\n— done. Press Enter to close this window. '
read -r _
