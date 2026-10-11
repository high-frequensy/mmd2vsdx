#!/usr/bin/env python3
"""Regression suite for mmd2vsdx.

For every tests/cases/<name>.mmd (optional <name>.config.json = Mermaid config, <name>.args = extra CLI flags):
  1. convert            -> tests/out/<name>.vsdx (+ Mermaid's reference PNG)
  2. validate           -> patched Microsoft schema + glue/container consistency (validate_vsdx.py)
  3. snapshot           -> structural JSON (shapes, text, colours, glue, containers) must equal tests/golden/<name>.json
  4. image (if libvisio)-> libvisio preview must match tests/golden/<name>.png (mean pixel diff)

Usage: .venv/bin/python tests/run_tests.py [--update] [case ...]
  --update rewrites the golden files from the current output (review the diff before committing).
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from xml.etree import ElementTree as ET

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, ROOT)
V = "{http://schemas.microsoft.com/office/visio/2012/main}"
IMAGE_TOLERANCE = 0.0002   # share of pixels allowed to differ visibly (>40/255) from the golden preview


def snapshot(vsdx):
    """Everything a person would notice in the drawing, independent of the converter's internals."""
    z = zipfile.ZipFile(vsdx)
    page = ET.fromstring(z.read("visio/pages/page1.xml"))
    sheet = ET.fromstring(z.read("visio/pages/pages.xml")).find(f"{V}Page/{V}PageSheet")
    pcell = {c.get("N"): c.get("V") for c in sheet.findall(V + "Cell")}
    shapes = {s.get("ID"): s for s in page.find(V + "Shapes")}
    name = lambda i: shapes[i].get("NameU") if i in shapes else f"#{i}"
    r2 = lambda v: round(float(v), 2) if v not in (None, "") else None
    glue = {}
    for c in page.iter(V + "Connect"):
        glue.setdefault(c.get("FromSheet"), {})["begin" if c.get("FromPart") == "9" else "end"] = \
            f"{name(c.get('ToSheet'))}:{c.get('ToCell')}"
    out = {"page": [r2(pcell.get("PageWidth")), r2(pcell.get("PageHeight"))], "shapes": []}
    for sid, s in shapes.items():
        cell = {c.get("N"): c for c in s.findall(V + "Cell")}
        val = lambda n: cell[n].get("V") if n in cell else None
        text = s.find(V + "Text")
        runs = []
        if text is not None:
            styles = [r for sec in s.findall(V + "Section") if sec.get("N") == "Character" for r in sec.findall(V + "Row")]
            for cp in text.findall(V + "cp"):
                row = next((r for r in styles if r.get("IX") == cp.get("IX")), None)
                rc = {c.get("N"): c.get("V") for c in row.findall(V + "Cell")} if row is not None else {}
                runs.append([cp.tail or "", rc.get("Color"), r2(float(rc.get("Size", 0)) * 72),
                             int(rc.get("Style") or 0) | (16 if rc.get("Strikethru") == "1" else 0)])
        entry = {"name": s.get("NameU"), "master": s.get("Master"), "text": runs,
                 "line": [val("LineColor"), r2(float(val("LineWeight") or 0) * 96), val("LinePattern")],
                 "fill": val("FillForegnd") if val("FillPattern") == "1" else None}
        entry["text_box"] = [r2(val(n)) for n in ("TxtPinX", "TxtPinY", "TxtWidth", "TxtHeight", "LeftMargin",
                                                    "RightMargin", "TopMargin", "BottomMargin") if val(n) is not None]
        if s.get("Master"):   # connector
            entry.update(glue=glue.get(sid, {}), arrows=[val("BeginArrow"), val("EndArrow")],
                         reroute=val("ConFixedCode"), route=len(s.find(f"{V}Section[@N='Geometry']") or []) - 2)
        else:
            entry["box"] = [r2(val("PinX")), r2(val("PinY")), r2(val("Width")), r2(val("Height"))]
            entry["geometry_sections"] = sum(1 for sec in s.findall(V + "Section") if sec.get("N") == "Geometry")
            rel = val("Relationships") and cell["Relationships"].get("F")
            if rel:
                entry["relationships"] = re.sub(r"Sheet\.(\d+)!SheetRef\(\)", lambda m: name(m.group(1)), rel)
            if any(r.get("N") == "msvStructureType" for r in s.iter(V + "Row")):
                entry["container"] = True
        out["shapes"].append(entry)
    return out


