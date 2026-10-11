#!/usr/bin/env bash
# One-time setup for mmd2vsdx. Installs everything inside this folder (no global installs):
#   node_modules/   puppeteer-core (~15 MB); the browser is an installed Chrome/Chromium/Edge when there is one
#   vendor/         pinned Mermaid, KaTeX and font files (~8 MB, tools/fetch_assets.py)
#   puppeteer.json  which browser the renderer and the measuring script drive
#   .venv/          xmlschema + pillow for validate_vsdx.py and the test suite
# Optional extra: libvisio (brew install libvisio) for previews and image tests.
set -euo pipefail
cd "$(dirname "$0")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
need node "https://nodejs.org, v18+"
need npm "comes with node"
need python3 "Python 3.9+"
node -e 'process.exit(parseInt(process.versions.node) >= 18 ? 0 : 1)' || { echo "node 18+ required"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "python 3.9+ required"; exit 1; }

# a browser for headless rendering: $CHROME_PATH, else a local Chrome/Chromium/Edge, else let puppeteer fetch one
chrome="${CHROME_PATH:-}"
if [ -z "$chrome" ]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium" \
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
           "$(command -v google-chrome || true)" "$(command -v chromium || true)" "$(command -v chromium-browser || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { chrome="$c"; break; }
  done
fi

echo "== installing the browser driver (puppeteer-core) into ./node_modules"
npm install --no-audit --no-fund --loglevel=error
if [ -z "$chrome" ]; then
  echo "   no local Chrome found; downloading Chrome for Testing into ./.browser"
  chrome="$(npx --yes @puppeteer/browsers install chrome@stable --path .browser | tail -1 | cut -d' ' -f2-)"
fi
echo "   using browser: $chrome"
python3 - "$chrome" <<'PY'
import json, sys
json.dump({"executablePath": sys.argv[1], "headless": "new", "args": ["--no-sandbox", "--disable-gpu"]},
          open("puppeteer.json", "w"), indent=2)
PY

echo "== fetching Mermaid and its fonts into ./vendor (about 8 MB)"
python3 tools/fetch_assets.py

echo "== python environment for checks and tests (./.venv)"
python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

echo "== optional tools"
command -v vsd2raw >/dev/null && echo "   libvisio: $(vsd2raw --version 2>&1 | head -1) (previews + image tests enabled)" \
  || echo "   libvisio: not found (brew install libvisio) - previews and image tests will be skipped"

echo "== smoke test"
./mmd2vsdx.py tests/cases/shapes.mmd -o "$(mktemp -d)/smoke.vsdx" --no-png
echo "setup complete. Try: ./mmd2vsdx.py your.mmd   or   .venv/bin/python tests/run_tests.py"
