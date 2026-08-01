#!/usr/bin/env bash
# Put `claude-login` (and the short alias `ccl`) on your PATH.
#
#   ./install.sh              symlink into ~/.local/bin  (no copy, edits are live)
#   ./install.sh --pipx       install as an isolated pipx app
#   ./install.sh --uninstall  remove the symlinks
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CLAUDE_LOGIN_BIN_DIR:-$HOME/.local/bin}"
MODE="${1:-}"

case "$MODE" in
  --pipx)
    command -v pipx >/dev/null || { echo "pipx is not installed" >&2; exit 1; }
    pipx install --force "$REPO"
    exit 0
    ;;
  --uninstall)
    rm -f "$BIN_DIR/claude-login" "$BIN_DIR/ccl"
    echo "removed claude-login and ccl from $BIN_DIR"
    exit 0
    ;;
  "") ;;
  *)
    echo "usage: $0 [--pipx|--uninstall]" >&2
    exit 2
    ;;
esac

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$BIN_DIR"
chmod +x "$REPO/bin/claude-login"
ln -sf "$REPO/bin/claude-login" "$BIN_DIR/claude-login"
ln -sf "$REPO/bin/claude-login" "$BIN_DIR/ccl"

echo "installed:"
echo "  $BIN_DIR/claude-login -> $REPO/bin/claude-login"
echo "  $BIN_DIR/ccl          -> $REPO/bin/claude-login"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "note: $BIN_DIR is not on your PATH. Add this to your shell profile:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo
echo "next: run 'claude-login add' to sign your first account in."
