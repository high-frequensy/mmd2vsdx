"""Visio stencil masters: find them by name and copy them, as real master instances, into a .vsdx package.

    lib = Library("stencils/1033")          # a folder of .vssx files (searched recursively), a .vssx, or a .zip of them
    m = lib.find("Server")                  # or "PERIPH/Server" / "Network and Peripherals/Server" to pick the stencil
    imp = Importer(files)                   # files: {part name: bytes} of the target package
    mid = imp.add(m)                        # copies master, its media and its style sheets (once per master)
    shape = imp.place(parent, m, new_id, x, y, scale)   # an instance, uniformly scaled, sub-shapes written out
    imp.finish()                            # writes masters.xml, rels, document styles, content types back

Only US-unit stencils (*_U.vssx) are used when both unit versions exist.
"""
import copy
import html
import io
import os
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

NS = "http://schemas.microsoft.com/office/visio/2012/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
Q = lambda n: f"{{{NS}}}{n}"
ET.register_namespace("", NS)   # Visio parts use the default namespace; some readers (libvisio) compare literal names
ET.register_namespace("r", REL_NS)
RID = f"{{{REL_NS}}}id"
CONTENT_TYPES = {"emf": "image/x-emf", "wmf": "image/x-wmf", "jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png",
                 "gif": "image/gif", "bmp": "image/bmp", "svg": "image/svg+xml", "xls": "application/vnd.ms-excel",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "bin": "application/vnd.openxmlformats-officedocument.oleObject",
                 "rels": "application/vnd.openxmlformats-package.relationships+xml"}

# lengths that scale with the shape (Visio stores these in inches); everything else (angles, ratios, fractions,
# line weights, font sizes) does not
LENGTH_CELLS = {"PinX", "PinY", "Width", "Height", "LocPinX", "LocPinY", "BeginX", "BeginY", "EndX", "EndY",
                "TxtPinX", "TxtPinY", "TxtWidth", "TxtHeight", "TxtLocPinX", "TxtLocPinY",
                "ImgOffsetX", "ImgOffsetY", "ImgWidth", "ImgHeight"}
GEOMETRY_LENGTHS = {"MoveTo": "XY", "LineTo": "XY", "ArcTo": "XYA", "EllipticalArcTo": "XYAB", "Ellipse": "XYABCD",
                    "NURBSTo": "XY", "PolylineTo": "XY", "SplineStart": "XY", "SplineKnot": "XY", "InfiniteLine": "XYAB"}
COLOR_CELLS = {"LineColor", "FillForegnd", "FillBkgnd", "ShdwForegnd", "ShdwBkgnd", "TextBkgnd", "Color",
               "GradientStopColor", "LineGradientColor"}
