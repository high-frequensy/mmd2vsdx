#!/usr/bin/env python3
"""Preview a .vsdx without Visio: libvisio (vsd2raw) parses the file, this draws what it parsed as SVG,
and Chrome rasterises it. No watermark, real fonts. Limits: libvisio ignores connector Rounding and
does not wrap text (fine for mmd2vsdx output, which uses hard line breaks).

Usage: python3 vsdx_preview.py drawing.vsdx [out.png]      (needs libvisio's vsd2raw: brew install libvisio)
"""
import html
import os
import re
import subprocess
import sys
import tempfile

from mmd2vsdx import flatten_path

HERE = os.path.dirname(os.path.realpath(__file__))
MODULES = os.environ.get("MMDC_MODULES", os.path.join(HERE, "node_modules"))
PPTR = os.environ.get("PUPPETEER_CONFIG", os.path.join(HERE, "puppeteer.json"))   # written by setup.sh
PX = 96

def props(s):
    """'a:b: v, c:d: w' -> dict (values may contain spaces, never ', key:')."""
    return {k: v.strip() for k, v in re.findall(r"([\w:-]+): (.*?)(?=, [\w-]+:[\w-]+: |$)", s)}


deco = lambda s: " ".join(d for k, d in (("style:text-underline-type", "underline"), ("style:text-line-through-type", "line-through"))
                          if s.get(k, "none") != "none") or "none"
