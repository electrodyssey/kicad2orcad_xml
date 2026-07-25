#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
"""Convert a KiCad 6/7 .kicad_sym library into an OrCAD Capture library XML
(olb.xsd dialect) that can be imported with File > Import > Library XML.

Usage:
    python3 kicad2orcad_xml.py xcku060.kicad_sym -o xcku060.xml

Notes / conventions used:
  * 1 OrCAD unit = 10 mil = 0.254 mm.  KiCad Y grows up, OrCAD Y grows down.
  * Each KiCad unit becomes one section of an OrCAD package.  Sections with
    different graphics/pins means the package is heterogeneous
    (isHomogeneous="0"); use --homogeneous to force otherwise.
  * A KiCad pin's (at x y) point is the electrical end -> OrCAD hotpt*,
    the body end (at + length along the pin angle) -> OrCAD start*.
"""

import argparse
import os
import re
import sys
from xml.sax.saxutils import escape, quoteattr

MM_PER_UNIT = 0.254  # 10 mil

# KiCad electrical type -> OrCAD PortType
#   0 Input, 1 Bidirectional, 2 Output, 3 OpenCollector,
#   4 Passive, 5 ThreeState, 6 OpenEmitter, 7 Power
PIN_TYPE = {
    "input": 0,
    "bidirectional": 1,
    "output": 2,
    "open_collector": 3,
    "passive": 4,
    "free": 4,
    "unspecified": 4,
    "tri_state": 5,
    "open_emitter": 6,
    "power_in": 7,
    "power_out": 7,
    "no_connect": 4,
}

# ---------------------------------------------------------------- s-expression


def parse_sexp(text):
    """Minimal s-expression reader -> nested lists of str."""
    tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')
    stack, cur = [], []
    for m in tok.finditer(text):
        t = m.group()
        if t == "(":
            stack.append(cur)
            cur = []
            stack[-1].append(cur)
        elif t == ")":
            cur = stack.pop()
        elif t.startswith('"'):
            cur.append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            cur.append(t)
    return cur[0]


def kids(node, name):
    return [n for n in node if isinstance(n, list) and n and n[0] == name]


def kid(node, name):
    r = kids(node, name)
    return r[0] if r else None


# ---------------------------------------------------------------- KiCad model


class Pin:
    __slots__ = ("name", "number", "etype", "x", "y", "angle", "length")


class Unit:
    def __init__(self, index):
        self.index = index
        self.pins = []
        self.lines = []  # [(x1, y1, x2, y2), ...] in mm


def read_symbol(path, want=None):
    root = parse_sexp(open(path, encoding="utf-8").read())
    symbols = kids(root, "symbol")
    if not symbols:
        sys.exit("no (symbol ...) found in %s" % path)
    if want:
        symbols = [s for s in symbols if s[1] == want] or sys.exit(
            "symbol %r not in library" % want)
    return symbols


def properties(sym):
    props = {}
    for p in kids(sym, "property"):
        props[p[1]] = (p[2], p)
    return props