# Rel* rows hold fractions of Width/Height: they follow the shape by themselves
SECTION_LENGTHS = {"Connection": "XY", "Controls": "XY"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Master:
    def __init__(self, stencil, key, title, el):
        self.stencil, self.key, self.title, self.el = stencil, key, title, el
        self.name_u = el.get("NameU") or el.get("Name") or f"Master {el.get('ID')}"   # a few have only Name
        self.name = el.get("Name") or self.name_u

    def __repr__(self):
        return f"{self.key}/{self.name_u}"


class Stencil:
    """One .vssx: its parts, read lazily."""

    def __init__(self, label, read):
        self.label, self._read, self._files = label, read, None
        base = re.sub(r"\.vssx$", "", posixpath.basename(label), flags=re.I)
        self.key = re.sub(r"_[UM]$", "", base, flags=re.I).upper()

    @property
    def files(self):
        if self._files is None:
            with zipfile.ZipFile(io.BytesIO(self._read())) as z:
                self._files = {n: z.read(n) for n in z.namelist()}
        return self._files

    def masters(self):
        title = re.search(rb"<dc:title>([^<]*)</dc:title>", self.files.get("docProps/core.xml", b""))
        title = html.unescape(title.group(1).decode()) if title else self.key
        root = ET.fromstring(self.files["visio/masters/masters.xml"])
        return [Master(self, self.key, title, m) for m in root.findall(Q("Master")) if m.get("Hidden") != "1"]

    def colors(self):
        """The stencil's own colour table (index -> #RRGGBB). Indexes 0-23 are Visio's fixed palette; 24 and up
        differ per document, so copied cells must not keep them as numbers."""
        d = ET.fromstring(self.files["visio/document.xml"])
        return {c.get("IX"): c.get("RGB") for c in d.iter(Q("ColorEntry")) if int(c.get("IX")) >= 24}

    def master_part(self, m):
        """Part name of a master's content."""
        rels = ET.fromstring(self.files["visio/masters/_rels/masters.xml.rels"])
        target = {r.get("Id"): r.get("Target") for r in rels}[m.el.find(Q("Rel")).get(RID)]
        return posixpath.normpath(posixpath.join("visio/masters", target))


class Library:
    def __init__(self, path):
        self.path = path
        sources = []   # (label, reader)
        if os.path.isdir(path):
            for d, _, fs in os.walk(path):
                for f in sorted(fs):
                    if f.lower().endswith(".vssx"):
                        p = os.path.join(d, f)
                        sources.append((p, lambda p=p: open(p, "rb").read()))
        elif path.lower().endswith(".zip"):
            z = zipfile.ZipFile(path)
            sources = [(n, lambda n=n: z.read(n)) for n in z.namelist() if n.lower().endswith(".vssx")]
        elif os.path.isfile(path):
            sources = [(path, lambda: open(path, "rb").read())]
        if not sources:
            raise SystemExit(f"no Visio stencils (.vssx) found at {path!r}. mmd2vsdx does not ship Microsoft's stencils: copy the "
                             "'Visio Content/1033' folder from your Visio install to ./stencils/1033, or point --stencils "
                             "(or MMD2VSDX_STENCILS) at it. See docs/visio-stencils.md.")
        labels = {posixpath.basename(l).upper() for l, _ in sources}
        # both unit versions shipped: keep US units (_U), drop the metric twin (_M)
        sources = [(l, r) for l, r in sources
                   if not (re.search(r"_M\.vssx$", l, re.I) and re.sub(r"_M\.VSSX$", "_U.VSSX", posixpath.basename(l).upper()) in labels)]
        self.stencils = [Stencil(l, r) for l, r in sorted(sources)]
        self._masters = None

    def masters(self):
        if self._masters is None:
            self._masters = [m for s in self.stencils for m in s.masters()]
        return self._masters

    def find(self, query):
        """All masters matching 'Name' or 'Stencil/Name' (stencil = file key like PERIPH, or its title), ignoring case,
        spaces and punctuation. Exact name matches only."""
        stencil, name = (query.split("/", 1) if "/" in query else (None, query))
        hits = [m for m in self.masters() if norm(name) in (norm(m.name_u), norm(m.name))]
        if stencil is not None:
            scoped = [m for m in hits if norm(stencil) in (norm(m.key), norm(m.title))]
            if scoped or any(norm(stencil) in (norm(s.key),) for s in self.stencils):
                return scoped
            hits = [m for m in self.masters() if norm(query) in (norm(m.name_u), norm(m.name))]   # a '/' in the name
        return hits

    def search(self, text):
        """Masters whose name, stencil or keywords contain text (for discovery)."""
        t = norm(text)
        out = []
        for m in self.masters():
            kw = m.el.find(f"{Q('PageSheet')}/{Q('Cell')}[@N='ShapeKeywords']")
            words = kw.get("V") or kw.text if kw is not None else ""
            if t in norm(m.name_u) or t in norm(m.name) or t in norm(m.title) or t in norm(m.key) or t in norm(words):
                out.append(m)
        return out


class Importer:
    """Copies masters into a package (dict of part name -> bytes) and places instances of them."""

    def __init__(self, files):
        self.files = files
        self.masters_xml = ET.fromstring(files["visio/masters/masters.xml"])
        self.doc = ET.fromstring(files["visio/document.xml"])
        self.styles = self.doc.find(Q("StyleSheets"))
        self.base_styles = {s.get("NameU") for s in self.styles.findall(Q("StyleSheet"))}   # the template's own
        self.rels, self.ctypes, self.added = [], [], {}
        self.trees = {}   # Master -> its top shape (for placing instances)
        ids = [int(m.get("ID")) for m in self.masters_xml.findall(Q("Master"))]
        self.next_master = max(ids + [0]) + 1
        self.part_n = 0

    # ----- copying -----
    def _style_map(self, st, used):
        """Stencil style ids -> document style ids, parents first. The document's base styles (Normal, Theme, ...)
        are shared; a style another stencil already brought in is reused only if it is the same style, otherwise
        this one is copied under a suffixed name (different stencils define "3D Net Face" differently)."""
        sdoc = ET.fromstring(st.files["visio/document.xml"])
        src = {s.get("ID"): s for s in sdoc.iter(Q("StyleSheet"))}
        have = {s.get("NameU"): s.get("ID") for s in self.styles.findall(Q("StyleSheet"))}
        out = {}

        def bring(i):
            if i in out or i not in src:
                return out.get(i, "0")
            s = src[i]
            out[i] = "pending"
            new = copy.deepcopy(s)
            fix_colors(new, st.colors())
            for a in ("LineStyle", "FillStyle", "TextStyle"):
                if new.get(a) is not None:
                    new.set(a, bring(new.get(a)))
            base = s.get("NameU") or s.get("Name") or f"Style {i}"   # some stencils name styles only by Name
            name = base
            if name in self.base_styles:
                out[i] = have[name]
                return out[i]
            k = 1
            while name in have:
                if style_body(self.styles.find(f"{Q('StyleSheet')}[@ID='{have[name]}']")) == style_body(new):
                    out[i] = have[name]
                    return out[i]
                k += 1
                name = f"{base}.{k}"
            new.set("NameU", name)
            new.set("Name", name if k > 1 else (s.get("Name") or name))
            nid = str(max(int(x.get("ID")) for x in self.styles.findall(Q("StyleSheet"))) + 1)
            new.set("ID", nid)
            self.styles.append(new)
            have[new.get("NameU")] = out[i] = nid
            return nid

        for i in used:
            bring(i)
        return out

    def _copy_part(self, st, name):
        """Copy a part (and, recursively, what its rels point at) under a fresh name; return the new name."""
        self.part_n += 1
        d, b = posixpath.split(name)
        new = posixpath.join(d, f"s{self.part_n}_{b}")
        self.files[new] = st.files[name]
        ext = b.rsplit(".", 1)[-1].lower()
        if ext not in ("xml",):
            self.ctypes.append(("Default", ext, CONTENT_TYPES.get(ext, "application/octet-stream")))
        rels_name = posixpath.join(d, "_rels", b + ".rels")
        if rels_name in st.files:
            rels = ET.fromstring(st.files[rels_name])
            for r in rels:
                if r.get("TargetMode") != "External":
                    tgt = posixpath.normpath(posixpath.join(d, r.get("Target")))
                    r.set("Target", posixpath.relpath(self._copy_part(st, tgt), d))
            self.files[posixpath.join(d, "_rels", posixpath.basename(new) + ".rels")] = rels_xml(rels)
        return new

    def add(self, m):
        """Copy master m into the package once; return its master ID."""
        if m in self.added:
            return self.added[m]
        st = m.stencil
        part = st.master_part(m)
        body = ET.fromstring(st.files[part])
        used = {e.get(a) for e in body.iter() for a in ("LineStyle", "FillStyle", "TextStyle") if e.get(a) is not None}
        el = copy.deepcopy(m.el)
        used |= {e.get(a) for e in el.iter() for a in ("LineStyle", "FillStyle", "TextStyle") if e.get(a) is not None}
        smap = self._style_map(st, used)
        for c in list(body.iter(Q("Cell"))) + list(el.iter(Q("Cell"))):   # some stencils (Cisco) put a cell's value
            if c.get("V") is None and (c.text or "").strip():              # in its text: the schema wants V="..."
                c.set("V", c.text)
                c.text = None
        fix_colors(body, st.colors())
        fix_colors(el, st.colors())
        for e in list(body.iter()) + list(el.iter()):
            for a in ("LineStyle", "FillStyle", "TextStyle"):
                if e.get(a) is not None:
                    e.set(a, smap.get(e.get(a), "0"))
        mid = self.next_master
        self.next_master += 1
        new_part = f"visio/masters/master{mid}.xml"
        while new_part in self.files:
            mid = self.next_master
            self.next_master += 1
            new_part = f"visio/masters/master{mid}.xml"
        self.files[new_part] = ET.tostring(body, encoding="utf-8", xml_declaration=True)
        rels_name = posixpath.join("visio/masters/_rels", posixpath.basename(part) + ".rels")
        if rels_name in st.files:   # the master's own media (EMF/JPEG pictures, embedded objects)
            rels = ET.fromstring(st.files[rels_name])
            for r in rels:
                if r.get("TargetMode") != "External":
                    tgt = posixpath.normpath(posixpath.join("visio/masters", r.get("Target")))
                    r.set("Target", posixpath.relpath(self._copy_part(st, tgt), "visio/masters"))
            self.files[f"visio/masters/_rels/master{mid}.xml.rels"] = rels_xml(rels)
        names = {x.get("NameU") for x in self.masters_xml.findall(Q("Master"))}
        if m.name_u in names:   # Visio's own convention for a second master of the same name
            el.set("NameU", f"{m.name_u}.{mid}")
            el.set("Name", f"{m.name}.{mid}")
        el.set("ID", str(mid))
        el.find(Q("Rel")).set(RID, f"rIdM{mid}")
        self.masters_xml.append(el)
        self.rels.append(f'<Relationship Id="rIdM{mid}" Type="http://schemas.microsoft.com/visio/2010/relationships/master" '
                         f'Target="master{mid}.xml"/>')
        self.ctypes.append(("Override", f"/{new_part}", "application/vnd.ms-visio.master+xml"))
        self.added[m] = str(mid)
        self.trees[m] = body.find(Q("Shapes"))[0]
        return str(mid)

    def size(self, m):
        """Native (width, height) of a master's shape, in inches."""
        self.add(m)
        c = {x.get("N"): x for x in self.trees[m].findall(Q("Cell"))}
        return float(c["Width"].get("V")), float(c["Height"].get("V"))

    # ----- placing -----
    def place(self, parent, m, new_id, x, y, scale=1.0, name=None, shape_id=None, blank_text=False):
        """An instance of m with its bottom-left corner at (x, y) inches, scaled uniformly. Every scaled length is
        written out (F="Inh": the master's formula is kept, the cached value is the scaled one), so readers that do
        not evaluate formulas draw it at the right size too. Sub-shapes are listed as Visio lists them."""
        mid = self.add(m)
        top = self.trees[m]
        w, h = self.size(m)
        c = {x_.get("N"): x_ for x_ in top.findall(Q("Cell"))}
        val = lambda n, d=0.0: float(c[n].get("V")) if n in c and c[n].get("V") not in (None, "") else d
        s = ET.SubElement(parent, Q("Shape"), {"ID": str(shape_id or new_id()), "Type": top.get("Type") or "Shape", "Master": mid})
        if name:
            s.set("NameU", name)
        lpx, lpy = val("LocPinX", w / 2) * scale, val("LocPinY", h / 2) * scale
        cells = [("PinX", x + lpx), ("PinY", y + lpy), ("Width", w * scale), ("Height", h * scale),
                 ("LocPinX", lpx), ("LocPinY", lpy)]
        if "BeginX" in c:   # a 1-D master (line, brace, callout): Visio places it by its end points
            ox, oy = val("PinX") - val("LocPinX"), val("PinY") - val("LocPinY")   # master's box corner
            cells += [(n, (x if n.endswith("X") else y) + (val(n) - (ox if n.endswith("X") else oy)) * scale)
                      for n in ("BeginX", "BeginY", "EndX", "EndY")]
        for n, v in cells:
            a = {"N": n, "V": repr(v)}
            if n in c and c[n].get("F") and (n.startswith("LocPin") or "BeginX" in c):
                a["F"] = "Inh"   # keep the master's formula (a 1-D shape's box follows its end points)
            ET.SubElement(s, Q("Cell"), a)
        if scale != 1:
            scale_cells(top, s, scale, skip={"PinX", "PinY", "Width", "Height", "LocPinX", "LocPinY",
                                             "BeginX", "BeginY", "EndX", "EndY"})
        kids = top.find(Q("Shapes"))
        if kids is not None:
            box = ET.SubElement(s, Q("Shapes"))
            for k in kids.findall(Q("Shape")):
                clone(k, box, new_id, scale, blank_text)
        return s

    def finish(self):
        f = self.files
        f["visio/masters/masters.xml"] = ET.tostring(self.masters_xml, encoding="utf-8", xml_declaration=True)
        f["visio/document.xml"] = ET.tostring(self.doc, encoding="utf-8", xml_declaration=True)
        f["visio/masters/_rels/masters.xml.rels"] = f["visio/masters/_rels/masters.xml.rels"].replace(
            b"</Relationships>", "".join(self.rels).encode() + b"</Relationships>")
        ct = f["[Content_Types].xml"].decode()
        for kind, key, typ in self.ctypes:
            attr = "Extension" if kind == "Default" else "PartName"
            if f'{attr}="{key}"' not in ct:
                ct = ct.replace("</Types>", f'<{kind} {attr}="{key}" ContentType="{typ}"/></Types>')
        f["[Content_Types].xml"] = ct.encode()


def style_body(el):
    """A style sheet's content, for comparing two styles regardless of their ids and names."""
    c = copy.deepcopy(el)
    for a in ("ID", "NameU", "Name", "IsCustomName", "IsCustomNameU"):
        c.attrib.pop(a, None)
    return ET.tostring(c)


def fix_colors(el, table):
    """Indexed colours (the stencil's table) -> explicit #RRGGBB, so they mean the same in any document; colour
    values cached as "RGB(r, g, b)" (some stencils) -> #RRGGBB, which every reader parses (libvisio rejects the
    whole document otherwise)."""
    for c in el.iter(Q("Cell")):
        m = re.match(r"\s*RGB\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*$", c.get("V") or "")
        if m and c.get("N") in COLOR_CELLS | {"Color"}:
            c.set("V", "#" + "".join(f"{min(int(v), 255):02X}" for v in m.groups()))
        if c.get("N") in COLOR_CELLS and table:
            for a in ("V", "F"):
                if (c.get(a) or "").strip() in table:
                    c.set(a, table[c.get(a).strip()] if a == "V" else f'RGB({",".join(str(int(table[c.get(a).strip()][i:i + 2], 16)) for i in (1, 3, 5))})')


def rels_xml(rels):
    """Serialize a relationships element with the default namespace (some readers match unprefixed names)."""
    body = "".join(f'<Relationship {" ".join(f"{k}={quote(v)}" for k, v in r.attrib.items())}/>' for r in rels)
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Relationships xmlns="{PKG_NS}">{body}</Relationships>').encode()


def quote(v):
    return '"' + v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;") + '"'


def scale_number_list(formula, scale, fn):
    """Scale the absolute coordinates inside NURBS(...)/POLYLINE(...) when their x/y types say absolute (1)."""
    m = re.match(rf"\s*{fn}\((.*)\)\s*$", formula or "", re.S)
    if not m:
        return None
    a = [t.strip() for t in m.group(1).split(",")]
    head = 4 if fn == "NURBS" else 2
    xt, yt = a[head - 2], a[head - 1]
    step = 4 if fn == "NURBS" else 2
    for i in range(head, len(a), step):
        try:
            if xt == "1":
                a[i] = repr(float(a[i]) * scale)
            if yt == "1":
                a[i + 1] = repr(float(a[i + 1]) * scale)
        except (ValueError, IndexError):
            return None
    return f"{fn}({', '.join(a)})"


def scale_cells(src, dst, scale, skip=()):
    """Write the scaled length values of src's cells and geometry/connection/control rows onto dst (F="Inh")."""
    for c in src.findall(Q("Cell")):
        n = c.get("N")
        if n in LENGTH_CELLS and n not in skip and c.get("V") not in (None, ""):
            try:
                ET.SubElement(dst, Q("Cell"), {"N": n, "V": repr(float(c.get("V")) * scale), "F": "Inh"})
            except ValueError:
                pass
    for sec in src.findall(Q("Section")):
        kind = sec.get("N")
        if kind not in ("Geometry",) and kind not in SECTION_LENGTHS:
            continue
        out = None
        for row in sec.findall(Q("Row")):
            names = GEOMETRY_LENGTHS.get(row.get("T"), "") if kind == "Geometry" else SECTION_LENGTHS[kind]
            cells = []
            for c in row.findall(Q("Cell")):
                n, v = c.get("N"), c.get("V")
                if n in names and v not in (None, ""):
                    try:
                        cells.append({"N": n, "V": repr(float(v) * scale), "F": "Inh"})
                    except ValueError:
                        pass
                elif (row.get("T"), n) in (("NURBSTo", "E"), ("PolylineTo", "A")):
                    sv = scale_number_list(v, scale, "NURBS" if row.get("T") == "NURBSTo" else "POLYLINE")
                    if sv and sv != v:
                        cells.append({"N": n, "V": sv, "F": "Inh"})
            if cells:
                if out is None:
                    out = ET.SubElement(dst, Q("Section"), {k: v for k, v in sec.attrib.items()})
                r = ET.SubElement(out, Q("Row"), {k: v for k, v in row.attrib.items()})
                for a in cells:
                    ET.SubElement(r, Q("Cell"), a)


def clone(src, parent, new_id, scale, blank_text=False):
    """A sub-shape of an instance: points at its master sub-shape; carries scaled lengths when resized.
    blank_text: hide the master's own captions (the instance's text replaces them)."""
    el = ET.SubElement(parent, Q("Shape"), {"ID": str(new_id()), "Type": src.get("Type") or "Shape", "MasterShape": src.get("ID")})
    if scale != 1:
        scale_cells(src, el, scale)
    if blank_text and src.find(Q("Text")) is not None and "".join(src.find(Q("Text")).itertext()).strip():
        ET.SubElement(el, Q("Cell"), {"N": "HideText", "V": "1"})   # reversible in Visio (Format > Behavior)
    kids = src.find(Q("Shapes"))
    if kids is not None:
        box = ET.SubElement(el, Q("Shapes"))
        for k in kids.findall(Q("Shape")):
            clone(k, box, new_id, scale, blank_text)
    return el
