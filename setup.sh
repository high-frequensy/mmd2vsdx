#!/usr/bin/env bash
# One-time setup of a git checkout (everything stays inside this folder, no global installs):
#   .venv/          xmlschema + pillow for validate_vsdx and the test suite
#   node_modules/   puppeteer-core (~32 MB), vendor/ pinned Mermaid and fonts (~8 MB), puppeteer.json (which browser to drive)
# The last three are made by mmd2vsdx/runtime.py, which also runs by itself on the first conversion.
# Optional extra: libvisio (brew install libvisio) for previews and image tests.
set -euo pipefail
cd "$(dirname "$0")"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "python 3.9+ required"; exit 1; }
echo "== python environment for checks and tests (./.venv)"
python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

echo "== browser driver, Mermaid and fonts"
.venv/bin/python -m mmd2vsdx.runtime

echo "== optional tools"
command -v vsd2raw >/dev/null && echo "   libvisio: $(vsd2raw --version 2>&1 | head -1) (previews + image tests enabled)" \
  || echo "   libvisio: not found (brew install libvisio) - previews and image tests will be skipped"

echo "== smoke test"
.venv/bin/python -m mmd2vsdx tests/cases/shapes.mmd -o "$(mktemp -d)/smoke.vsdx" --no-png
echo "setup complete. Try: .venv/bin/python -m mmd2vsdx your.mmd   or   .venv/bin/python tests/run_tests.py"
