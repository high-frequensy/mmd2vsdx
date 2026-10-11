#!/usr/bin/env python3
"""Convert a Mermaid flowchart into an editable .vsdx that matches Mermaid's own render 1:1.

Usage: mmd2vsdx diagram.mmd [more.mmd | notes.md ...] [-o out.vsdx|dir] [options]   (see --help)
  Renders each diagram with the pinned Mermaid (render.cjs) using mermaid-config.json (Arial, so the font exists in
  Visio too), writes the matching reference PNG, then rebuilds the drawing as native Visio shapes:
  - nodes of any shape: Mermaid's outline (rect, polygon, circle, path incl. curves/arcs) becomes
    Visio geometry that scales with the shape; fills, strokes, rounding and neo shadow kept
  - subgraphs (nested too): real Visio containers (members move with the frame), with their titles
  - text: Chrome's exact line breaks (measure_lines.cjs), same font, size, line height and colours
  - edges: Visio Dynamic connectors glued to both shapes, same route, rounded bends, solid/dotted/
    thick strokes, triangle/circle/cross ends, labels at Mermaid's position with the same background
Markdown input converts every ```mermaid block (name-1.vsdx, name-2.vsdx, ...).
The first run sets up the browser driver and Mermaid (runtime.py). Env overrides: see runtime.py.
"""
import argparse
import base64
import html
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from . import runtime
from .runtime import DATA, PPTR, STENCILS   # STENCILS: Visio stencil library (.vssx)
TEMPLATE = os.path.join(DATA, "base.vsdx")   # carries the Dynamic connector master
RENDER = os.path.join(DATA, "render.cjs")   # Mermaid -> SVG/PNG in Chrome (puppeteer-core + vendor/)
CONFIG = os.path.join(DATA, "mermaid-config.json")
REROUTE = {"freely": 0, "as-needed": 1, "never": 2}   # Visio ConFixedCode

SVGNS = "{http://www.w3.org/2000/svg}"
NS = "http://schemas.microsoft.com/office/visio/2012/main"
ET.register_namespace("", NS)
ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
Q = lambda n: f"{{{NS}}}{n}"
S = 96            # CSS px per inch, so Visio sizes equal the browser's
MARGIN = 0.4      # inches around the drawing
DYN_MASTER = "2"
ARROWS = {"point": 4, "circle": 42, "cross": 23}   # Visio arrowheads: filled triangle, filled circle, oblique line (no plain X)
PATTERNS = {"solid": 1, "dashed": 2, "dotted": 3}
with ZipFile(TEMPLATE) as _z:   # how many route rows the connector master defines
    _m = ET.fromstring(_z.read("visio/masters/master1.xml"))
    MASTER_GEO_ROWS = max(int(r.get("IX")) for r in _m.iter(Q("Row")) if r.get("T") in ("MoveTo", "LineTo"))


# ---------- small SVG helpers ----------
def style_of(el):
    st = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            st[k.strip()] = v.replace("!important", "").strip()
    return st


NAMED = {"white": "#FFFFFF", "black": "#000000"}


def hexcolor(css, over="#FFFFFF"):
    """CSS colour -> '#RRGGBB' (alpha blended over white); None for none/transparent/unknown."""
    if not css:
        return None
    css = NAMED.get(css.strip().lower(), css.strip())
    if css.startswith("#"):
        h = css[1:]
        return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h[:6]).upper()
    nums = [float(v) for v in re.findall(r"[\d.]+", css)]
    if len(nums) < 3:
        return None
    a = nums[3] if len(nums) > 3 else 1.0
    if a == 0:
        return None
    base = [int(over[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c * a + b * (1 - a)):02X}" for c, b in zip(nums[:3], base))


def css_value(css, selector_part, prop, default):
    for rule in css.split("}"):
        if selector_part in rule.split("{")[0]:
            m = re.search(prop + r"\s*:\s*([^;]+)", rule)
            if m:
                return m.group(1).strip()
    return default


def dashed(css):
    """True for a real dash array; Mermaid's rough.js shapes write '0 0', which draws solid."""
    return any(float(v) > 0 for v in re.findall(r"[\d.]+", css or ""))


def px(v, default=0.0):
    try:
        return float(str(v).replace("px", ""))
    except (TypeError, ValueError):
        return default


def mat_mul(m, n):   # affine (a,b,c,d,e,f) composition: m after n
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D, a * E + c * F + e, b * E + d * F + f)


def parse_transform(t):
    m = (1, 0, 0, 1, 0, 0)
    for fn, args in re.findall(r"(\w+)\s*\(([^)]*)\)", t or ""):
        v = [float(x) for x in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", args)]
        if fn == "translate":
            n = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif fn == "scale":
            n = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif fn == "matrix":
            n = tuple(v)
        elif fn == "rotate":
            r = math.radians(v[0])
            n = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                n = mat_mul(mat_mul((1, 0, 0, 1, v[1], v[2]), n), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        m = mat_mul(m, n)
    return m


def apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def flatten_path(d):
    """SVG path data -> [(points, closed)], curves and arcs sampled finely enough to be smooth."""
    toks = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", d)
    subs, pts, i, cmd = [], [], 0, None
    cx = cy = sx = sy = 0.0
    last_ctrl = None

    def num():
        nonlocal i
        i += 1
        return float(toks[i - 1])

    def bez(p0, p1, p2, p3, n=16):
        for k in range(1, n + 1):
            t = k / n
            u = 1 - t
            pts.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                        u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))

    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if pts:
                    subs.append((pts, True))
                pts, (cx, cy) = [], (sx, sy)
                continue
        rel = cmd.islower()
        ox, oy = (cx, cy) if rel else (0, 0)
        c = cmd.upper()
        if c == "M":
            if pts:
                subs.append((pts, False))
            cx, cy = num() + ox, num() + oy
            sx, sy, pts = cx, cy, [(cx, cy)]
            cmd = "l" if rel else "L"
        elif c == "L":
            cx, cy = num() + ox, num() + oy
            pts.append((cx, cy))
        elif c == "H":
            cx = num() + ox
            pts.append((cx, cy))
        elif c == "V":
            cy = num() + oy
            pts.append((cx, cy))
        elif c in "CS":
            if c == "C":
                p1 = (num() + ox, num() + oy)
            else:
                p1 = (2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1]) if last_ctrl else (cx, cy)
            p2 = (num() + ox, num() + oy)
            p3 = (num() + ox, num() + oy)
            bez((cx, cy), p1, p2, p3)
            last_ctrl, (cx, cy) = p2, p3
            continue
        elif c in "QT":
            q = (num() + ox, num() + oy) if c == "Q" else ((2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1]) if last_ctrl else (cx, cy))
            p3 = (num() + ox, num() + oy)
            bez((cx, cy), ((cx + 2 * q[0]) / 3, (cy + 2 * q[1]) / 3), ((p3[0] + 2 * q[0]) / 3, (p3[1] + 2 * q[1]) / 3), p3)
            last_ctrl, (cx, cy) = q, p3
            continue
        elif c == "A":
            rx, ry, phi, large, sweep = abs(num()), abs(num()), math.radians(num()), num(), num()
            x2, y2 = num() + ox, num() + oy
            if rx == 0 or ry == 0:
                pts.append((x2, y2))
            else:   # SVG endpoint -> centre parameterisation (SVG spec F.6.5)
                cp, sp = math.cos(phi), math.sin(phi)
                dx, dy = (cx - x2) / 2, (cy - y2) / 2
                x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
                lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
                if lam > 1:
                    rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
                num_ = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
                k = math.sqrt(max(0, num_ / (rx * rx * y1p * y1p + ry * ry * x1p * x1p))) * (-1 if large == sweep else 1)
                cxp, cyp = k * rx * y1p / ry, -k * ry * x1p / rx
                ccx, ccy = cp * cxp - sp * cyp + (cx + x2) / 2, sp * cxp + cp * cyp + (cy + y2) / 2
                ang = lambda ux, uy, vx, vy: math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
                t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
                dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
                if not sweep and dt > 0:
                    dt -= 2 * math.pi
                elif sweep and dt < 0:
                    dt += 2 * math.pi
                n = max(4, int(abs(dt) / (math.pi / 24)) + 1)
                for kk in range(1, n + 1):
                    t = t1 + dt * kk / n
                    pts.append((ccx + rx * math.cos(t) * cp - ry * math.sin(t) * sp,
                                ccy + rx * math.cos(t) * sp + ry * math.sin(t) * cp))
            cx, cy = x2, y2
        last_ctrl = None
    if pts:
        subs.append((pts, False))
    return subs


