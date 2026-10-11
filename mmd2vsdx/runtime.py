"""Where the browser driver and Mermaid files live, and the one-time setup that fetches them.

`ensure()` runs before the first conversion: it installs puppeteer-core with npm (about 32 MB), finds a Chrome/Chromium/Edge
(or downloads Chrome for Testing) and downloads the pinned Mermaid, KaTeX and font files (about 8 MB) from npm. Run from a
git checkout everything goes in the checkout folder; when pip-installed it goes in the user cache folder.
Env overrides: MMD2VSDX_HOME (where it all lives), CHROME_PATH, MMD2VSDX_STENCILS, PUPPETEER_CONFIG, MMDC_MODULES.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PKG = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(PKG, "data")
ROOT = os.path.dirname(PKG)

# package -> (exact version, regexes of the files wanted, relative to the package root); Mermaid is exact on purpose:
# the converter reads details of its SVG
LIC = r"LICENSE(\.txt|\.md)?"
PINNED = {
    "mermaid": ("12.1.0", [r"dist/mermaid\.min\.js", LIC]),
    "katex": ("0.16.47", [r"dist/katex\.css", r"dist/fonts/.*", LIC]),
    "@fortawesome/fontawesome-free": ("7.3.1", [r"css/(brands|regular|solid|fontawesome)\.css", r"webfonts/.*", LIC]),
    "@fontsource/open-sans": ("5.3.0", [r"400(-italic)?\.css", r"files/open-sans-[\w-]+-400-(normal|italic)\.woff2?", LIC]),
    "@fontsource-variable/recursive": ("5.3.0", [r"index\.css", r"files/recursive-[\w-]+-wght-normal\.woff2?", LIC]),
    "@mermaid-js/mermaid-cli": ("12.0.0", [r"src/browser/(fontEmbedder|tokenizeCSS)\.js", LIC]),
}


def home():
    if os.environ.get("MMD2VSDX_HOME"):
        return os.path.abspath(os.environ["MMD2VSDX_HOME"])
    if os.path.exists(os.path.join(ROOT, "setup.sh")):   # a git checkout keeps everything in its own folder
        return ROOT
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "mmd2vsdx")


HOME = home()
MODULES = os.environ.get("MMDC_MODULES", os.path.join(HOME, "node_modules"))   # puppeteer-core
VENDOR = os.path.join(HOME, "vendor", "node_modules")                          # Mermaid, KaTeX, fonts
PPTR = os.environ.get("PUPPETEER_CONFIG", os.path.join(HOME, "puppeteer.json"))
STENCILS = os.environ.get("MMD2VSDX_STENCILS", os.path.join(HOME, "stencils"))
STAMP = os.path.join(HOME, "vendor", ".done")


def node_env():
    return {**os.environ, "MMDC_MODULES": MODULES, "MMD2VSDX_VENDOR": VENDOR}


def find_browser():
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    pf = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    fixed = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    fixed += [os.path.join(b, rel) for b in pf if b for rel in ("Google\\Chrome\\Application\\chrome.exe",
                                                               "Microsoft\\Edge\\Application\\msedge.exe")]
    for c in fixed + [shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "chrome", "microsoft-edge", "msedge")]:
        if c and os.path.isfile(c):
            return c
    return None


def _need(tool, hint):
    path = shutil.which(tool)
    if not path:
        sys.exit(f"mmd2vsdx needs {tool} ({hint}); install it and run again.")
    return path


def _fetch_assets():
    npm = _need("npm", "comes with Node")
    for name, (version, patterns) in PINNED.items():
        print(f"   {name}@{version}", file=sys.stderr, flush=True)
        keep = re.compile("|".join(f"(?:{p})" for p in patterns))
        with tempfile.TemporaryDirectory() as tmp:
            tgz = subprocess.run([npm, "pack", f"{name}@{version}", "--silent", "--pack-destination", tmp],
                                 check=True, capture_output=True, text=True).stdout.strip().splitlines()[-1]
            with tarfile.open(os.path.join(tmp, tgz)) as t:
                members = [m for m in t.getmembers() if m.isfile() and keep.fullmatch(m.name.split("/", 1)[1])]
                if not members:
                    sys.exit(f"nothing matched in {name}@{version}")
                for m in members:
                    dest = os.path.join(VENDOR, name, m.name.split("/", 1)[1])
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(t.extractfile(m).read())


def ensure():
    """Do the one-time setup if it has not been done (a few stat calls when it has)."""
    want = json.dumps({k: v[0] for k, v in PINNED.items()}, sort_keys=True)
    done = os.path.exists(STAMP) and open(STAMP).read() == want
    if done and os.path.isdir(os.path.join(MODULES, "puppeteer-core")) and os.path.exists(PPTR):
        return
    print(f"mmd2vsdx: one-time setup in {HOME} (about 40 MB, needs network)", file=sys.stderr, flush=True)
    node = _need("node", "https://nodejs.org, v18+")
    npm = _need("npm", "comes with Node")
    if int(subprocess.run([node, "-p", "process.versions.node.split('.')[0]"], capture_output=True, text=True).stdout or 0) < 18:
        sys.exit("mmd2vsdx needs Node 18 or newer.")
    os.makedirs(HOME, exist_ok=True)
    for f in ("package.json", "package-lock.json"):
        shutil.copyfile(os.path.join(DATA, f), os.path.join(HOME, f))
    if not os.path.isdir(os.path.join(MODULES, "puppeteer-core")):
        print("== installing the browser driver (puppeteer-core)", file=sys.stderr, flush=True)
        subprocess.run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=HOME, check=True)
    if not os.path.exists(PPTR) or os.environ.get("CHROME_PATH"):
        chrome = find_browser()
        if not chrome:
            print("== no Chrome, Chromium or Edge found; downloading Chrome for Testing", file=sys.stderr, flush=True)
            npx = _need("npx", "comes with Node")
            out = subprocess.run([npx, "--yes", "@puppeteer/browsers", "install", "chrome@stable", "--path", os.path.join(HOME, ".browser")],
                                 check=True, capture_output=True, text=True, cwd=HOME).stdout.strip().splitlines()[-1]
            chrome = out.split(" ", 1)[1]   # "chrome@<version> <path to the executable>"
        print(f"== using browser: {chrome}", file=sys.stderr, flush=True)
        with open(PPTR, "w") as f:
            json.dump({"executablePath": chrome, "headless": "new", "args": ["--no-sandbox", "--disable-gpu"]}, f, indent=2)
    if not done:
        print("== fetching Mermaid and its fonts (about 8 MB)", file=sys.stderr, flush=True)
        _fetch_assets()
        with open(STAMP, "w") as f:
            f.write(want)


if __name__ == "__main__":
    ensure()
