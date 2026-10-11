#!/usr/bin/env python3
"""Validate every Visio XML part of a .vsdx/.vssx against Microsoft's published Visio 2013 schema (XSD 1.1).

Usage: python -m mmd2vsdx.validate_vsdx file.vsdx [more files...]     (needs: pip install xmlschema)
Also checks the package plumbing Visio relies on: content types and relationship targets.
Exit code 1 if anything is invalid.
"""
import os
import posixpath
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

import xmlschema

from .runtime import DATA


def load_schema():
    """Microsoft's published XSD (namespace retargeted to the shipping 2012/main), with fixes for places
    where it rejects files Visio itself writes. Each fix was confirmed against Microsoft-authored
    .vsdx/.vssx files (the template and the stencils under stencils/), which must validate clean."""
    xsd = open(os.path.join(DATA, "visio2012.xsd"), encoding="utf-8").read()
    # 1. section asserts compare against bare names (@N = Character), which XPath reads as a child element
    xsd = re.sub(r'test="@N = ([A-Za-z]+)"', lambda m: f"test=\"@N = '{m.group(1)}'\"", xsd)
    # 2. real parts carry xml:space on their roots and r:id on <Rel>
    xsd = xsd.replace('<xsd:complexType name="Rel_Type" />',
                      '<xsd:complexType name="Rel_Type"><xsd:anyAttribute namespace="##other" processContents="lax" /></xsd:complexType>')
    for root in ("VisioDocument", "Pages", "PageContents", "Masters", "MasterContents", "Windows"):
        xsd = re.sub(rf'(<xsd:complexType name="{root}_Type">.*?)(\s*</xsd:complexType>)',
                     lambda m: m.group(0) if "anyAttribute" in m.group(1) or "complexContent" in m.group(1) else
                     m.group(1) + '<xsd:anyAttribute namespace="##other" processContents="lax" />' + m.group(2),
                     xsd, count=1, flags=re.S)
    # 3. FaceName is keyed by NameU in shipping files; ID/Name are optional
    xsd = xsd.replace('<xsd:attribute name="ID" type="xsd:unsignedInt" use="required" />\n        <xsd:attribute name="Name" type="xsd:string" use="required" />\n        <xsd:attribute name="UnicodeRanges"',
                      '<xsd:attribute name="ID" type="xsd:unsignedInt" />\n        <xsd:attribute name="Name" type="xsd:string" />\n        <xsd:attribute name="NameU" type="xsd:string" />\n        <xsd:attribute name="UnicodeRanges"')
    # 4. <Text> is mixed content: characters between <cp>/<pp>/<tp>/<fld> markers
    xsd = xsd.replace('<xsd:complexType name="Text_Type">', '<xsd:complexType name="Text_Type" mixed="true">')
    # 5. connection-point rows carry T="Connection" in Microsoft's own stencils (e.g. every 3D network master)
    xsd = re.sub(r'(<xsd:complexType name="ConnectionRow_Type">.*?)(\s*</xsd:extension>)',
                 lambda m: m.group(1) + '\n                <xsd:attribute name="T" type="xsd:string" />' + m.group(2),
                 xsd, count=1, flags=re.S)
    return xmlschema.XMLSchema11(xsd, base_url=DATA)


SCHEMA = load_schema()
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def check(path, schema_parts=lambda name: True):
    """Problems found in a .vsdx; schema_parts(name) picks which XML parts get the (slow) schema check."""
    problems = []
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    ct = ET.fromstring(z.read("[Content_Types].xml"))
    overrides = {o.get("PartName").lstrip("/") for o in ct if o.tag.endswith("Override")}
    defaults = {d.get("Extension") for d in ct if d.tag.endswith("Default")}
    for n in sorted(names):
        if n.endswith("/") or n == "[Content_Types].xml":
            continue
        if n not in overrides and n.rsplit(".", 1)[-1] not in defaults:
            problems.append(f"{n}: no content type")
        if n.endswith(".rels"):   # every internal relationship target must exist
            base = posixpath.dirname(posixpath.dirname(n))
            for r in ET.fromstring(z.read(n)):
                if r.get("TargetMode") == "External":
                    continue
                tgt = posixpath.normpath(posixpath.join(base, r.get("Target"))).lstrip("/")
                if tgt not in names:
                    problems.append(f"{n}: relationship {r.get('Id')} -> missing {tgt}")
        elif n.startswith("visio/") and n.endswith(".xml") and schema_parts(n):
            errors = list(SCHEMA.iter_errors(z.read(n).decode("utf-8")))
            for e in errors[:5]:
                where = e.path or ""
                problems.append(f"{n} {where}: {e.reason}")
            if len(errors) > 5:
                problems.append(f"{n}: ... {len(errors) - 5} more")
    for o in overrides:
        if o not in names:
            problems.append(f"[Content_Types].xml: override for missing part {o}")
    for n in sorted(names):
        if re.match(r"visio/pages/page\d+\.xml$", n):
            problems += [f"{n}: {p}" for p in glue_problems(ET.fromstring(z.read(n)))]
    return problems


