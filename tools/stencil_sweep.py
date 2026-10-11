#!/usr/bin/env python3
"""Check every master of a stencil library without looking at each one.

Per stencil: one page with every master (scaled) is schema-checked. Per master, built with the same code the
converter uses (stencil_lib): libvisio must draw something at native size; at SCALE, every coordinate libvisio
parses (paths, pictures, text blocks) must be the native one times SCALE; and the preview renderer that draws
stencil icons for Mermaid must handle it. --previews also renders one contact sheet per stencil for spot checks.

Usage: .venv/bin/python tools/stencil_sweep.py [stencils/1033] [--only KEY ...] [--jobs 8] [--previews] [--out DIR]
Prints one line per stencil (and its problems), writes report.json to the out dir; exit 1 if anything failed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)
import stencil_lib  # noqa: E402
from stencil_lib import Q  # noqa: E402

TEMPLATE = os.path.join(HERE, "template", "base.vsdx")
SCALE = 0.6
GAP, PAGE_W = 0.4, 24.0
# end points and boxes; arc radii are left out (libvisio turns a near-zero arc into a line at some scales)
MASTER_PART = re.compile(r"visio/masters/master\d+\.xml$")
COORD = re.compile(r"(svg:(?:x|y|x1|y1|x2|y2|width|height)): (-?[\d.]+(?:e[-+]?\d+)?)in")
DRAW_OPS = ("drawPath", "drawGraphicObject", "drawEllipse", "drawRectangle", "drawPolygon", "drawPolyline", "insertText")


def layout(sizes):
    """Shelf-pack (w, h) boxes into rows; return positions (x, y-from-top) and page size, inches."""
    pos, x, y, row = [], GAP, GAP, 0.0
    for w, h in sizes:
        if x + w > PAGE_W and x > GAP:
            x, y, row = GAP, y + row + GAP, 0.0
        pos.append((x, y))
        x, row = x + w + GAP, max(row, h)
    return pos, PAGE_W + GAP, y + row + GAP


def write(files, page, pw, ph, out):
    files["visio/pages/page1.xml"] = ET.tostring(page, encoding="utf-8", xml_declaration=True)
    pages = ET.fromstring(files["visio/pages/pages.xml"])
    sheet = pages.find(f"{Q('Page')}/{Q('PageSheet')}")
    for k, v in (("PageWidth", pw), ("PageHeight", ph)):
        sheet.find(f"{Q('Cell')}[@N='{k}']").set("V", str(v))
    files["visio/pages/pages.xml"] = ET.tostring(pages, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)


def build(masters, scale, out, single=False):
    """A page with the masters at scale: shelf-packed, or (single) one master at the origin on a page its size."""
    with zipfile.ZipFile(TEMPLATE) as z:
        files = {n: z.read(n) for n in z.namelist()}
    imp = stencil_lib.Importer(files)
    sizes = [tuple(max(abs(v), 0.05) for v in imp.size(m)) for m in masters]
    pos, pw, ph = ([(0, 0)], sizes[0][0], sizes[0][1]) if single else layout(sizes)
    page = ET.fromstring(files["visio/pages/page1.xml"])
    shapes = page.find(Q("Shapes"))
    shapes.clear()
    if page.find(Q("Connects")) is not None:
        page.remove(page.find(Q("Connects")))
    n = iter(range(1, 10 ** 7))
    for m, (w, h), (x, y) in zip(masters, sizes, pos):
        imp.place(shapes, m, lambda: next(n), x * scale, (ph - y - h) * scale, scale, name=m.name_u)
    imp.finish()
    write(files, page, pw * scale, ph * scale, out)


def parsed(path):
    """What libvisio draws: (number of drawing operations, every coordinate it parsed, in inches; x/y alternate
    within each path, so pairs are points)."""
    raw = subprocess.run(["vsd2raw", path], capture_output=True, text=True).stdout
    ops, nums = 0, []
    for line in raw.splitlines():
        t = line.strip()
        if t.startswith(DRAW_OPS):
            ops += 1
        if t.startswith(("drawPath", "drawGraphicObject", "drawEllipse", "drawRectangle", "startTextObject")):
            nums += [float(v) for _, v in COORD.findall(t)]
    return ops, nums


def check_master(m, tmp):
    import vsdx_preview
    a, b = os.path.join(tmp, "native.vsdx"), os.path.join(tmp, "scaled.vsdx")
    build([m], 1.0, a, single=True)
    build([m], SCALE, b, single=True)
    (ops, na), (_, sb) = parsed(a), parsed(b)
    out = []
    if not ops and m.name_u != "Dynamic connector":   # a connector draws only once routed between two shapes
        out.append("draws nothing")
    if len(na) != len(sb):
        # libvisio drops or merges near-zero segments differently at another size: then every scaled point must
        # still lie on a native point (times SCALE), and the extents must agree
        pa = [(x * SCALE, y * SCALE) for x, y in zip(na[0::2], na[1::2])]
        pb = list(zip(sb[0::2], sb[1::2]))
        far = [p for p in pb if min(abs(p[0] - q[0]) + abs(p[1] - q[1]) for q in pa) > 0.01] if pa else pb
        if far or not pa:
            out.append(f"scaled copy parses differently ({len(na)} vs {len(sb)} coordinates, {len(far)} points off)")
    else:
        # a coordinate either scales with the shape or is a fixed-size detail (a formula constant such as a 1/16 in
        # corner radius, which Visio keeps when resizing too); anything else is wrong
        tol = lambda x: 0.002 + 0.005 * abs(x)
        bad = [(x, y) for x, y in zip(na, sb) if abs(x * SCALE - y) > tol(x) and abs(x - y) > tol(x)]
        if bad:
            out.append(f"{len(bad)} of {len(na)} coordinates don't scale (e.g. {bad[0][0]:.4f} -> {bad[0][1]:.4f}, "
                       f"expected {bad[0][0] * SCALE:.4f})")
    try:
        vsdx_preview.to_svg(a)
    except Exception as e:   # noqa: BLE001
        out.append(f"preview renderer fails: {type(e).__name__}: {e}")
    return out


def check_stencil(args):
    path, outdir, previews = args
    try:
        return _check_stencil(path, outdir, previews)
    except Exception as e:   # noqa: BLE001 - one broken stencil must not stop the sweep
        return os.path.basename(path), 0, [f"sweep crashed: {type(e).__name__}: {e}"]


def _check_stencil(path, outdir, previews):
    import validate_vsdx
    st = stencil_lib.Stencil(path, lambda: open(path, "rb").read())
    masters = st.masters()
    if not masters:
        return st.key, 0, []
    problems = []
    tmp = tempfile.mkdtemp()
    sheet = os.path.join(outdir if previews else tmp, f"{st.key}.vsdx")
    build(masters, SCALE, sheet)
    # master parts are Microsoft's XML (only colours rewritten): schema-check what this code writes
    problems += [f"invalid: {p}" for p in validate_vsdx.check(sheet, lambda n: not MASTER_PART.match(n))[:5]]
    for m in masters:
        problems += [f"{m.name_u}: {p}" for p in check_master(m, tmp)]
    if previews:
        import vsdx_preview
        native = os.path.join(outdir, f"{st.key}_native.vsdx")
        build(masters, 1.0, native)
        vsdx_preview.render(vsdx_preview.to_svg(native), native[:-5] + ".png")
    return st.key, len(masters), problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("library", nargs="?", default=os.path.join(HERE, "stencils", "1033"))
    ap.add_argument("--out", default=os.path.join(HERE, "tests", "out", "stencils"))
    ap.add_argument("--only", nargs="*", help="stencil keys to check (e.g. PERIPH NETLOC)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--previews", action="store_true", help="also render a contact sheet per stencil into --out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    lib = stencil_lib.Library(a.library)
    todo = [s.label for s in lib.stencils if not a.only or s.key in {k.upper() for k in a.only}]
    report, total, failing, nprob = {}, 0, 0, 0
    with ProcessPoolExecutor(a.jobs) as ex:
        for key, n, probs in ex.map(check_stencil, [(p, a.out, a.previews) for p in todo]):
            total += n
            failing += bool(probs)
            nprob += len(probs)
            report[key] = {"masters": n, "problems": probs}
            print(f"{'ok  ' if not probs else 'FAIL'} {key:28} {n:4} masters" + (f"  {len(probs)} problems" if probs else ""), flush=True)
            for p in probs[:10]:
                print("       -", p[:220], flush=True)
    json.dump(report, open(os.path.join(a.out, "report.json"), "w"), indent=1)
    print(f"\n{len(todo)} stencils, {total} masters: {failing} stencils with problems, {nprob} problems")
    return 1 if nprob else 0


if __name__ == "__main__":
    sys.exit(main())