def font_name(css_family):
    """First family of a CSS font-family list, as Visio names it ('"courier new", x' -> 'Courier New')."""
    return css_family.split(",")[0].strip("\"' ").title()


def runs_from(lines, hard=None, soft=False):
    """Measured lines -> [(text, run style "bits|color|size|font"; bits: 1 bold, 2 italic, 4 underline, 16 strike)]. Exact mode breaks every measured line; soft mode keeps only
    explicit <br> breaks and joins wrapped lines with a space so Visio re-wraps them."""
    runs = []
    for i, line in enumerate(lines):
        for j, (t, b) in enumerate(line):
            brk = "" if not i or j else ("\n" if not soft or (hard and hard[i]) else " ")
            t = brk + t
            if runs and runs[-1][1] == b:
                runs[-1] = (runs[-1][0] + t, b)
            else:
                runs.append((t, b))
    return runs


# ---------- Visio stencil icons: A@{ icon: "visio:Server" } ----------
_libraries = {}


def library(path):
    if path not in _libraries:
        from . import stencil_lib
        _libraries[path] = stencil_lib.Library(path)
    return _libraries[path]


def resolve_master(lib, query, prefer=()):
    """The master for visio:query. An ambiguous name prefers a stencil the diagram already uses (a 3D diagram
    stays 3D), then the first stencil alphabetically."""
    hits = lib.find(query)
    hits.sort(key=lambda m: m.key not in prefer)
    if not hits:
        near = ", ".join(f"visio:{m!r}" for m in lib.search(query.split("/")[-1])[:8])
        raise AssertionError(f"no stencil master named {query!r}" + (f"; similar: {near}" if near else ""))
    if len(hits) > 1:
        print(f"note: visio:{query} matches {len(hits)} masters ({', '.join(map(repr, hits[:6]))}"
              f"{', ...' if len(hits) > 6 else ''}); using {hits[0]!r}. Write visio:STENCIL/NAME to pick another.",
              file=sys.stderr)
    return hits[0]


def master_svg(m):
    """The master as libvisio draws it (SVG, transparent), and its native size in inches."""
    from . import stencil_lib, vsdx_preview
    with ZipFile(TEMPLATE) as z:
        pkg = {n: z.read(n) for n in z.namelist()}
    imp = stencil_lib.Importer(pkg)
    w, h = imp.size(m)
    pg = ET.fromstring(pkg["visio/pages/page1.xml"])
    pg.find(Q("Shapes")).clear()
    ids = iter(range(1, 10 ** 6))
    imp.place(pg.find(Q("Shapes")), m, lambda: next(ids), 0, 0)
    imp.finish()
    pkg["visio/pages/page1.xml"] = ET.tostring(pg, encoding="utf-8", xml_declaration=True)
    pgs = ET.fromstring(pkg["visio/pages/pages.xml"])
    sheet = pgs.find(Q("Page")).find(Q("PageSheet"))
    for n, v in (("PageWidth", w), ("PageHeight", h)):
        sheet.find(f"{Q('Cell')}[@N='{n}']").set("V", str(v))
    pkg["visio/pages/pages.xml"] = ET.tostring(pgs, encoding="utf-8", xml_declaration=True)
    path = os.path.join(tempfile.mkdtemp(), "master.vsdx")
    with ZipFile(path, "w") as z:
        for n, c in pkg.items():
            z.writestr(n, c)
    return vsdx_preview.to_svg(path, background=False), w, h


ICON_NODE = re.compile(r"([A-Za-z0-9_][\w-]*)\s*@\{([^{}]*)\}", re.S)
FIELD = re.compile(r"""(\w+)\s*:\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^,}\n]+)""")


def use_stencils(text, stencils):
    """Rewrite each A@{ icon: "visio:NAME", ... } node into an image node of the master's exact size (drawn by
    libvisio, so Mermaid's own render shows the stencil icon too); return the text and {node id: (master, scale)}.
    Optional h / w (px) scale the master uniformly; label and pos are kept."""
    found = {}
    lib = library(stencils)
    # stencils named explicitly (visio:STENCIL/NAME) or matched uniquely anywhere in the diagram set the preference
    asked = [v.strip().strip("\"'")[6:] for blk in ICON_NODE.findall(text) for k, v in FIELD.findall(blk[1])
             if k == "icon" and v.strip().strip("\"'").startswith("visio:")]
    prefer = {h[0].key for q in asked for h in [lib.find(q)] if len(h) == 1}

    def swap(m):
        f = {k: v.strip().strip("\"'") for k, v in FIELD.findall(m.group(2))}
        if not f.get("icon", "").startswith("visio:"):
            return m.group(0)
        master = resolve_master(lib, f["icon"][6:].strip(), prefer)
        svg, w, h = master_svg(master)
        scale = float(f["h"]) / (h * S) if "h" in f else float(f["w"]) / (w * S) if "w" in f else 1.0
        found[m.group(1)] = (master, scale)
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        keep = "".join(f', {k}: "{f[k]}"' for k in ("label", "pos") if k in f)
        return f'{m.group(1)}@{{ img: "{uri}", w: {w * S * scale:.2f}, h: {h * S * scale:.2f}, constraint: "on"{keep} }}'

    text = ICON_NODE.sub(swap, text)
    if found:   # the picture is the stencil shape: no image frame or background
        text = text.rstrip() + "\n" + "".join(f"  style {k} fill:none,stroke:none\n" for k in found)
    return text, found