def image_diff(a, b, diff_out):
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return float("inf"), f"size {ia.size} vs golden {ib.size}"
    d = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 40 else 0)
    share = d.histogram()[255] / (ia.size[0] * ia.size[1])
    if share > IMAGE_TOLERANCE:
        d.save(diff_out)
    return share, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="rewrite golden files from current output")
    ap.add_argument("cases", nargs="*")
    a = ap.parse_args()
    import mmd2vsdx
    import validate_vsdx   # imported here so --help works without xmlschema
    outdir = os.path.join(TESTS, "out")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)
    have_libvisio = shutil.which("vsd2raw") is not None
    cases = sorted(glob.glob(os.path.join(TESTS, "cases", "*.mmd")))
    if a.cases:
        cases = [c for c in cases if os.path.splitext(os.path.basename(c))[0] in a.cases]
    failures = skipped = 0
    have_stencils = any(f.lower().endswith(".vssx") for _, _, fs in os.walk(mmd2vsdx.STENCILS) for f in fs)
    for case in cases:
        name = os.path.splitext(os.path.basename(case))[0]
        if "visio:" in open(case).read() and not have_stencils:   # Microsoft's stencils are not in the repo
            print(f"skip {name:<14} needs Visio stencils (see docs/visio-stencils.md)")
            skipped += 1
            continue
        cfg = os.path.join(TESTS, "cases", name + ".config.json")
        vsdx, png = os.path.join(outdir, name + ".vsdx"), os.path.join(outdir, name + ".png")
        results = []
        cmd = [sys.executable, os.path.join(ROOT, "mmd2vsdx.py"), case, "-o", vsdx]
        if os.path.exists(cfg):
            cmd += ["--config", cfg]
        args = os.path.join(TESTS, "cases", name + ".args")   # optional extra CLI flags for this case
        if os.path.exists(args):
            cmd += shlex.split(open(args).read())
        conv = subprocess.run(cmd, capture_output=True, text=True)
        if conv.returncode:
            print(f"FAIL {name}: convert\n{conv.stderr}")
            failures += 1
            continue
        probs = validate_vsdx.check(vsdx)
        results.append(("validate", not probs, "; ".join(probs[:3])))
        snap = snapshot(vsdx)
        gjson = os.path.join(TESTS, "golden", name + ".json")
        if a.update or not os.path.exists(gjson):
            json.dump(snap, open(gjson, "w"), indent=1)
            results.append(("snapshot", True, "golden written"))
        else:
            gold = json.load(open(gjson))
            diffs = [f"{k}" for k in ("page",) if snap[k] != gold[k]]
            for i, (s1, s2) in enumerate(zip(snap["shapes"], gold["shapes"])):
                if s1 != s2:
                    keys = [k for k in set(s1) | set(s2) if s1.get(k) != s2.get(k)]
                    diffs.append(f"{s2.get('name')}: {', '.join(sorted(keys))}")
            if len(snap["shapes"]) != len(gold["shapes"]):
                diffs.append(f"shape count {len(snap['shapes'])} vs {len(gold['shapes'])}")
            results.append(("snapshot", not diffs, "; ".join(diffs[:4])))
        if have_libvisio:
            prev = os.path.join(outdir, name + "_preview.png")
            subprocess.run([sys.executable, os.path.join(ROOT, "vsdx_preview.py"), vsdx, prev], capture_output=True, check=True)
            gpng = os.path.join(TESTS, "golden", name + ".png")
            if a.update or not os.path.exists(gpng):
                shutil.copy(prev, gpng)
                results.append(("image", True, "golden written"))
            else:
                mean, why = image_diff(prev, gpng, os.path.join(outdir, name + "_diff.png"))
                results.append(("image", mean <= IMAGE_TOLERANCE, why or f"{mean:.3%} of pixels differ"))
        ok = all(r[1] for r in results)
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:<14} " + "  ".join(f"{k}:{'ok' if v else 'FAIL'}" for k, v, _ in results))
        for k, v, why in results:
            if not v or why.startswith("golden"):
                print(f"       {k}: {why}")
    print(f"\n{len(cases) - failures - skipped}/{len(cases) - skipped} cases passed" + (f", {skipped} skipped" if skipped else "") + ("" if have_libvisio else " (image checks skipped: no libvisio)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