inch = lambda v: float(v.rstrip("in").rstrip("%")) * PX
def to_svg(src, background=True):
    """SVG of what libvisio parses from src: the whole page, at 96 px per inch."""
    raw = subprocess.run(["vsd2raw", src], capture_output=True, text=True).stdout
    body, defs, style, text = [], [], {}, None
    W = H = 0
    for line in raw.splitlines():
        t = line.strip()
        m = re.match(r"(\w+)\s*\((.*)\)$", t)
        op, arg = (m.group(1), m.group(2)) if m else (t, "")
        if op == "startPage":
            p = props(arg)
            W, H = inch(p["svg:width"]), inch(p["svg:height"])
        elif op == "setStyle":
            style = props(arg)
        elif op == "drawPath":
            d = ""
            for seg in re.findall(r"\(([^()]*path-action[^()]*)\)", arg):   # keys come in any order
                act = re.search(r"path-action: (\w)", seg).group(1)
                v = {k: float(n.rstrip("in")) for k, n in re.findall(r"([\w:-]+): ([-\d.]+(?:in)?)", seg)}
                P = lambda k: v[k] * PX
                if act == "A":
                    d += (f"A{P('svg:rx'):.2f},{P('svg:ry'):.2f} {v.get('librevenge:rotate', 0):.2f} "
                          f"{int(v.get('librevenge:large-arc', 0))},{int(v.get('librevenge:sweep', 1))} "   # librevenge: no sweep = 1
                          f"{P('svg:x'):.2f},{P('svg:y'):.2f} ")
                elif act == "C":
                    d += f"C{P('svg:x1'):.2f},{P('svg:y1'):.2f} {P('svg:x2'):.2f},{P('svg:y2'):.2f} {P('svg:x'):.2f},{P('svg:y'):.2f} "
                elif act == "Q":
                    d += f"Q{P('svg:x1'):.2f},{P('svg:y1'):.2f} {P('svg:x'):.2f},{P('svg:y'):.2f} "
                elif "svg:x" in v:
                    d += f"{act}{P('svg:x'):.2f},{P('svg:y'):.2f} "
                else:
                    d += act
            fill = style.get("draw:fill-color", "none") if style.get("draw:fill") == "solid" else "none"
            if style.get("draw:fill") == "gradient":   # ODF gradient: angle 0 runs top -> bottom, start colour first
                gid, a0, b0 = f"g{len(defs)}", style.get("draw:start-color", "#fff"), style.get("draw:end-color", "#000")
                stops = (a0, b0, a0) if style.get("draw:style") == "axial" else (a0, b0)
                tag = "radialGradient" if style.get("draw:style") in ("radial", "ellipsoid") else "linearGradient"
                geo = "" if tag == "radialGradient" else (f'x1="0" y1="0" x2="0" y2="1" gradientTransform='
                                                          f'"rotate({-float(style.get("draw:angle", "0")):g} .5 .5)"')
                defs.append(f'<{tag} id="{gid}" {geo}>' + "".join(
                    f'<stop offset="{i / (len(stops) - 1):g}" stop-color="{c}"/>' for i, c in enumerate(stops)) + f"</{tag}>")
                fill = f"url(#{gid})"
            stroke = style.get("svg:stroke-color", "#000") if style.get("draw:stroke") in ("solid", "dash") else "none"
            sw = inch(style["svg:stroke-width"]) if "svg:stroke-width" in style else 1
            attrs = f'fill="{fill}" stroke="{stroke}" stroke-width="{sw:.2f}" stroke-linejoin="round"'
            if "draw:opacity" in style and fill != "none":
                attrs += f' fill-opacity="{float(style["draw:opacity"].rstrip("%")) / 100:g}"'
            if style.get("draw:stroke") == "dash":   # libvisio gives dot/gap lengths as % of the stroke width
                pct = lambda k, dflt: (inch(style.get(k, dflt)) if style.get(k, dflt).endswith("in")   # inches or
                                       else float(style.get(k, dflt).rstrip("%")) / 100 * sw)        # % of stroke
                attrs += f' stroke-dasharray="{pct("draw:dots1-length", "300%"):.1f} {pct("draw:distance", "300%"):.1f}"'
            for end in ("start", "end"):
                path = style.get(f"draw:marker-{end}-path")
                if path:
                    mid, mw = f"m{len(defs)}", inch(style[f"draw:marker-{end}-width"])
                    # libvisio's marker points "up" (tip at top centre); its viewboxes don't always match the path, so
                    # use the path's own bbox. rotate(90) maps (x, y) -> (-y, x): the tip lands on the right edge.
                    xs, ys = zip(*(q for pts, _ in flatten_path(path) for q in pts))
                    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                    ref = -(y0 + y1) / 2 if style.get(f"draw:marker-{end}-center") == "true" else -y0
                    defs.append(f'<marker id="{mid}" viewBox="{-y1:g} {x0:g} {y1 - y0:g} {x1 - x0:g}" refX="{ref:g}" '
                                f'refY="{(x0 + x1) / 2:g}" markerUnits="userSpaceOnUse" markerWidth="{mw * (y1 - y0) / (x1 - x0):.2f}" '
                                f'markerHeight="{mw:.2f}" orient="auto-start-reverse">'
                                f'<path d="{path}" fill="{stroke}" fill-rule="evenodd" transform="rotate(90)"/></marker>')
                    attrs += f' marker-{end}="url(#{mid})"'
            if style.get("draw:shadow") == "visible" and fill != "none":
                dx, dy = inch(style["draw:shadow-offset-x"]), inch(style["draw:shadow-offset-y"])
                body.append(f'<path d="{d}" fill="{style["draw:shadow-color"]}" transform="translate({dx:.2f},{dy:.2f})"/>')
            body.append(f'<path d="{d}" {attrs}/>')
        elif op == "drawGraphicObject":
            p = props(arg)
            body.append(f'<image x="{inch(p["svg:x"]):.2f}" y="{inch(p["svg:y"]):.2f}" width="{inch(p["svg:width"]):.2f}" '
                        f'height="{inch(p["svg:height"]):.2f}" preserveAspectRatio="none" '
                        f'href="data:{p["librevenge:mime-type"]};base64,{p["office:binary-data"]}"/>')
        elif op == "startTextObject":
            p = props(arg)
            text = dict(x=inch(p["svg:x"]), y=inch(p["svg:y"]), w=inch(p["svg:width"]), h=inch(p["svg:height"]),
                        va=p.get("draw:textarea-vertical-align", "top"), pl=inch(p.get("fo:padding-left", "0in")),
                        pt=inch(p.get("fo:padding-top", "0in")), pr=inch(p.get("fo:padding-right", "0in")),
                        pb=inch(p.get("fo:padding-bottom", "0in")), rot=float(p.get("librevenge:rotate", "0")), paras=[])
        elif op == "openParagraph" and text is not None:
            p = props(arg)
            lh = p.get("fo:line-height", "1.2in")
            text["paras"].append(dict(align=p.get("fo:text-align", "left"), lh=inch(lh) if lh.endswith("in") else None, spans=[]))
        elif op == "openSpan" and text is not None:
            text["span"] = props(arg)
        elif op == "insertText" and text is not None:
            if not text["paras"]:   # text before any paragraph opened
                text["paras"].append(dict(align="left", lh=None, spans=[]))
            text["paras"][-1]["spans"].append((arg, text.get("span", {})))
        elif op == "endTextObject" and text is not None:
            first = len(body)   # everything drawn for this text block, so a rotated block can be wrapped in a <g>
            paras = [p for p in text["paras"] if p["spans"]]
            for p in paras:
                p["size"] = max(float(s.get("fo:font-size", "12pt").rstrip("pt")) * PX / 72 for _, s in p["spans"])
                p["lh"] = p["lh"] or p["size"] * 1.2
            total = sum(p["lh"] for p in paras)
            ih = text["h"] - text["pt"] - text["pb"]   # text block sits inside the margins
            top = text["y"] + text["pt"] + {"top": 0, "middle": (ih - total) / 2, "bottom": ih - total}[text["va"]]
            for p in paras:
                anchor, x = {"left": ("start", text["x"] + text["pl"]), "center": ("middle", text["x"] + (text["w"] + text["pl"] - text["pr"]) / 2),
                             "right": ("end", text["x"] + text["w"] - text["pr"])}[
                {"start": "left", "end": "right", "justify": "left"}.get(p["align"], p["align"])]
                bg = next((s["fo:background-color"] for _, s in p["spans"] if "fo:background-color" in s), None)
                if bg:
                    body.append(f'<rect x="{text["x"] + text["pl"]:.2f}" y="{top:.2f}" width="{text["w"] - text["pl"] - text["pr"]:.2f}" '
                                f'height="{p["lh"]:.2f}" fill="{bg}"/>')
                tspans = "".join(
                    f'<tspan fill="{s.get("fo:color", "#000")}" font-weight="{s.get("fo:font-weight", "normal")}" '
                    f'font-style="{s.get("fo:font-style", "normal")}" text-decoration="{deco(s)}" '
                    f'font-family="{s.get("style:font-name", "Arial")}" '
                    f'font-size="{float(s.get("fo:font-size", "12pt").rstrip("pt")) * PX / 72:.2f}">{html.escape(t)}</tspan>'
                    for t, s in p["spans"])
                body.append(f'<text x="{x:.2f}" y="{top + p["lh"] / 2:.2f}" text-anchor="{anchor}" '
                            f'dominant-baseline="central">{tspans}</text>')
                top += p["lh"]
            if text["rot"]:   # libvisio's angle is in page (y-down) terms, about the block's centre
                cx, cy = text["x"] + text["w"] / 2, text["y"] + text["h"] / 2
                body[first:] = [f'<g transform="rotate({text["rot"]:g} {cx:.2f} {cy:.2f})">' + "".join(body[first:]) + "</g>"]
            text = None

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.2f} {H:.2f}">'
           + ('<rect width="100%" height="100%" fill="#fff"/>' if background else "") + f'<defs>{"".join(defs)}</defs>{"".join(body)}</svg>')
    return svg


def render(svg, out_png):
    svg_path = os.path.join(tempfile.mkdtemp(), "preview.svg")
    open(svg_path, "w").write(svg)
    subprocess.run(["node", os.path.join(HERE, "shot.cjs"), svg_path, out_png, PPTR],
                   env={**os.environ, "MMDC_MODULES": MODULES}, check=True)


if __name__ == "__main__":
    src = sys.argv[1]
    out_png = sys.argv[2] if len(sys.argv) > 2 else re.sub(r"\.vsdx$", "", src) + "_preview.png"
    render(to_svg(src), out_png)
    print(out_png)
