#!/usr/bin/env bash
# Install mmd2vsdx and put the `mmd2vsdx` command on this machine's PATH. Run it from a checkout, or straight from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/high-frequensy/mmd2vsdx/main/install.sh | bash
# Re-running updates it. Env: MMD2VSDX_REF (tag or branch to install, default the latest release),
# MMD2VSDX_DIR (where, default ~/.local/share/mmd2vsdx).
set -euo pipefail
repo=high-frequensy/mmd2vsdx
src="${BASH_SOURCE[0]:-}"
if [ -n "$src" ] && [ -f "$(dirname "$src")/mmd2vsdx/__init__.py" ]; then
  here="$(cd "$(dirname "$src")" && pwd -P)"
else   # piped from curl: unpack the release next to nothing else of ours
  here="${MMD2VSDX_DIR:-$HOME/.local/share/mmd2vsdx}"
  ref="${MMD2VSDX_REF:-$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest" | sed 's#.*/##')}"
  case "$ref" in ""|latest) ref=main ;; esac
  echo "== downloading mmd2vsdx $ref into $here"
  mkdir -p "$here"
  # ponytail: unpacks over an existing copy, so files deleted upstream stay behind; your stencils/ and downloads are kept
  curl -fsSL "https://github.com/$repo/archive/$ref.tar.gz" | tar -xz --strip-components=1 -C "$here"
fi
PYTHONPATH="$here" python3 -m mmd2vsdx.runtime   # the one-time fetch of the browser driver and Mermaid; no test environment needed

mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\nPYTHONPATH="%s" exec python3 -m mmd2vsdx "$@"\n' "$here" > "$HOME/.local/bin/mmd2vsdx"
chmod +x "$HOME/.local/bin/mmd2vsdx"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH to use 'mmd2vsdx' directly" ;; esac
echo "installed: mmd2vsdx -> $here"
echo "try: cd /tmp && mmd2vsdx $here/examples/shapes.mmd -o shapes.vsdx"
