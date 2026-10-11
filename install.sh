#!/usr/bin/env bash
# Put mmd2vsdx on this machine's PATH, so it runs from any directory.
#   ./install.sh            (re-run any time; it only refreshes the link)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd -P)"
[ -d "$here/node_modules" ] && [ -d "$here/.venv" ] || "$here/setup.sh"

mkdir -p "$HOME/.local/bin"
ln -sfn "$here/mmd2vsdx.py" "$HOME/.local/bin/mmd2vsdx"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH to use 'mmd2vsdx' directly" ;; esac
echo "installed: mmd2vsdx -> $here/mmd2vsdx.py"
echo "try: cd /tmp && mmd2vsdx $here/examples/shapes.mmd -o shapes.vsdx"