# ---------- render + measure ----------
def convert(src, out_path, png_path=None, config=CONFIG, reroute="as-needed", wrap="exact", stencils=None):
    """Convert one .mmd file to out_path (.vsdx); optionally also write Mermaid's own PNG to png_path."""
    runtime.ensure()
    tmp_svg = os.path.join(tempfile.mkdtemp(), "diagram.svg")
    text = open(src, encoding="utf-8").read()
    visio_icons = {}
    if "visio:" in text:
        text, visio_icons = use_stencils(text, stencils or STENCILS)
        src = os.path.join(os.path.dirname(tmp_svg), "diagram.mmd")
        open(src, "w", encoding="utf-8").write(text)
        if visio_icons:   # the embedded stencil pictures easily pass Mermaid's 50,000-character source limit
            cfg = json.load(open(config, encoding="utf-8"))
            cfg["maxTextSize"] = max(cfg.get("maxTextSize", 50000), len(text) * 2)
            # a stencil shape's caption is plain text, like the shape's own text in Visio: no image-label chip
            cfg["themeCSS"] = cfg.get("themeCSS", "") + "".join(
                f'[id*="-flowchart-{k}-"] p, [id*="-flowchart-{k}-"] span, [id*="-flowchart-{k}-"] div, '
                f'[id*="-flowchart-{k}-"] .label {{ background-color: transparent !important; }}'
                f'[id*="-flowchart-{k}-"] .label rect {{ fill: none !important; }}' for k in visio_icons)
            config = os.path.join(os.path.dirname(tmp_svg), "config.json")
            json.dump(cfg, open(config, "w", encoding="utf-8"))
    for target, extra in ((tmp_svg, []),) + (((png_path, ["-s", "2"]),) if png_path else ()):
        subprocess.run(["node", RENDER, "-i", src, "-o", target, "-c", config, "-p", PPTR, "-b", "white", "-q", *extra],
                       env=runtime.node_env(), check=True)
    measured_all = json.loads(subprocess.run(["node", os.path.join(DATA, "measure_lines.cjs"), tmp_svg, PPTR, src],
                                         env=runtime.node_env(),
                                         check=True, capture_output=True, text=True).stdout)
    measured = measured_all["labels"]

    svg = ET.parse(tmp_svg).getroot()
    assert "Maximum text size in diagram exceeded" not in open(tmp_svg, encoding="utf-8").read(), \
        "Mermaid refused the diagram: text larger than its maxTextSize (raise it in the --config file)"
    assert "flowchart" in (svg.get("aria-roledescription") or "") or "flowchart" in (svg.get("class") or ""), \
        "only Mermaid flowcharts are supported so far"
    parent = {c: p for p in svg.iter() for c in p}
    LABEL_GROUPS = {"label", "cluster-label"}   # text holders inside nodes/clusters (their rects are not outline)
    DRAWABLE = {SVGNS + t for t in ("rect", "polygon", "path", "circle", "ellipse", "line", "polyline")}
    computed = dict(zip((e for e in svg.iter() if e.tag in DRAWABLE), measured_all["paints"]))   # Chrome's resolved CSS


    def ctm(el):
        """Accumulated transform from el's own coordinates to the SVG root."""
        m, chain = (1, 0, 0, 1, 0, 0), []
        while el is not None:
            chain.append(el)
            el = parent.get(el)
        for e in reversed(chain):
            m = mat_mul(m, parse_transform(e.get("transform")))
        return m


    css = "".join(s.text or "" for s in svg.iter(SVGNS + "style"))
    NODE_FILL = hexcolor(css_value(css, ".node rect", "fill", "#ffffff"))
    NODE_STROKE = hexcolor(css_value(css, ".node rect", "stroke", "#333333"))
    NODE_W = px(css_value(css, ".node rect", "stroke-width", "1px"), 1)
    EDGE_W = {"normal": px(css_value(css, ".edge-thickness-normal", "stroke-width", "2px"), 2),
              "thick": px(css_value(css, ".edge-thickness-thick", "stroke-width", "3.5px"), 3.5)}
    EDGE_C = hexcolor(css_value(css, ".flowchart-link", "stroke", "#333333"))
    shadow = None
    for f in svg.iter(SVGNS + "filter"):
        if f.get("id", "").endswith("-drop-shadow"):
            d = f.find(SVGNS + "feDropShadow")
            shadow = (float(d.get("dx")), float(d.get("dy")), float(d.get("flood-opacity")), d.get("flood-color"))

    vx, vy, vw, vh = map(float, svg.get("viewBox").split())
    PW, PH = vw / S + 2 * MARGIN, vh / S + 2 * MARGIN
    X = lambda p: (p - vx) / S + MARGIN
    Y = lambda p: PH - ((p - vy) / S + MARGIN)          # Visio y grows upward


    def text_of(key):
        m = measured.get(key)
        if not m:
            return None
        runs = runs_from(m["lines"], m.get("hard"), wrap == "visio")
        widths = iter(m.get("pics", []))   # an inline picture's place: no-break spaces as wide as the picture
        runs = [(re.sub("\uFFFC", lambda _: "\u00a0" * max(1, round(next(widths)[2] / (m["nbsp"] or m["size"] * 0.278))), t), st)
                for t, st in runs]
        return dict(runs=runs, color=hexcolor(m["color"]), size=m["size"],
                    lh=m["lineHeight"], font=font_name(m["font"]), bg=hexcolor(m["bg"]),
                    align={"center": 1, "right": 2}.get(m.get("align"), 0), box=m.get("box"),
                    angle=m.get("angle"), local=m.get("local"))


    def paint(el, prop, default):
        """Effective fill/stroke as Chrome resolved it (all CSS rules applied); falls back to style/attributes."""
        c = computed.get(el)
        if c:
            if prop == "fill" and c["fill"] not in ("none", "") and float(c.get("fillOpacity") or 1) > 0:
                return c["fill"]
            if prop == "stroke" and float(c.get("strokeOpacity") or 1) == 0:
                return "none"
            return c[prop] if c[prop] not in ("none", "") else "none"
        e = el
        while e is not None and e.tag != SVGNS + "svg":
            v = style_of(e).get(prop) or e.get(prop)
            if v:
                return v
            e = parent.get(e)
        return default


    def outline_parts(node):
        """Every drawable piece of a node's outline (label excluded) in absolute coordinates."""
        parts = []

        def walk(el):
            for k in el:
                if k.tag == SVGNS + "g" and LABEL_GROUPS & set((k.get("class") or "").split()):
                    continue
                tag = k.tag.replace(SVGNS, "")
                m = ctm(k)
                fill = hexcolor(paint(k, "fill", NODE_FILL or "#ffffff"))
                stroke = hexcolor(paint(k, "stroke", NODE_STROKE))
                sw = computed[k]["sw"] if k in computed else px(style_of(k).get("stroke-width") or k.get("stroke-width"), NODE_W)
                if sw == 0:
                    stroke = None
                if tag == "rect" and k.get("width"):
                    x, y, w, h = (px(k.get(a)) for a in ("x", "y", "width", "height"))
                    pts = [apply(m, *p) for p in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]
                    parts.append(dict(kind="poly", pts=pts, closed=True, fill=fill, stroke=stroke, sw=sw,
                                      rx=px(k.get("rx"))))
                elif tag == "polygon":
                    v = [float(t) for t in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", k.get("points"))]
                    parts.append(dict(kind="poly", pts=[apply(m, v[j], v[j + 1]) for j in range(0, len(v), 2)],
                                      closed=True, fill=fill, stroke=stroke, sw=sw, rx=0))
                elif tag in ("circle", "ellipse"):
                    ccx, ccy = apply(m, px(k.get("cx")), px(k.get("cy")))
                    rx = px(k.get("r") or k.get("rx")) * math.hypot(m[0], m[1])
                    ry = px(k.get("r") or k.get("ry")) * math.hypot(m[2], m[3])
                    parts.append(dict(kind="ellipse", c=(ccx, ccy), r=(rx, ry), fill=fill, stroke=stroke, sw=sw))
                elif tag == "path" and k.get("d"):
                    for pts, closed in flatten_path(k.get("d")):
                        pts = [apply(m, *p) for p in pts]
                        if fill and not closed and len(pts) > 2 and stroke and pts[0] != pts[-1]:
                            # SVG fills an open subpath as if closed but strokes only what is drawn
                            parts.append(dict(kind="poly", pts=pts, closed=True, fill=fill, stroke=None, sw=sw, rx=0))
                            fill = None
                        parts.append(dict(kind="poly", pts=pts, closed=closed,
                                          fill=fill if closed or len(pts) > 2 else None, stroke=stroke, sw=sw, rx=0))
                elif tag == "g":
                    walk(k)

        walk(node)
        return parts


    def bbox(parts):
        xs, ys = [], []
        for p in parts:
            if p["kind"] == "ellipse":
                xs += [p["c"][0] - p["r"][0], p["c"][0] + p["r"][0]]
                ys += [p["c"][1] - p["r"][1], p["c"][1] + p["r"][1]]
            else:
                xs += [q[0] for q in p["pts"]]
                ys += [q[1] for q in p["pts"]]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


    boxes = []
    for g in svg.iter(SVGNS + "g"):
        cls = (g.get("class") or "").split()
        if "cluster" in cls:
            parts = outline_parts(g)   # the frame (a rect, or rough.js paths when handDrawn); titles excluded
            frame = [e for e in g.iter() if e.tag in DRAWABLE and not LABEL_GROUPS & set((parent[e].get("class") or "").split())]
            x, y, w, h = bbox(parts)
            t = text_of(g.get("id"))
            lx, ly, lw = (t["box"][0], t["box"][1], t["box"][2]) if t and t["box"] else (x, y, w)
            boxes.append(dict(key=g.get("id").split("-", 2)[-1], x=x, y=y, w=w, h=h, parts=parts,
                              dash=any(dashed(computed.get(e, {}).get("dash")) or dashed(style_of(e).get("stroke-dasharray"))
                                       for e in frame),
                              text=t, cluster=True, pad=(lx - x, ly - y, max(x + w - lx - lw, 0), 0)))   # title box
        elif {"node", "rough-node", "icon-shape", "image-shape"} & set(cls) and re.match(r".*flowchart-.+-\d+$", g.get("id", "")):
            parts = outline_parts(g)
            if not parts:
                continue
            x, y, w, h = bbox(parts)
            t = text_of(g.get("id"))
            pad = (0, 0, 0, 0)
            if t and t["box"]:   # centre the text block on Mermaid's label centre, not the outline's bbox
                lcx, lcy = t["box"][0] + t["box"][2] / 2, t["box"][1] + t["box"][3] / 2
                hx, hy = min(lcx - x, x + w - lcx), min(lcy - y, y + h - lcy)
                pad = (lcx - hx - x, lcy - hy - y, x + w - (lcx + hx), y + h - (lcy + hy))
            shadowed = any("drop-shadow" in (computed.get(e, {}).get("filter") or "") for e in g.iter() if e.tag in DRAWABLE)
            chip = bool(t and t["box"] and t["bg"] and {"icon-shape", "image-shape"} & set(cls))
            if chip:   # image/icon nodes put their label on a chip: text block = exactly the label box
                lx, ly, lw, lh = t["box"]
                pad = (lx - x, ly - y, x + w - lx - lw, y + h - ly - lh)
            dash = any(dashed(computed.get(e, {}).get("dash")) for e in g.iter() if e.tag in DRAWABLE
                       and "label" not in (parent[e].get("class") or ""))
            a = parent.get(g)   # click links wrap the node in <a>
            link = a.get("{http://www.w3.org/1999/xlink}href") or a.get("href") if a is not None and a.tag == SVGNS + "a" else None
            boxes.append(dict(key=re.match(r".*flowchart-(.+)-\d+$", g.get("id")).group(1), x=x, y=y, w=w, h=h,
                              parts=parts, dash=dash, text=t, cluster=False, pad=pad, link=link, tip=g.get("title"), shadow=shadowed, chip=chip,
                              classes=[c for c in cls if c not in ("node", "rough-node", "default", "clickable", "icon-shape", "image-shape")],
                              images=[i for i in measured_all.get("images", []) if i["key"] == g.get("id")]))
    by_key = {b["key"]: b for b in boxes}
    for k, (master, scale) in visio_icons.items():
        if k in by_key:
            by_key[k]["stencil"] = (master, scale)

    labels = {}
    for k in measured:
        t = text_of(k)
        if k.startswith("L_") and t and t["runs"]:
            bx_, by_, bw_, bh_ = t["box"]
            labels[k] = dict(t, pos=(bx_ + bw_ / 2, by_ + bh_ / 2), wh=(bw_, bh_))


    def marker_kind(url):
        m = re.search(r"(point|circle|cross)(Start|End)", url or "")
        return ARROWS[m.group(1)] if m else 0


    edges = []
    for p in svg.iter(SVGNS + "path"):
        cls = (p.get("class") or "").split()
        if "flowchart-link" not in cls or "edge-thickness-invisible" in cls or not p.get("data-points"):
            continue
        eid = p.get("data-id")
        src_k, tgt_k = measured_all["edges"].get(eid, (None, None))
        assert src_k in by_key and tgt_k in by_key, f"cannot resolve edge {eid}"
        m = ctm(p)
        qs = re.findall(r"L([-\d.]+),([-\d.]+)Q([-\d.]+),([-\d.]+)", p.get("d"))   # rounded-corner radius
        radius = min((abs(float(a) - float(c)) + abs(float(b) - float(d))) for a, b, c, d in qs) if qs else 0
        thick = next((c.split("-")[-1] for c in cls if c.startswith("edge-thickness-")), "normal")
        pattern = PATTERNS.get(next((c.split("-")[-1] for c in cls if c.startswith("edge-pattern-")), "solid"), 1)
        if any(c.startswith("edge-animation-") for c in cls):   # animated edges are moving dashes; static = dashed
            pattern = 2
        # Mermaid always writes its own inline dasharray first (a draw-in animation trick); linkStyle ones follow it
        dashes = [d.split(":", 1)[1].strip() for d in (p.get("style") or "").split(";") if d.strip().startswith("stroke-dasharray")]
        custom = dashes[1:]
        if custom:
            nums = [float(v) for v in re.findall(r"[\d.]+", custom[-1])]
            pattern = 3 if nums and max(nums) <= 3 else 2
        c = computed.get(p, {})
        route = [apply(m, d["x"], d["y"]) for d in json.loads(base64.b64decode(p.get("data-points")))]
        if re.search(r"[CcSs]", p.get("d")):   # a real curve (basis, natural, ...): data-points are only its control
            curve = [apply(m, *q) for sub, _ in flatten_path(p.get("d")) for q in sub]   # points, so trace the drawn
            route = route[:1] + curve[1:-1] + route[-1:]   # path; Mermaid's own end points stay the glue points
        edges.append(dict(id=eid, src=src_k, tgt=tgt_k, label=labels.get(eid), radius=radius,
                          width=c.get("sw") or EDGE_W.get(thick, EDGE_W["normal"]), pattern=pattern,
                          color=hexcolor(c.get("stroke")) or EDGE_C,
                          pts=route,
                          end=marker_kind(p.get("marker-end")), beg=marker_kind(p.get("marker-start"))))


    def attach(b, pt):
        """Add a connection point on box b at (clamped) svg point pt; return its row index and the point."""
        fx = min(max((pt[0] - b["x"]) / b["w"], 0), 1) if b["w"] else 0.5
        fy = min(max((pt[1] - b["y"]) / b["h"], 0), 1) if b["h"] else 0.5
        b.setdefault("conns", []).append((fx, fy))
        return len(b["conns"]) - 1, (b["x"] + fx * b["w"], b["y"] + fy * b["h"])


    def onto_stencil(e):
        """Mermaid counts a stencil node's caption as part of the node, so a sideways edge meets the node in the
        middle of its text. Move such ends onto the nearest side of the stencil shape (a right-angled jog) and
        recentre the edge label on the new route."""
        boxes_ = [by_key[k]["images"][0]["box"] if by_key[k].get("stencil") and by_key[k].get("images") else None
                  for k in (e["src"], e["tgt"])]
        on = lambda p, r: r[0] - 1 <= p[0] <= r[0] + r[2] + 1 and r[1] - 1 <= p[1] <= r[1] + r[3] + 1
        # only ends on the node's left/right side: a top/bottom end lies beyond the caption, clear of its text
        sideways = lambda p, n: min(abs(p[0] - n["x"]), abs(p[0] - n["x"] - n["w"])) < \
            min(abs(p[1] - n["y"]), abs(p[1] - n["y"] - n["h"]))
        move = [r is not None and not on(pt, r) and sideways(pt, by_key[k])
                for r, pt, k in zip(boxes_, (e["pts"][0], e["pts"][-1]), (e["src"], e["tgt"]))]
        if not any(move):
            return
        pts = e["pts"]
        straight = all(abs((b[0] - a[0]) * (pts[-1][1] - pts[0][1]) - (b[1] - a[1]) * (pts[-1][0] - pts[0][0])) < 1
                       for a, b in zip(pts, pts[1:]))
        if straight:
            pts = [pts[0], pts[-1]]

        def side(r, end, node):   # the shape's side matching the node side Mermaid ended on: (point, horizontal?)
            cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
            gaps = {"l": abs(end[0] - node["x"]), "r": abs(end[0] - node["x"] - node["w"]),
                    "t": abs(end[1] - node["y"]), "b": abs(end[1] - node["y"] - node["h"])}
            k = min(gaps, key=gaps.get)
            return {"l": ((r[0], cy), True), "r": ((r[0] + r[2], cy), True),
                    "t": ((cx, r[1]), False), "b": ((cx, r[1] + r[3]), False)}[k]

        if straight and all(move):   # both ends: a direct elbow between the two shapes
            (a, ha), (b, _) = side(boxes_[0], pts[0], by_key[e["src"]]), side(boxes_[1], pts[-1], by_key[e["tgt"]])
            mid = ((a[0] + b[0]) / 2, a[1]) if ha else (a[0], (a[1] + b[1]) / 2)
            pts = [a, mid, (mid[0], b[1]) if ha else (b[0], mid[1]), b]
        else:
            for i, r in ((0, boxes_[0]), (-1, boxes_[1])):
                if not move[i]:
                    continue
                q = pts[1] if i == 0 else pts[-2]
                (n, horiz) = side(r, pts[i], by_key[e["src"] if i == 0 else e["tgt"]])
                knee = [((q[0] + n[0]) / 2, q[1]), ((q[0] + n[0]) / 2, n[1])] if horiz else \
                       [(q[0], (q[1] + n[1]) / 2), (n[0], (q[1] + n[1]) / 2)]
                pts = ([n] + knee[::-1] + pts[1:]) if i == 0 else (pts[:-1] + knee + [n])
        e["pts"] = pts
        if e["label"]:   # the label sits at the middle of the new route, as Mermaid places it on its own
            seg = [(math.dist(a, b), a, b) for a, b in zip(pts, pts[1:])]
            half, run = sum(l for l, _, _ in seg) / 2, 0.0
            for l, a, b in seg:
                if run + l >= half and l:
                    t = (half - run) / l
                    e["label"]["pos"] = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                    break
                run += l

    # ---- keep connectors off text: cluster titles, stencil captions and other edges' labels ----
    def hits(a, b, r, shrink=1.0):
        """Does the axis-aligned segment a-b run through the interior of rectangle r (x, y, w, h)?"""
        x0, y0, w, h = r[0] + shrink, r[1] + shrink, r[2] - 2 * shrink, r[3] - 2 * shrink
        if w <= 0 or h <= 0:
            return False
        if abs(a[0] - b[0]) < 0.5:
            return x0 < a[0] < x0 + w and min(a[1], b[1]) < y0 + h and max(a[1], b[1]) > y0
        if abs(a[1] - b[1]) < 0.5:
            return y0 < a[1] < y0 + h and min(a[0], b[0]) < x0 + w and max(a[0], b[0]) > x0
        return False

    obstacles = [(b["key"], b["text"]["box"]) for b in boxes
                 if b["text"] and b["text"]["box"] and (b["cluster"] or b.get("stencil"))]
    obstacles += [(e["id"], (e["label"]["pos"][0] - e["label"]["wh"][0] / 2, e["label"]["pos"][1] - e["label"]["wh"][1] / 2,
                             e["label"]["wh"][0], e["label"]["wh"][1])) for e in edges if e["label"]]

    def avoid_text(e):
        """Move route segments that cross text to just beside it. Middle segments shift as a whole; a first or last
        segment (its end is glued) gets a short jog instead. Curved routes are left as Mermaid drew them."""
        rects = [r for owner, r in obstacles if owner not in (e["src"], e["tgt"], e["id"])]
        def merged(q):   # Mermaid adds mid-points along straight runs; a run is one segment for routing purposes
            out = [q[0]]
            for u, v in zip(q[1:], q[2:] + [None]):
                w = out[-1]
                if v is not None and ((abs(w[0] - u[0]) < 0.5 and abs(u[0] - v[0]) < 0.5) or
                                      (abs(w[1] - u[1]) < 0.5 and abs(u[1] - v[1]) < 0.5)):
                    continue
                out.append(u)
            return out

        orig = merged(list(e["pts"]))
        pts, m = list(orig), 6.0
        for _ in range(40):
            bad = next(((i, r) for i in range(len(pts) - 1) for r in rects if hits(pts[i], pts[i + 1], r)), None)
            if not bad:
                break
            i, r = bad
            a, b, n = pts[i], pts[i + 1], len(pts)
            ax = 0 if abs(a[0] - b[0]) < 0.5 else 1      # coordinate that moves (x for a vertical segment)
            o = 1 - ax
            lo, hi = (r[0], r[0] + r[2]) if ax == 0 else (r[1], r[1] + r[3])
            rlo, rhi = (r[1], r[1] + r[3]) if ax == 0 else (r[0], r[0] + r[2])
            P = lambda u, v: (u, v) if ax == 0 else (v, u)
            ja, jb = (rlo - m, rhi + m) if a[o] < b[o] else (rhi + m, rlo - m)
            options = []
            for c in sorted((lo - m, hi + m), key=lambda c: abs(c - a[ax])):
                prev, nxt = pts[i - 1] if i > 0 else None, pts[i + 2] if i + 2 < n else None
                flips = any(q is not None and abs(q[o] - ref[o]) < 0.5 and (c - q[ax]) * (ref[ax] - q[ax]) <= 0
                            for q, ref in ((prev, a), (nxt, b)))
                if 0 < i and i + 1 < n - 1 and not flips:
                    q = pts[:i] + [P(c, a[o]), P(c, b[o])] + pts[i + 2:]
                elif i == 0 and n > 2 and not flips:
                    q = [a, P(a[ax], ja), P(c, ja), P(c, b[o])] + pts[2:]
                elif i + 1 == n - 1 and n > 2 and not flips:
                    q = pts[:i] + [P(c, a[o]), P(c, jb), P(b[ax], jb), b]
                else:
                    q = pts[:i] + [a, P(a[ax], ja), P(c, ja), P(c, jb), P(a[ax], jb), b] + pts[i + 2:]
                options.append((sum(hits(u, v, rr) for u, v in zip(q, q[1:]) for rr in rects), q))
            pts = min(options, key=lambda t: t[0])[1]   # fewest remaining crossings; the nearer side on a tie
        if pts != orig:
            e["pts"] = pts
            if e["label"]:   # keep the label on the route: nearest point of the new one
                lx, ly = e["label"]["pos"]
                best = None
                for u, v in zip(pts, pts[1:]):
                    dx, dy = v[0] - u[0], v[1] - u[1]
                    t = max(0, min(1, ((lx - u[0]) * dx + (ly - u[1]) * dy) / (dx * dx + dy * dy))) if dx or dy else 0
                    q = (u[0] + t * dx, u[1] + t * dy)
                    if best is None or math.dist(q, (lx, ly)) < best[0]:
                        best = (math.dist(q, (lx, ly)), q)
                e["label"]["pos"] = best[1]

    for e in edges:
        onto_stencil(e)
        avoid_text(e)
        e["ia"], e["pts"][0] = attach(by_key[e["src"]], e["pts"][0])
        e["ib"], e["pts"][-1] = attach(by_key[e["tgt"]], e["pts"][-1])

    # ---------- emit ----------
    with ZipFile(TEMPLATE) as z:
        files = {n: z.read(n) for n in z.namelist()}
    page = ET.fromstring(files["visio/pages/page1.xml"])
    shapes = page.find(Q("Shapes"))
    shapes.clear()
    connects = page.find(Q("Connects"))
    if connects is None:
        connects = ET.SubElement(page, Q("Connects"))
    connects.clear()
    nid = 0


    def cell(p, n, v, f=None):
        a = {"N": n, "V": str(v)}
        if f:
            a["F"] = f
        return ET.SubElement(p, Q("Cell"), a)


    def text_sections(s, t, align):
        # One Character row per run, in text order (some readers, libvisio, assign rows positionally). Every row
        # spells out all its cells, as Visio writes them: only row 0 can inherit from the text style, so a row 1 that
        # leaves FontScale out gets 0% in Visio and its characters collapse onto each other.
        sec = ET.SubElement(s, Q("Section"), {"N": "Character"})
        for ix, (_, key) in enumerate(t["runs"]):
            bits, color, size, font = (str(key).split("|") + [None] * 3)[:4]   # per-run style, label's as fallback
            st = int(bits)
            r = ET.SubElement(sec, Q("Row"), {"IX": str(ix)})
            for n, v in (("Font", font_name(font) if font else t["font"]), ("Color", hexcolor(color) or t["color"] or "#000000"),
                         ("Style", st & 7), ("Case", 0), ("Pos", 0), ("FontScale", 1),
                         ("Size", float(size or t["size"]) / S),   # px -> inches (14px = 10.5pt)
                         ("DblUnderline", 0), ("Overline", 0), ("Strikethru", int(bool(st & 16))), ("DoubleStrikethrough", 0),
                         ("Letterspace", 0), ("ColorTrans", 0), ("AsianFont", 0), ("ComplexScriptFont", 0),
                         ("ComplexScriptSize", -1), ("LangID", "en-US")):
                cell(r, n, v)
        r = ET.SubElement(ET.SubElement(s, Q("Section"), {"N": "Paragraph"}), Q("Row"), {"IX": "0"})
        for n, v in (("IndFirst", 0), ("IndLeft", 0), ("IndRight", 0),
                     ("SpLine", t["lh"] / S),   # positive = absolute line pitch, same as CSS line-height
                     ("SpBefore", 0), ("SpAfter", 0), ("HorzAlign", align), ("Bullet", 0), ("BulletStr", ""),
                     ("BulletFont", 0), ("BulletFontSize", -1), ("TextPosAfterBullet", 0), ("Flags", 0)):
            cell(r, n, v)


    def text_body(s, runs):
        tx = ET.SubElement(s, Q("Text"))
        for ix, (txt, _) in enumerate(runs):
            ET.SubElement(tx, Q("cp"), {"IX": str(ix)}).tail = txt


    def geometry(s, b, w, h, parts):
        """One Geometry section per outline piece, in Width/Height-relative coordinates (of b's box) so it scales."""
        fx = lambda x: (x - b["x"]) / b["w"] if b["w"] else 0
        fy = lambda y: 1 - (y - b["y"]) / b["h"] if b["h"] else 0
        for ix, p in enumerate(parts):
            g = ET.SubElement(s, Q("Section"), {"N": "Geometry", "IX": str(ix)})
            cell(g, "NoFill", 0 if p["fill"] else 1)
            cell(g, "NoLine", 0 if p["stroke"] else 1)
            if p["kind"] == "ellipse":   # MoveTo + two EllipticalArcTo halves: portable, unlike an Ellipse row
                (cx, cy), (rx, ry) = p["c"], p["r"]
                kx, ky = rx / b["w"], ry / b["h"]
                ratio = (f"(Width*{kx:.6g})/(Height*{ky:.6g})", rx / ry)
                rows = [("MoveTo", (cx - rx, cy), None), ("EllipticalArcTo", (cx + rx, cy), (cx, cy - ry)),
                        ("EllipticalArcTo", (cx - rx, cy), (cx, cy + ry))]
                for i, (t, (ex, ey), ctrl) in enumerate(rows, 1):
                    r = ET.SubElement(g, Q("Row"), {"T": t, "IX": str(i)})
                    cell(r, "X", fx(ex) * w, f"Width*{fx(ex):.6g}")
                    cell(r, "Y", fy(ey) * h, f"Height*{fy(ey):.6g}")
                    if ctrl:
                        cell(r, "A", fx(ctrl[0]) * w, f"Width*{fx(ctrl[0]):.6g}")
                        cell(r, "B", fy(ctrl[1]) * h, f"Height*{fy(ctrl[1]):.6g}")
                        cell(r, "C", 0)
                        cell(r, "D", ratio[1], ratio[0])
                continue
            closed = p["closed"] or p["fill"]   # Visio only fills closed geometry
            pts = p["pts"] + ([p["pts"][0]] if closed and p["pts"][0] != p["pts"][-1] else [])
            for i, (x, y) in enumerate(pts, 1):
                r = ET.SubElement(g, Q("Row"), {"T": "MoveTo" if i == 1 else "LineTo", "IX": str(i)})
                cell(r, "X", fx(x) * w, f"Width*{fx(x):.6g}")
                cell(r, "Y", fy(y) * h, f"Height*{fy(y):.6g}")


    ordered = sorted(boxes, key=lambda b: not b["cluster"])   # clusters behind nodes (document order keeps nesting)
    for b in ordered:
        nid += 1
        b["id"] = nid


    def inside(inner, outer, tol=1.0):
        return (outer["x"] - tol <= inner["x"] and outer["y"] - tol <= inner["y"] and
                inner["x"] + inner["w"] <= outer["x"] + outer["w"] + tol and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + tol)


    # subgraph membership: each box belongs to the smallest cluster that encloses it (nested subgraphs too)
    for b in boxes:
        hosts = [c for c in boxes if c["cluster"] and c is not b and inside(b, c)]
        b["container"] = min(hosts, key=lambda c: c["w"] * c["h"]) if hosts else None
    members = {}
    for b in boxes:
        if b["container"]:
            members.setdefault(b["container"]["key"], []).append(b)

    rels = []   # extra page relationships (pictures); spliced in as text to keep the part's default namespace
    media = 0

    def style_runs(parts):
        """Split outline pieces into consecutive runs sharing one stroke (a Visio shape has a single line style).
        Unstroked fills join any run. handDrawn nodes, e.g., draw a hachure fill in one colour, then the outline."""
        runs, key = [], "unset"
        for p in parts:
            k = (p["stroke"], p["sw"]) if p["stroke"] else None
            if runs and (k is None or k == key or key is None):
                runs[-1].append(p)
                key = k if key is None else key
            else:
                runs.append([p])
                key = k
        return runs

    def paint_cells(sh, b, parts):
        fill = next((p["fill"] for p in parts if p["fill"]), None)
        stroked = [p for p in parts if p["stroke"]]
        cells = [("LineColor", stroked[0]["stroke"] if stroked else "#000000"),
                 ("LineWeight", (stroked[0]["sw"] if stroked else 1) / S),
                 ("LinePattern", (2 if b["dash"] else 1) if stroked else 0),
                 ("FillForegnd", fill or "#FFFFFF"), ("FillPattern", 1 if fill else 0),
                 ("Rounding", max((p.get("rx", 0) for p in parts), default=0) / S)]
        if shadow and fill and b.get("shadow"):   # neo look: flat offset shadow where Mermaid draws one
            dx, dy, op, col = shadow
            rgb = hexcolor(col) or "#000000"   # pre-blend the faint shadow onto white: not every reader honours
            flat = hexcolor(f"rgba({int(rgb[1:3], 16)}, {int(rgb[3:5], 16)}, {int(rgb[5:7], 16)}, {op})")   # transparency
            cells += [("ShdwPattern", 1), ("ShdwForegnd", flat), ("ShapeShdwType", 1),
                      ("ShapeShdwOffsetX", dx / S), ("ShapeShdwOffsetY", -dy / S), ("ShapeShdwBlur", 0)]
        for n, v in cells:
            cell(sh, n, v)

    def child(kids, name, kind="Shape"):
        """A member of b's group that covers the whole node and scales with it."""
        nonlocal nid
        nid += 1
        k = ET.SubElement(kids, Q("Shape"), {"ID": str(nid), "NameU": name, "Type": kind,
                                             "LineStyle": "0", "FillStyle": "0", "TextStyle": "0"})
        return k

    importer = None

    def next_id():
        nonlocal nid
        nid += 1
        return nid

    def picture(kids, im, b=None):
        """A captured picture as a Foreign (PNG) shape: a member of node b's group (follows and scales with it),
        or a free shape on the page when b is None (pictures in edge labels)."""
        nonlocal media
        media += 1
        ix, iy, iw, ih = im["box"]
        files[f"visio/media/image{media}.png"] = base64.b64decode(im["png"])
        rid = f"rIdImg{media}"
        rels.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="../media/image{media}.png"/>')
        k = child(kids, f"{b['key'] if b else im['key']} image", "Foreign")
        kw, kh = iw / S, ih / S
        if b:
            pos = (("PinX", (ix - b["x"]) / S, f"Sheet.{b['id']}!Width*{(ix - b['x']) / b['w']:.6g}"),
                   ("PinY", (b["y"] + b["h"] - ih - iy) / S, f"Sheet.{b['id']}!Height*{(b['y'] + b['h'] - ih - iy) / b['h']:.6g}"),
                   ("Width", kw, f"Sheet.{b['id']}!Width*{iw / b['w']:.6g}"),
                   ("Height", kh, f"Sheet.{b['id']}!Height*{ih / b['h']:.6g}"))
        else:
            pos = (("PinX", X(ix), None), ("PinY", Y(iy + ih), None), ("Width", kw, None), ("Height", kh, None))
        bg = None if b else hexcolor((measured.get(im["key"]) or {}).get("bg"))   # edge label: keep its background
        for n, v, f in pos + (("LocPinX", 0, None), ("LocPinY", 0, None), ("ImgOffsetX", 0, None), ("ImgOffsetY", 0, None),
                              ("ImgWidth", kw, "Width*1"), ("ImgHeight", kh, "Height*1"),
                              ("LinePattern", 0, None), ("FillPattern", 1 if bg else 0, None), ("FillForegnd", bg or "#FFFFFF", None)):
            cell(k, n, v, f)
        g = ET.SubElement(k, Q("Section"), {"N": "Geometry", "IX": "0"})
        cell(g, "NoFill", 0 if bg else 1)
        cell(g, "NoLine", 1)
        for i, (fx_, fy_) in enumerate(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)), 1):
            r = ET.SubElement(g, Q("Row"), {"T": "MoveTo" if i == 1 else "LineTo", "IX": str(i)})
            cell(r, "X", fx_ * kw, f"Width*{fx_}")
            cell(r, "Y", fy_ * kh, f"Height*{fy_}")
        fd = ET.SubElement(k, Q("ForeignData"), {"ForeignType": "Bitmap", "CompressionType": "PNG"})
        ET.SubElement(fd, Q("Rel"), {"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": rid})

    for b in ordered:
        w, h = b["w"] / S, b["h"] / S
        runs = style_runs(b["parts"])
        stencil = b.get("stencil") and b.get("images")
        grouped = not stencil and (len(runs) > 1 or bool(b.get("images")))
        # local frame of the Visio shape (x, y, w, h in SVG px): the node's box, or a stencil master's picture box
        fr = b["images"][0]["box"] if stencil else (b["x"], b["y"], b["w"], b["h"])
        if stencil:   # the node IS a stencil master instance (not nested: libvisio can't resolve nested instances)
            importer = importer or __import__("mmd2vsdx.stencil_lib", fromlist=["Importer"]).Importer(files)
            s = importer.place(shapes, b["stencil"][0], next_id, X(fr[0]), Y(fr[1] + fr[3]), b["stencil"][1],
                               name=b["key"], shape_id=b["id"], blank_text=bool(b["text"] and b["text"]["runs"]))
            pl = pt = pr = pb = 0
            if b["text"] and b["text"]["box"] and not b["text"]["angle"]:   # label where Mermaid put it
                lx, ly, lw_, lh_ = b["text"]["box"]
                for n, v in (("TxtPinX", (lx + lw_ / 2 - fr[0]) / S), ("TxtPinY", (fr[1] + fr[3] - ly - lh_ / 2) / S),
                             ("TxtWidth", (lw_ + 2) / S), ("TxtHeight", lh_ / S), ("TxtLocPinX", (lw_ + 2) / S / 2),
                             ("TxtLocPinY", lh_ / S / 2)):
                    for old in [c for c in s.findall(Q("Cell")) if c.get("N") == n]:   # replaces the scaled master's
                        s.remove(old)
                    cell(s, n, v)
            for n, v in (("LeftMargin", 0), ("RightMargin", 0), ("TopMargin", 0), ("BottomMargin", 0), ("VerticalAlign", 1)):
                cell(s, n, v)
        else:
            s = ET.SubElement(shapes, Q("Shape"), {"ID": str(b["id"]), "NameU": b["key"], "Type": "Group" if grouped else "Shape",
                                                  "LineStyle": "0", "FillStyle": "0", "TextStyle": "0"})
            pl, pt, pr, pb = (0, 0, 0, 0) if b["text"] and b["text"]["angle"] else b["pad"]
            for n, v in (("PinX", X(b["x"])), ("PinY", Y(b["y"] + b["h"])), ("Width", w), ("Height", h),
                         ("LocPinX", 0), ("LocPinY", 0), ("Angle", 0),
                         ("LeftMargin", pl / S), ("RightMargin", pr / S), ("TopMargin", pt / S), ("BottomMargin", pb / S),
                         ("VerticalAlign", 0 if b["cluster"] else 1)):
                cell(s, n, v)
            if grouped:   # outline lives in the members; the group keeps text, glue points and data
                for n, v in (("LinePattern", 0), ("FillPattern", 0)):
                    cell(s, n, v)
            else:
                paint_cells(s, b, b["parts"])
        # Visio containers: the frame lists its members (DEPENDSON 1), each member names its container (DEPENDSON 4)
        rel = []
        if members.get(b["key"]):
            rel.append("DEPENDSON(1," + ",".join(f"Sheet.{m['id']}!SheetRef()" for m in members[b["key"]]) + ")")
        if b["container"]:
            rel.append(f"DEPENDSON(4,Sheet.{b['container']['id']}!SheetRef())")
        if rel:
            cell(s, "Relationships", 0, "SUM(" + ",".join(rel) + ")")
        if b["cluster"]:
            row = ET.SubElement(ET.SubElement(s, Q("Section"), {"N": "User"}), Q("Row"), {"N": "msvStructureType"})
            ET.SubElement(row, Q("Cell"), {"N": "Value", "V": "Container", "U": "STR", "F": '"Container"'})
        if b["text"] and b["text"]["angle"]:   # rotated label (swimlane titles): own text block, turned
            (lw, lh), (bx_, by_, bw_, bh_) = b["text"]["local"], b["text"]["box"]
            for n, v in (("TxtPinX", (bx_ + bw_ / 2 - b["x"]) / S), ("TxtPinY", (b["y"] + b["h"] - by_ - bh_ / 2) / S),
                         ("TxtWidth", (lw + 2) / S), ("TxtHeight", lh / S), ("TxtLocPinX", (lw + 2) / S / 2),
                         ("TxtLocPinY", lh / S / 2), ("TxtAngle", math.radians(-b["text"]["angle"]))):
                cell(s, n, v)
        if b["text"]:
            if b.get("chip"):   # the label chip under image/icon nodes
                cell(s, "TextBkgnd", b["text"]["bg"])
            text_sections(s, b["text"], b["text"]["align"] if b["cluster"] else 1)   # titles: as Mermaid aligns them
        if b.get("conns"):
            sec = ET.SubElement(s, Q("Section"), {"N": "Connection"})
            for i, (fx, fy) in enumerate(b["conns"]):   # node-box fractions -> the shape's own frame
                lx_ = (b["x"] + fx * b["w"] - fr[0]) / fr[2] if fr[2] else 0
                ly_ = (fr[1] + fr[3] - b["y"] - fy * b["h"]) / fr[3] if fr[3] else 0
                r = ET.SubElement(sec, Q("Row"), {"IX": str(i)})
                cell(r, "X", lx_ * fr[2] / S, f"Width*{lx_:.6g}")
                cell(r, "Y", ly_ * fr[3] / S, f"Height*{ly_:.6g}")
                for n in ("DirX", "DirY", "Type", "AutoGen"):
                    cell(r, n, 0)
        if not b["cluster"]:   # Mermaid id and classes as Shape Data; click link and tooltip
            sec = ET.SubElement(s, Q("Section"), {"N": "Property"})
            for n, label, v in (("MermaidId", "Mermaid ID", b["key"]), ("MermaidClasses", "Mermaid classes", " ".join(b["classes"]))):
                if v:
                    r = ET.SubElement(sec, Q("Row"), {"N": n})
                    ET.SubElement(r, Q("Cell"), {"N": "Value", "V": v, "U": "STR"})
                    cell(r, "Label", label)
            if b["link"]:
                r = ET.SubElement(ET.SubElement(s, Q("Section"), {"N": "Hyperlink"}), Q("Row"), {"N": "Row_1"})
                cell(r, "Address", b["link"])
                cell(r, "Description", b["tip"] or b["link"])
            if b["tip"]:
                cell(s, "Comment", b["tip"])   # Visio shows it as the shape's screen tip
        if not grouped and not stencil:
            geometry(s, b, w, h, b["parts"])
        if b["text"]:
            text_body(s, b["text"]["runs"])
        if stencil:   # the instance's sub-shape list must come last in a Shape
            sub = s.find(Q("Shapes"))
            if sub is not None:
                s.remove(sub)
                s.append(sub)
        if not grouped:
            continue
        kids = ET.SubElement(s, Q("Shapes"))
        for i, run in enumerate(runs, 1):   # in Mermaid's drawing order
            k = child(kids, f"{b['key']} outline {i}")
            for n, v, f in (("PinX", 0, None), ("PinY", 0, None), ("Width", w, f"Sheet.{b['id']}!Width"),
                            ("Height", h, f"Sheet.{b['id']}!Height"), ("LocPinX", 0, None), ("LocPinY", 0, None)):
                cell(k, n, v, f)
            paint_cells(k, b, run)
            geometry(k, b, w, h, run)
        for im in b.get("images", []):   # pictures/icons: Foreign members, so they move and scale with the node
            picture(kids, im, b)

    title = text_of("__title__")
    if title and title["runs"]:
        nid += 1
        tx_, ty_, tw_, th_ = title["box"]
        s = ET.SubElement(shapes, Q("Shape"), {"ID": str(nid), "NameU": "Title", "Type": "Shape",
                                              "LineStyle": "0", "FillStyle": "0", "TextStyle": "0"})
        for n, v in (("PinX", X(tx_)), ("PinY", Y(ty_ + th_)), ("Width", (tw_ + 4) / S), ("Height", th_ / S),
                     ("LocPinX", 0), ("LocPinY", 0), ("LinePattern", 0), ("FillPattern", 0),
                     ("LeftMargin", 0), ("RightMargin", 0), ("TopMargin", 0), ("BottomMargin", 0), ("VerticalAlign", 1)):
            cell(s, n, v)
        text_sections(s, title, 1)
        text_body(s, title["runs"])

    for e in edges:
        nid += 1
        a, b = by_key[e["src"]], by_key[e["tgt"]]
        (bx, by), (ex, ey) = e["pts"][0], e["pts"][-1]
        BX, BY, EX, EY = X(bx), Y(by), X(ex), Y(ey)
        s = ET.SubElement(shapes, Q("Shape"), {"ID": str(nid), "NameU": f"{e['src']} to {e['tgt']}",
                                              "Type": "Shape", "Master": DYN_MASTER})
        glue = lambda o, i: f"PAR(PNT(Sheet.{o['id']}!Connections.X{i + 1},Sheet.{o['id']}!Connections.Y{i + 1}))"
        for n, v, f in (("PinX", (BX + EX) / 2, None), ("PinY", (BY + EY) / 2, None),
                        ("Width", EX - BX, None), ("Height", EY - BY, None),
                        ("LocPinX", (EX - BX) / 2, None), ("LocPinY", (EY - BY) / 2, None),
                        ("BeginX", BX, glue(a, e["ia"])), ("BeginY", BY, glue(a, e["ia"])),
                        ("EndX", EX, glue(b, e["ib"])), ("EndY", EY, glue(b, e["ib"])),
                        # what Visio itself writes on glued ends: recompute them when the attached shape moves
                        ("BegTrigger", 2, f"_XFTRIGGER(Sheet.{a['id']}!EventXFMod)"),
                        ("EndTrigger", 2, f"_XFTRIGGER(Sheet.{b['id']}!EventXFMod)"),
                        ("LineColor", e["color"], None), ("LineWeight", e["width"] / S, None),
                        ("LinePattern", e["pattern"], None), ("Rounding", e["radius"] / S, None),
                        ("BeginArrow", e["beg"], None), ("EndArrow", e["end"], None),
                        ("ConFixedCode", REROUTE[reroute], None),
                        # Visio grows arrowheads with line weight; Mermaid's stay ~14px, so go a size down on thick
                        # edges (unverified: libvisio ignores ArrowSize, so only Visio itself can confirm)
                        ("BeginArrowSize", 2 if e["width"] <= 2 else 1, None), ("EndArrowSize", 2 if e["width"] <= 2 else 1, None)):
            cell(s, n, v, f)
        lab = e["label"]
        if lab:
            (lx, ly), (lw, lh) = lab["pos"], lab["wh"]
            cell(s, "TextBkgnd", lab["bg"] or "#FFFFFF")
            # cached values for readers that don't evaluate the master's text formulas (formulas stay inherited)
            tw, th = (lw + 2) / S, lh / S
            for n, v in (("TxtWidth", tw), ("TxtHeight", th), ("TxtLocPinX", tw / 2), ("TxtLocPinY", th / 2),
                         ("TxtPinX", X(lx) - BX), ("TxtPinY", Y(ly) - BY),   # local origin = Begin point
                         ("LeftMargin", 0), ("RightMargin", 0), ("TopMargin", 0), ("BottomMargin", 0)):
                cell(s, n, v)
            ctl = ET.SubElement(ET.SubElement(s, Q("Section"), {"N": "Control"}), Q("Row"), {"N": "TextPosition"})
            cell(ctl, "X", X(lx) - BX)
            cell(ctl, "Y", Y(ly) - BY)
            text_sections(s, lab, 1)
        geo = ET.SubElement(s, Q("Section"), {"N": "Geometry", "IX": "0"})
        # cover every route row the master defines, else its extra point leaks in; repeating the end
        # point (zero-length segment) works in every reader, unlike a Del="1" row, which some readers reject
        route = e["pts"] + [e["pts"][-1]] * max(0, MASTER_GEO_ROWS - len(e["pts"]))
        for i, (ppx, ppy) in enumerate(route, 1):
            r = ET.SubElement(geo, Q("Row"), {"T": "MoveTo" if i == 1 else "LineTo", "IX": str(i)})
            cell(r, "X", X(ppx) - BX)
            cell(r, "Y", Y(ppy) - BY)
        if lab:
            text_body(s, lab["runs"])
        for cellname, part, o, i in (("BeginX", 9, a, e["ia"]), ("EndX", 12, b, e["ib"])):
            ET.SubElement(connects, Q("Connect"), {"FromSheet": str(nid), "FromCell": cellname, "FromPart": str(part),
                                                   "ToSheet": str(o["id"]), "ToCell": f"Connections.X{i + 1}",
                                                   "ToPart": str(100 + i)})

    edge_ids = {e["id"] for e in edges}
    for im in measured_all.get("images", []):   # pictures in edge labels: free shapes on top (connectors can't group)
        if im["key"] in edge_ids:
            picture(shapes, im)
    if importer:
        importer.finish()
    files["visio/pages/page1.xml"] = ET.tostring(page, encoding="utf-8", xml_declaration=True)
    if media:
        files["visio/pages/_rels/page1.xml.rels"] = files["visio/pages/_rels/page1.xml.rels"].replace(
            b"</Relationships>", "".join(rels).encode() + b"</Relationships>")
        ct = files["[Content_Types].xml"].decode()
        if 'Extension="png"' not in ct:
            files["[Content_Types].xml"] = ct.replace("<Default ", '<Default Extension="png" ContentType="image/png"/><Default ', 1).encode()
    pages = ET.fromstring(files["visio/pages/pages.xml"])
    pg = pages.find(Q("Page"))
    pg.set("NameU", "Diagram")
    pg.set("Name", "Diagram")
    pg.set("ViewCenterX", str(PW / 2))
    pg.set("ViewCenterY", str(PH / 2))
    sheet = pg.find(Q("PageSheet"))
    for n, v in (("PageWidth", PW), ("PageHeight", PH)):
        sheet.find(f"{Q('Cell')}[@N='{n}']").set("V", str(v))
    files["visio/pages/pages.xml"] = ET.tostring(pages, encoding="utf-8", xml_declaration=True)
    # document properties: accTitle/accDescr (or the front-matter title); not the template author's name
    meta = lambda tag: html.escape(" ".join((svg.findtext(SVGNS + tag) or "").split()))
    doc_title = meta("title") or (html.escape(" ".join(t for t, _ in title["runs"])) if title and title["runs"] else "")
    core = files["docProps/core.xml"].decode()
    for tag, v in (("dc:title", doc_title), ("dc:description", meta("desc")), ("dc:creator", "")):
        core = re.sub(f"<{tag}>.*?</{tag}>", lambda _: f"<{tag}>{v}</{tag}>", core)
    files["docProps/core.xml"] = core.encode()
    files["docProps/app.xml"] = files["docProps/app.xml"].replace(b"<vt:lpstr>Page-1</vt:lpstr>", b"<vt:lpstr>Diagram</vt:lpstr>")
    with ZipFile(out_path, "w", ZIP_DEFLATED) as z:
        for n, c in files.items():
            z.writestr(n, c)
    print(f"{out_path}: {len(boxes)} boxes, {len(edges)} glued connectors, page {PW:.2f} x {PH:.2f} in"
          + (f"; reference {png_path}" if png_path else ""))