def read_units(sym):
    """KiCad sub-symbol names are  <NAME>_<unit>_<bodystyle>."""
    units = {}
    for sub in kids(sym, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", sub[1])
        if not m:
            continue
        uidx, style = int(m.group(1)), int(m.group(2))
        if style not in (0, 1):  # ignore DeMorgan alternate bodies
            continue
        u = units.setdefault(uidx, Unit(uidx))
        for poly in kids(sub, "polyline"):
            pts = [(float(p[1]), float(p[2])) for p in kids(kid(poly, "pts"), "xy")]
            for a, b in zip(pts, pts[1:]):
                u.lines.append((a[0], a[1], b[0], b[1]))
        for rect in kids(sub, "rectangle"):
            s = kid(rect, "start")
            e = kid(rect, "end")
            x1, y1, x2, y2 = float(s[1]), float(s[2]), float(e[1]), float(e[2])
            u.lines += [(x1, y1, x2, y1), (x2, y1, x2, y2),
                        (x2, y2, x1, y2), (x1, y2, x1, y1)]
        for pn in kids(sub, "pin"):
            p = Pin()
            p.etype = pn[1]
            at = kid(pn, "at")
            p.x, p.y = float(at[1]), float(at[2])
            p.angle = int(float(at[3])) if len(at) > 3 else 0
            p.length = float(kid(pn, "length")[1])
            p.name = kid(pn, "name")[1]
            p.number = kid(pn, "number")[1]
            u.pins.append(p)
    return [units[k] for k in sorted(units)]


# ---------------------------------------------------------------- XML writing


DEFAULT_FONTS = [
    # (index, height, width, name)   -- Capture's stock defaults
    (0, -9, 4, "Arial"), (1, -9, 4, "Arial"), (2, -9, 4, "Arial"),
    (3, 8, 0, "Arial"), (4, 8, 0, "Arial"), (5, -9, 4, "Arial"),
    (6, -9, 4, "Arial"), (7, -9, 4, "Arial"), (8, -9, 4, "Arial"),
    (9, -9, 4, "Arial"), (10, -9, 4, "Arial"), (11, -9, 4, "Arial"),
    (12, -9, 4, "Arial"), (13, -9, 5, "Courier New"), (14, -9, 4, "Arial"),
    (15, -9, 4, "Arial"), (16, -9, 4, "Arial"), (17, 8, 0, "Arial"),
    (18, 8, 0, "Arial"), (19, 8, 0, "Arial"), (20, 8, 0, "Arial"),
    (21, 8, 0, "Arial"), (22, 8, 0, "Arial"), (23, 8, 0, "Arial"),
]

PART_FIELDS = ["1ST PART FIELD", "2ND PART FIELD", "3RD PART FIELD",
               "4TH PART FIELD", "5TH PART FIELD", "6TH PART FIELD",
               "7TH PART FIELD", "PCB Footprint"]


class Out:
    def __init__(self, fh):
        self.fh = fh
        self.d = 0

    def open(self, tag):
        self.fh.write("  " * self.d + "<%s>\n" % tag)
        self.d += 1

    def close(self, tag):
        self.d -= 1
        self.fh.write("  " * self.d + "</%s>\n" % tag)

    def defn(self, **kw):
        self.leaf("Defn", **kw)

    def leaf(self, tag, **kw):
        attrs = "".join(
            " %s=%s" % (k.rstrip("_"), quoteattr(str(v)))
            for k, v in kw.items())
        self.fh.write("  " * self.d + "<%s%s/>\n" % (tag, attrs))

    def flag(self, tag, val):
        self.open(tag)
        self.defn(val=val)
        self.close(tag)


def write_default_values(o):
    o.open("DefaultValues")
    o.leaf("Defn")
    for idx, h, w, nm in DEFAULT_FONTS:
        o.open("DefaultFont")
        o.defn(charset=0, escapement=0, height=h, index=idx, italic=0,
               name=nm, orientation=0, weight=400, width=w)
        o.close("DefaultFont")
    o.open("DefaultPageRec")
    o.defn(ANSIGridRefs=1, BorderDisplayed=1, BorderPrinted=1,
           GridRefDisplayed=1, GridRefPrinted=1, HorizontalLabelCount=5,
           HorizontalLabelIsAscending=0, HorizontalLabelIsChar=0,
           HorizontalLabelWidth=2540, IsMetric=0, PinToPin=2540,
           TitleBlockDisplayed=1, TitleBlockPrinted=1, VerticalLabelCount=4,
           VerticalLabelIsAscending=0, VerticalLabelIsChar=1,
           VerticalLabelWidth=2540)
    o.close("DefaultPageRec")
    o.flag("DefaultPlacedInstIsPrimitive", 0)
    o.flag("DefaultDrawnInstIsPrimitive", 0)
    for i, name in enumerate(PART_FIELDS, 1):
        o.open("DefaultPartFieldMapping")
        o.defn(index=i, val=name)
        o.close("DefaultPartFieldMapping")
    o.close("DefaultValues")


def prop_font(o, tag="PropFont"):
    o.open(tag)
    o.defn(charset=0, escapement=0, height=-9, italic=0, name="Arial",
           orientation=0, weight=400, width=4)
    o.close(tag)


def display_prop(o, name, x, y, color=48, dispmode=1):
    o.open("SymbolDisplayProp")
    o.defn(locX=x, locY=y, name=name, rotation=0, textJustification=0)
    prop_font(o)
    o.open("PropColor")
    o.defn(val=color)
    o.close("PropColor")
    o.open("PropDispType")
    o.defn(ValueIfValueExist=0, val=dispmode)
    o.close("PropDispType")
    o.close("SymbolDisplayProp")


def convert(sym, o, args, timestamp):
    name = sym[1]
    props = properties(sym)
    refdes = props.get("Reference", ("U",))[0]
    value = props.get("Value", (name,))[0]
    footprint = props.get("Footprint", ("",))[0]
    if "/" in footprint or ":" in footprint:  # strip KiCad "lib:fp"
        footprint = re.split(r"[:/]", footprint)[-1]

    units = read_units(sym)
    if not units:
        sys.exit("symbol %s has no units" % name)
    homogeneous = 1 if (args.homogeneous or len(units) == 1) else 0

    o.open("Package")
    o.defn(alphabeticNumbering=args.numbering, isHomogeneous=homogeneous,
           name=name, pcbFootprint=footprint, pcbLib="", refdesPrefix=refdes,
           timestamp=timestamp, timezone=0)

    for u in units:
        # per-section origin: top-left corner of that section's body outline
        xs = [c for ln in u.lines for c in (ln[0], ln[2])]
        ys = [c for ln in u.lines for c in (ln[1], ln[3])]
        if not xs:  # no graphics: fall back to the pin extents
            xs = [p.x for p in u.pins]
            ys = [p.y for p in u.pins]
        x0, ytop = min(xs), max(ys)

        def X(mm):
            return int(round((mm - x0) / MM_PER_UNIT))

        def Y(mm):
            return int(round((ytop - mm) / MM_PER_UNIT))

        o.open("LibPart")
        o.defn(CellName=name if args.cellname == "same" else
               "%s_%d" % (name, u.index))
        o.open("NormalView")
        o.defn(suffix=".Normal")

        ref_at = props.get("Reference", (None, None))[1]
        val_at = props.get("Value", (None, None))[1]
        if ref_at is not None:
            a = kid(ref_at, "at")
            display_prop(o, "Part Reference", X(float(a[1])), Y(float(a[2])))
        if val_at is not None:
            a = kid(val_at, "at")
            display_prop(o, "Value", X(float(a[1])), Y(float(a[2])))
        for pname, (pval, pnode) in props.items():
            if pname in ("Reference", "Value") or pname.startswith("ki_"):
                continue
            o.open("SymbolUserProp")
            o.defn(name=pname, val=pval)
            o.close("SymbolUserProp")

        o.open("SymbolColor")
        o.defn(val=48)
        o.close("SymbolColor")
        o.open("SymbolBBox")
        o.defn(x1=X(min(xs)), x2=X(max(xs)), y1=Y(max(ys)), y2=Y(min(ys)))
        o.close("SymbolBBox")
        o.flag("IsPinNumbersVisible", 1)
        o.flag("IsPinNamesRotated", 0)
        o.flag("IsPinNamesVisible", 1)
        o.open("ContentsLibName")
        o.defn(name="")
        o.close("ContentsLibName")
        o.open("ContentsViewName")
        o.defn(name="")
        o.close("ContentsViewName")
        o.open("ContentsViewType")
        o.defn(type=0)
        o.close("ContentsViewType")
        o.open("PartValue")
        o.defn(name=value)
        o.close("PartValue")
        o.open("Reference")
        o.defn(name=refdes)
        o.close("Reference")

        for x1, y1, x2, y2 in u.lines:
            o.open("Line")
            o.defn(lineStyle=0, lineWidth=0,
                   x1=X(x1), x2=X(x2), y1=Y(y1), y2=Y(y2))
            o.close("Line")

        # pins, sorted left column top-down then right column top-down
        def sortkey(p):
            side = 0 if p.angle == 0 else 1
            return (side, -p.y, p.x)

        pins = sorted(u.pins, key=sortkey)
        for pos, p in enumerate(pins):
            dx = {0: 1.0, 180: -1.0}.get(p.angle, 0.0)
            dy = {90: 1.0, 270: -1.0}.get(p.angle, 0.0)
            bx = p.x + dx * p.length          # body end
            by = p.y + dy * p.length
            ptype = PIN_TYPE.get(p.etype, 4)
            o.open("SymbolPinScalar")
            o.defn(hotptX=X(p.x), hotptY=Y(p.y), name=p.name, position=pos,
                   startX=X(bx), startY=Y(by), type=ptype, visible=1)
            o.flag("IsLong", 1)
            o.flag("IsClock", 0)
            o.flag("IsDot", 0)
            o.flag("IsLeftPointing", 0)
            o.flag("IsRightPointing", 0)
            o.flag("IsNetStyle", 0)
            o.flag("IsNoConnect", 1 if p.etype == "no_connect" else 0)
            o.flag("IsGlobal", 0)
            o.flag("IsNumberVisible", 1)
            o.close("SymbolPinScalar")

        o.close("NormalView")

        o.open("PhysicalPart")
        o.leaf("Defn")
        for pos, p in enumerate(pins):
            o.open("PinNumber")
            o.defn(number=p.number, position=pos)
            o.close("PinNumber")
        o.close("PhysicalPart")
        o.close("LibPart")

    o.close("Package")
    return len(units), sum(len(u.pins) for u in units)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="input .kicad_sym")
    ap.add_argument("-o", "--output", help="output .xml (default: alongside input)")
    ap.add_argument("--symbol", help="convert only this symbol name")
    ap.add_argument("--olb-path", help="value of the library <Defn name=...>")
    ap.add_argument("--numbering", type=int, default=1, choices=(0, 1, 2),
                    help="package section numbering: 1 alphabetic (U1A..), "
                         "2 numeric (default: 1)")
    ap.add_argument("--homogeneous", action="store_true",
                    help="mark the package homogeneous even with several units")
    ap.add_argument("--cellname", choices=("same", "indexed"), default="same",
                    help="CellName of each section: identical to the package "
                         "name (default) or suffixed with the section index")
    ap.add_argument("--xsd", default=r"c:\cadence\spb_17.4\tools\capture\tclscripts\capdb\olb.xsd")
    args = ap.parse_args()

    out_path = args.output or os.path.splitext(args.source)[0] + ".xml"
    olb = args.olb_path or os.path.splitext(os.path.basename(out_path))[0] + ".olb"
    timestamp = int(os.path.getmtime(args.source))

    symbols = read_symbol(args.source, args.symbol)
    with open(out_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n')
        fh.write('<Lib xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                 'xsi:noNamespaceSchemaLocation=%s>\n' % quoteattr(args.xsd))
        o = Out(fh)
        o.d = 1
        o.leaf("Defn", name=olb)
        write_default_values(o)
        total = []
        for sym in symbols:
            total.append((sym[1],) + convert(sym, o, args, timestamp))
        fh.write("</Lib>\n")

    for nm, nsec, npin in total:
        print("%s: %d section(s), %d pins" % (nm, nsec, npin))
    print("wrote %s" % out_path)


if __name__ == "__main__":
    main()