V = "{http://schemas.microsoft.com/office/visio/2012/main}"


def glue_problems(page):
    """Each <Connect> must agree with the connector's own glue formula and move trigger;
    container relationships (DEPENDSON 1 / 4) must be reciprocal; no shape or row may set a cell twice."""
    out = []
    shapes = {s.get("ID"): s for s in page.find(V + "Shapes")}
    cells = lambda s: {c.get("N"): c for c in s.findall(V + "Cell")}
    for c in page.iter(V + "Connect"):
        conn, tgt, part = shapes.get(c.get("FromSheet")), c.get("ToSheet"), c.get("FromPart")
        if conn is None or tgt not in shapes:
            out.append(f"Connect {c.get('FromSheet')}->{tgt}: unknown shape")
            continue
        end = {"9": "Begin", "12": "End"}.get(part)
        cc = cells(conn)
        f = (cc.get(end + "X").get("F") or "") if end and cc.get(end + "X") is not None else ""
        row = re.match(r"Connections\.X(\d+)", c.get("ToCell") or "")
        if not end or f"Sheet.{tgt}!Connections.X{row.group(1) if row else '?'}" not in f:
            out.append(f"{conn.get('NameU')}: {end or part} glue record points at {tgt}/{c.get('ToCell')} but formula is {f!r}")
        trig = cc.get({"Begin": "BegTrigger", "End": "EndTrigger"}.get(end, ""))
        if trig is None or f"Sheet.{tgt}!" not in (trig.get("F") or ""):
            out.append(f"{conn.get('NameU')}: {end} end has no move trigger on sheet {tgt}")
        if row and tgt in shapes:
            n_rows = sum(len(sec.findall(V + "Row")) for sec in shapes[tgt].findall(V + "Section") if sec.get("N") == "Connection")
            if int(row.group(1)) > n_rows:
                out.append(f"{conn.get('NameU')}: glued to connection point {row.group(1)} but shape has {n_rows}")
    recorded = {(c.get("FromSheet"), c.get("FromCell")) for c in page.iter(V + "Connect")}
    for sid, sh in shapes.items():   # the reverse: a glue formula with no glue record is half-glued
        for end in ("BeginX", "EndX"):
            f = cells(sh).get(end)
            if f is not None and "PNT(Sheet." in (f.get("F") or "") and (sid, end) not in recorded:
                out.append(f"{sh.get('NameU')}: {end} has a glue formula but no <Connect> record")
    for sh in page.iter(V + "Shape"):   # rows after the first inherit nothing from the text style: Visio reads a
        for sec in sh.findall(V + "Section"):   # missing FontScale as 0% and the run's characters pile up
            if sec.get("N") == "Character":
                for row in sec.findall(V + "Row"):
                    have = {c.get("N") for c in row.findall(V + "Cell")}
                    if row.get("IX") != "0" and not {"FontScale", "Size"} <= have:
                        out.append(f"{sh.get('NameU') or sh.get('ID')}: Character row {row.get('IX')} lacks "
                                   f"{', '.join(sorted({'FontScale', 'Size'} - have))}")
    for holder in list(page.iter(V + "Shape")) + list(page.iter(V + "Row")):   # a cell set twice: Visio may refuse
        names = [c.get("N") for c in holder.findall(V + "Cell")]
        for n in sorted({n for n in names if names.count(n) > 1}):
            out.append(f"{holder.get('NameU') or holder.get('N') or holder.get('IX')}: cell {n} appears twice")
    rel = {sid: re.findall(r"DEPENDSON\((\d+),([^()]*(?:\(\)[^()]*)*)\)", (cells(s).get("Relationships").get("F") or ""))
           for sid, s in shapes.items() if cells(s).get("Relationships") is not None}
    links = {(t, sid, int(k)) for sid, deps in rel.items() for k, refs in deps for t in re.findall(r"Sheet\.(\d+)!", refs)}
    for t, sid, k in links:
        if k == 1 and (sid, t, 4) not in links:
            out.append(f"container {shapes[sid].get('NameU')} lists {shapes.get(t, {}).get('NameU') if t in shapes else t} but it doesn't point back")
        if k == 4 and (sid, t, 1) not in links:
            out.append(f"{shapes[sid].get('NameU')} says it's in {t} but that container doesn't list it")
    return out


def main(paths):
    bad = 0
    for f in paths:
        probs = check(f)
        bad += bool(probs)
        print(f"{'OK  ' if not probs else 'FAIL'} {os.path.basename(f)}" + ("" if not probs else f"  ({len(probs)} problems)"))
        for p in probs:
            print("     -", re.sub(r"\s+", " ", p)[:300])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