def mermaid_blocks(md_path):
    """Every ```mermaid fenced block in a Markdown file."""
    text = open(md_path, encoding="utf-8").read()
    return re.findall(r"^[ \t]*(?:```|~~~)[ \t]*mermaid[^\n]*\n(.*?)^[ \t]*(?:```|~~~)", text, re.S | re.M)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Mermaid flowcharts to editable Visio .vsdx, matching Mermaid's render.")
    ap.add_argument("inputs", nargs="*", help=".mmd files, or .md files whose ```mermaid blocks are converted")
    ap.add_argument("-o", "--out", help="output .vsdx (single diagram) or output directory")
    ap.add_argument("--no-png", action="store_true", help="skip writing Mermaid's reference PNG next to each .vsdx")
    ap.add_argument("--config", default=CONFIG, help="Mermaid config JSON (default: bundled Arial config)")
    ap.add_argument("--reroute", choices=list(REROUTE), default="as-needed",
                    help="how Visio may re-route connectors: as-needed (default; keeps Mermaid's route until "
                         "something moves), freely, never")
    ap.add_argument("--wrap", choices=["exact", "visio"], default="exact",
                    help="exact: Mermaid's line breaks as hard breaks (1:1 look); visio: keep only explicit breaks "
                         "so text re-wraps when shapes are resized")
    ap.add_argument("--stencils", default=STENCILS,
                    help="Visio stencil library for visio:NAME icons: a folder of .vssx (default: ./stencils), a .vssx or a .zip")
    ap.add_argument("--find", metavar="TEXT",
                    help="list stencil masters whose name, stencil or keywords contain TEXT, then exit")
    a = ap.parse_args(argv)
    if a.find:
        hits = library(a.stencils).search(a.find)
        for m in hits:
            print(f"visio:{m!r:44} {m.title}")
        print(f"{len(hits)} masters", file=sys.stderr)
        return 0
    if not a.inputs:
        ap.error("no input files")
    jobs = []   # (mmd source path, output stem, directory of the input)
    tmpdir = tempfile.mkdtemp()
    for inp in a.inputs:
        stem = os.path.splitext(os.path.basename(inp))[0]
        srcdir = os.path.dirname(os.path.abspath(inp))
        if inp.lower().endswith((".md", ".markdown")):
            blocks = mermaid_blocks(inp)
            if not blocks:
                print(f"{inp}: no mermaid blocks", file=sys.stderr)
            for i, b in enumerate(blocks, 1):
                p = os.path.join(tmpdir, f"{stem}-{i}.mmd")
                open(p, "w", encoding="utf-8").write(b)
                jobs.append((p, f"{stem}-{i}", srcdir))
        else:
            jobs.append((inp, stem, srcdir))
    single = bool(a.out) and a.out.lower().endswith(".vsdx")
    if single and len(jobs) > 1:
        ap.error("-o names a single .vsdx but there are several diagrams; give a directory instead")
    failed = 0
    for src, stem, srcdir in jobs:
        if single:
            out = a.out
        else:
            outdir = a.out or srcdir
            os.makedirs(outdir, exist_ok=True)
            out = os.path.join(outdir, stem + ".vsdx")
        png = None if a.no_png else os.path.splitext(out)[0] + ".png"
        try:
            convert(src, out, png, a.config, a.reroute, a.wrap, a.stencils)
        except (AssertionError, subprocess.CalledProcessError) as exc:
            failed += 1
            print(f"{stem}: FAILED: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
