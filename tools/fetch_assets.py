#!/usr/bin/env python3
"""Download the few files render.cjs needs from npm into vendor/node_modules/ (about 30 MB instead of mermaid-cli's 440 MB).

Each package is fetched with `npm pack` (npm checks its integrity) at an exact version, and only the listed files are
unpacked. setup.sh runs this; re-running does nothing once vendor/.done matches PINNED.
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(HERE, "vendor", "node_modules")
LIC = r"LICENSE(\.txt|\.md)?"

# package -> (exact version, regexes of the files wanted, relative to the package root)
PINNED = {
    "mermaid": ("12.1.0", [r"dist/mermaid\.min\.js", LIC]),
    "katex": ("0.16.47", [r"dist/katex\.css", r"dist/fonts/.*", LIC]),
    "@fortawesome/fontawesome-free": ("7.3.1", [r"css/(brands|regular|solid|fontawesome)\.css", r"webfonts/.*", LIC]),
    "@fontsource/open-sans": ("5.3.0", [r"400(-italic)?\.css", r"files/open-sans-[\w-]+-400-(normal|italic)\.woff2?", LIC]),
    "@fontsource-variable/recursive": ("5.3.0", [r"index\.css", r"files/recursive-[\w-]+-wght-normal\.woff2?", LIC]),
    "@mermaid-js/mermaid-cli": ("12.0.0", [r"src/browser/(fontEmbedder|tokenizeCSS)\.js", LIC]),
}


def main():
    stamp = os.path.join(HERE, "vendor", ".done")
    want = json.dumps({k: v[0] for k, v in PINNED.items()}, sort_keys=True)
    if os.path.exists(stamp) and open(stamp).read() == want:
        return
    for name, (version, patterns) in PINNED.items():
        print(f"   {name}@{version}", flush=True)
        keep = re.compile("|".join(f"(?:{p})$" for p in patterns))
        with tempfile.TemporaryDirectory() as tmp:
            tgz = subprocess.run(["npm", "pack", f"{name}@{version}", "--silent", "--pack-destination", tmp],
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
    open(stamp, "w").write(want)


if __name__ == "__main__":
    main()
