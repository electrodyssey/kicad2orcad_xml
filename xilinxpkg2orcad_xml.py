#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
"""Build an OrCAD Capture library XML (olb.xsd dialect) from a Xilinx package
pinout CSV, one schematic section per I/O bank.

    python3 xilinxpkg2orcad_xml.py xcku060ffva1156pkg.csv \
        --part XCKU060-2FFVA1156I --footprint FFVA1156_AMD -o xcku060.xml

The CSV is the "package pin file" AMD/Xilinx ships next to UG575:
    Pin, Pin Name, Memory Byte Group, Bank, I/O Type, Super Logic Region, No-Connect

Sections produced, in this order:
    bank 0 (CONFIG)  ->  one section
    HP I/O banks     ->  one section each, ascending bank number
    HR I/O banks     ->  one section each, ascending bank number
    GTH transceiver quads -> one section each
    system monitor / miscellaneous
    MGT supplies, core supplies
    ground (split over --gnd-sections sections)

Each bank section carries its I/O on the left, its VREF/VCCO on the right, a
"BANK nn (HP)" caption inside the body, and Bank / IO Type part properties so
the sections can be told apart in Capture without reading the pin names.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kicad2orcad_xml import Out, write_default_values, display_prop
except ImportError:
    sys.exit("kicad2orcad_xml.py must sit next to this script (shared XML writer)")

# ------------------------------------------------------------------ geometry
# All values in OrCAD units: 1 unit = 10 mil.
PITCH = 10          # 100 mil between pins
PIN_LEN = 30        # 300 mil stub
CHAR_W = 5          # nominal width of one character of the default font
TOP_MARGIN = 30     # body top edge -> first pin row (leaves room for caption)
BOT_MARGIN = 20     # last pin row -> body bottom edge
MIN_BODY_W = 200

# ------------------------------------------------------------------ pin types
#   0 Input, 1 Bidirectional, 2 Output, 3 OpenCollector,
#   4 Passive, 5 ThreeState, 6 OpenEmitter, 7 Power
INPUT, BIDI, OUTPUT, OC, PASSIVE, TRISTATE, OE, POWER = range(8)

# First match wins.  The MGT lane prefix carries a family letter -- GTX pins
# are MGTXTX*/MGTXRX*, GTH pins MGTHTX*/MGTHRX*, GTY pins MGTYTX*/MGTYRX*.
TYPE_RULES = [
    (r"^IO_", BIDI),
    (r"^MGT[A-Z]?TX", OUTPUT),
    (r"^MGT[A-Z]?RX", INPUT),
    (r"^MGTREFCLK", INPUT),
    (r"^(MGTRREF|MGTAVTTRCAL)", PASSIVE),
    (r"^(VCC|GND|MGTAVCC|MGTAVTT|MGTVCCAUX|VBATT|VREF)", POWER),
    (r"^DX[PN]", PASSIVE),
    (r"^V[PN](_|$)", INPUT),
    (r"^TDO(_|$)", OUTPUT),
    (r"^(TCK|TDI|TMS|M0|M1|M2|CFGBVS|PUDC_B|PROGRAM_B|POR_OVERRIDE)", INPUT),
    (r"^(INIT_B|DONE|CCLK|RDWR_FCS_B|D0\d)", BIDI),
]
TYPE_RULES = [(re.compile(p), t) for p, t in TYPE_RULES]


def pin_type(name):
    for rx, t in TYPE_RULES:
        if rx.match(name):
            return t
    return PASSIVE


# ------------------------------------------------------------------ ordering
# UltraScale:  IO_L24P_T3U_N10_44        7 series:  IO_L11P_T1_SRCC_13
#              IO_T3U_N12_44                        IO_0_13 / IO_25_VRP_32
IO_US = re.compile(r"^IO_(?:L(\d+)([PN])_)?T(\d)([UL])_N(\d+)")
IO_7P = re.compile(r"^IO_L(\d+)([PN])_T(\d)_")
IO_7S = re.compile(r"^IO_(\d+)_")


def io_key(p):
    """Order bank I/O the way Xilinx numbers it.

    UltraScale banks are ordered by byte group then nibble; 7 series banks by
    the pin index within the bank, where the differential pairs L1..L24 sit
    between the single-ended IO_0 and IO_25.  A bank never mixes the two.
    """
    m = IO_US.match(p.name)
    if m:
        _, _, t, ul, n = m.groups()
        return (int(t), 0 if ul == "L" else 1, int(n), p.name)
    m = IO_7P.match(p.name)
    if m:
        pair, pol, _ = m.groups()
        return (int(pair), 0 if pol == "P" else 1, 0, p.name)
    m = IO_7S.match(p.name)
    if m:
        return (int(m.group(1)), 0, 0, p.name)
    return (99, 99, 99, p.name)


def pkg_key(p):
    """Natural order of a BGA ball designator: A1 < A2 < ... < B1 < ... < AA1."""
    m = re.match(r"^([A-Z]+)(\d+)$", p.number)
    if not m:
        return (9, p.number, 0)
    return (len(m.group(1)), m.group(1), int(m.group(2)))


# MGTREFCLK0P_127 numbers the lane before the polarity, MGTHRXP0_127 after it.
MGT_CLK = re.compile(r"^MGTREFCLK(\d+)([PN])_")
MGT_LANE = re.compile(r"^MGT[A-Z]?(RX|TX)([PN])(\d+)_")


def is_mgt_tx(name):
    m = MGT_LANE.match(name)
    return bool(m) and m.group(1) == "TX"


def mgt_key(p):
    m = MGT_CLK.match(p.name)
    if m:
        return (0, int(m.group(1)), 0 if m.group(2) == "P" else 1, p.name)
    m = MGT_LANE.match(p.name)
    if m:
        kind, pol, idx = m.groups()
        return (1 if kind == "RX" else 2, int(idx),
                0 if pol == "P" else 1, p.name)
    return (9, 9, 9, p.name)   # MGTRREF, MGTAVTTRCAL -- bank-tied on 7 series


def name_then_pkg(p):
    return (p.name,) + pkg_key(p)


# ------------------------------------------------------------------ model
class Pin:
    __slots__ = ("number", "name", "bank", "iotype", "bytegroup")

    def __init__(self, number, name, bank, iotype, bytegroup):
        self.number = number
        self.name = name
        self.bank = bank
        self.iotype = iotype
        self.bytegroup = bytegroup


class Section:
    def __init__(self, suffix, caption, left, right, props=None):
        self.suffix = suffix        # appended to the part name -> CellName
        self.caption = caption      # text drawn inside the body
        self.left = left
        self.right = right
        self.props = props or {}    # extra SymbolUserProp entries

    @property
    def pins(self):
        return self.left + self.right


def read_pinout(path):
    """Read a Xilinx package pin file, comma- or whitespace-delimited.

    Columns are located by header name, not by position: 7 series files carry
    a "VCCAUX Group" column that UltraScale ones do not, and the two families
    order the remaining columns differently.
    """
    text = open(path, encoding="utf-8-sig", errors="replace").read()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    head = next((i for i, ln in enumerate(lines)
                 if re.match(r"\s*Pin\s*[,\t]|\s*Pin\s{2,}Pin Name", ln)), None)
    if head is None:
        sys.exit("%s: no 'Pin / Pin Name / Bank / I/O Type' header row" % path)

    comma = "," in lines[head]

    def split(ln):
        if comma:
            return [c.strip() for c in next(csv.reader([ln]))]
        return [c.strip() for c in re.split(r"\s{2,}|\t", ln.strip())]

    cols = {name.lower(): i for i, name in enumerate(split(lines[head]))}

    def col(row, *names, **kw):
        for n in names:
            i = cols.get(n)
            if i is not None and i < len(row):
                return row[i]
        return kw.get("default", "NA")

    pins = []
    for ln in lines[head + 1:]:
        if not ln.strip() or ln.lstrip().startswith("Total Number of Pins"):
            continue
        r = split(ln)
        if not r or not r[0]:
            continue
        pins.append(Pin(col(r, "pin"), col(r, "pin name"), col(r, "bank"),
                        col(r, "i/o type", "io type"),
                        col(r, "memory byte group")))
    return pins


# ------------------------------------------------------------------ sectioning
def chunk(seq, n):
    """Split seq into n as-equal-as-possible chunks."""
    out, start = [], 0
    for i in range(n):
        stop = len(seq) * (i + 1) // n
        out.append(seq[start:stop])
        start = stop
    return out


def build_sections(pins, args):
    by_bank = defaultdict(list)
    loose = []
    for p in pins:
        if p.bank and p.bank != "NA":
            by_bank[p.bank].append(p)
        else:
            loose.append(p)

    used = set()
    sections = []

    def take(seq):
        used.update(id(p) for p in seq)
        return seq

    def bank_section(bank, kind, caption):
        bp = by_bank[bank]
        io = sorted([p for p in bp if p.name.startswith("IO_")], key=io_key)
        sup = sorted([p for p in bp if not p.name.startswith("IO_")],
                     key=name_then_pkg)
        if not io:                       # bank 0: config signals, not IO_*
            io = sorted([p for p in bp if not p.name.startswith("VCCO")],
                        key=name_then_pkg)
            sup = sorted([p for p in bp if p.name.startswith("VCCO")],
                         key=pkg_key)
        return Section("B%s%s" % (bank, kind), caption, take(io), take(sup),
                       {"Bank": bank, "IO Type": kind})

    # --- bank 0 (configuration) ------------------------------------------
    if "0" in by_bank:
        sections.append(bank_section("0", "CFG", "BANK 0  CONFIGURATION"))

    # --- HP then HR I/O banks --------------------------------------------
    def io_banks(kind):
        return sorted({p.bank for p in pins if p.iotype == kind}, key=int)

    for kind in ("HP", "HR"):
        for bank in io_banks(kind):
            sections.append(bank_section(
                bank, kind, "BANK %s  %s I/O" % (bank, kind)))

    # --- transceiver quads (GTX, GTH, GTP, GTY, ...) ----------------------
    quads = sorted({(p.bank, p.iotype) for p in pins
                    if re.match(r"^GT[A-Z]$", p.iotype)},
                   key=lambda bk: int(bk[0]))
    for bank, kind in quads:
        bp = sorted(by_bank[bank], key=mgt_key)
        left = [p for p in bp if not is_mgt_tx(p.name)]
        right = [p for p in bp if is_mgt_tx(p.name)]
        sections.append(Section("Q%s" % bank, "%s QUAD %s" % (kind, bank),
                                take(left), take(right),
                                {"Bank": bank, "IO Type": kind}))

    # --- everything without a bank ---------------------------------------
    def pick(pred, seq=None):
        src = loose if seq is None else seq
        return sorted([p for p in src if pred(p) and id(p) not in used],
                      key=pkg_key)

    # On 7 series these sit in bank 0 and are absorbed by the config section.
    sysmon = pick(lambda p: re.match(
        r"^(DXP|DXN|VP|VN|VREFP|VREFN|VCCADC|GNDADC|POR_OVERRIDE"
        r"|VBATT|VCCBATT)(_\d+)?$", p.name))
    if sysmon:
        half = (len(sysmon) + 1) // 2
        sections.append(Section("MISC", "SYSTEM MONITOR / MISC",
                                take(sysmon[:half]), take(sysmon[half:]),
                                {"IO Type": "MISC"}))

    mgt_l = pick(lambda p: p.name.startswith(("MGTAVCC", "MGTVCCAUX")))
    mgt_r = pick(lambda p: p.name.startswith(("MGTAVTT", "MGTRREF")))
    if mgt_l or mgt_r:
        sections.append(Section("MGTPWR", "TRANSCEIVER SUPPLIES",
                                take(mgt_l), take(mgt_r),
                                {"IO Type": "POWER"}))

    core_l = take(pick(lambda p: p.name.startswith("VCCINT")))
    core_r = take(pick(lambda p: p.name.startswith("VCC")))  # AUX, BRAM, rest
    if core_l or core_r:
        sections.append(Section("COREPWR", "CORE SUPPLIES", core_l, core_r,
                                {"IO Type": "POWER"}))

    gnd = pick(lambda p: p.name.startswith("GND"))
    if gnd:
        parts = chunk(gnd, max(1, args.gnd_sections))
        for i, g in enumerate(parts, 1):
            cap = "GROUND" if len(parts) == 1 else \
                "GROUND  (%d of %d)" % (i, len(parts))
            half = (len(g) + 1) // 2
            sections.append(Section("GND%d" % i, cap,
                                    take(g[:half]), take(g[half:]),
                                    {"IO Type": "POWER"}))

    left_over = [p for p in pins if id(p) not in used]
    if left_over:
        print("warning: %d pin(s) matched no section, collected in _OTHER: %s"
              % (len(left_over), ", ".join(sorted({p.name for p in left_over}))),
              file=sys.stderr)
        lo = sorted(left_over, key=pkg_key)
        half = (len(lo) + 1) // 2
        sections.append(Section("OTHER", "UNCLASSIFIED",
                                take(lo[:half]), take(lo[half:])))
    return sections


# ------------------------------------------------------------------ emitting
def emit_section(o, sec, part, value, footprint, args):
    rows = max(len(sec.left), len(sec.right))
    wl = max([len(p.name) for p in sec.left] or [0])
    wr = max([len(p.name) for p in sec.right] or [0])
    body_w = max(MIN_BODY_W,
                 ((CHAR_W * (wl + wr) + 60) + PITCH - 1) // PITCH * PITCH)
    x_l, x_r = PIN_LEN, PIN_LEN + body_w
    y_bot = TOP_MARGIN + (rows - 1) * PITCH + BOT_MARGIN

    o.open("LibPart")
    o.defn(CellName="%s_%s" % (part, sec.suffix))
    o.open("NormalView")
    o.defn(suffix=".Normal")

    display_prop(o, "Part Reference", x_l, -25)
    display_prop(o, "Value", x_l, -13)
    props = {"PCB Footprint": footprint} if footprint else {}
    if args.bank_props:
        props.update(sec.props)
    for k, v in props.items():
        o.open("SymbolUserProp")
        o.defn(name=k, val=v)
        o.close("SymbolUserProp")

    o.open("SymbolColor")
    o.defn(val=48)
    o.close("SymbolColor")
    o.open("SymbolBBox")
    o.defn(x1=x_l, x2=x_r, y1=0, y2=y_bot)
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
    o.defn(name=args.refdes)
    o.close("Reference")

    for x1, y1, x2, y2 in ((x_l, 0, x_r, 0), (x_r, 0, x_r, y_bot),
                           (x_r, y_bot, x_l, y_bot), (x_l, y_bot, x_l, 0)):
        o.open("Line")
        o.defn(lineStyle=0, lineWidth=0, x1=x1, x2=x2, y1=y1, y2=y2)
        o.close("Line")

    cx, cy = x_l + PITCH, 8
    o.open("CommentText")
    o.defn(locX=cx, locY=cy, name=sec.caption, textJustification=0,
           x1=cx, x2=cx + CHAR_W * len(sec.caption), y1=cy, y2=cy + PITCH)
    o.open("TextFont")
    o.defn(charset=0, escapement=0, height=-9, italic=0, name="Courier New",
           orientation=0, weight=400, width=CHAR_W)
    o.close("TextFont")
    o.close("CommentText")

    placed = []
    for i, p in enumerate(sec.left):
        placed.append((p, 0, x_l, TOP_MARGIN + i * PITCH))
    for i, p in enumerate(sec.right):
        placed.append((p, 1, x_r, TOP_MARGIN + i * PITCH))

    for pos, (p, side, bx, y) in enumerate(placed):
        hot = bx - PIN_LEN if side == 0 else bx + PIN_LEN
        o.open("SymbolPinScalar")
        o.defn(hotptX=hot, hotptY=y, name=p.name, position=pos,
               startX=bx, startY=y, type=pin_type(p.name), visible=1)
        o.flag("IsLong", 1)
        o.flag("IsClock", 0)
        o.flag("IsDot", 0)
        o.flag("IsLeftPointing", 0)
        o.flag("IsRightPointing", 0)
        o.flag("IsNetStyle", 0)
        o.flag("IsNoConnect", 0)
        o.flag("IsGlobal", 0)
        o.flag("IsNumberVisible", 1)
        o.close("SymbolPinScalar")
    o.close("NormalView")

    o.open("PhysicalPart")
    o.leaf("Defn")
    for pos, (p, _, _, _) in enumerate(placed):
        o.open("PinNumber")
        o.defn(number=p.number, position=pos)
        o.close("PinNumber")
    o.close("PhysicalPart")
    o.close("LibPart")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pinout", help="Xilinx package pin CSV")
    ap.add_argument("-o", "--output", help="output .xml")
    ap.add_argument("--part", help="part name (default: CSV device/package)")
    ap.add_argument("--value", help="part value (default: same as --part)")
    ap.add_argument("--footprint", default="", help="PCB Footprint property")
    ap.add_argument("--refdes", default="U", help="reference designator prefix")
    ap.add_argument("--gnd-sections", type=int, default=3,
                    help="split the ground pins over this many sections "
                         "(default: 3)")
    ap.add_argument("--no-bank-props", dest="bank_props", action="store_false",
                    help="do not emit the Bank / IO Type part properties")
    ap.add_argument("--numbering", type=int, default=1, choices=(0, 1, 2),
                    help="section numbering: 1 alphabetic (U1A..), 2 numeric")
    ap.add_argument("--olb-path", help="value of the library <Defn name=...>")
    ap.add_argument("--xsd",
                    default=r"c:\cadence\spb_17.4\tools\capture\tclscripts"
                            r"\capdb\olb.xsd")
    args = ap.parse_args()

    pins = read_pinout(args.pinout)
    if not pins:
        sys.exit("%s: no pins" % args.pinout)
    part = args.part or os.path.splitext(os.path.basename(args.pinout))[0].upper()
    value = args.value or part
    out_path = args.output or (part + ".xml")
    olb = args.olb_path or os.path.splitext(os.path.basename(out_path))[0] + ".olb"
    timestamp = int(os.path.getmtime(args.pinout))

    sections = build_sections(pins, args)

    with open(out_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n')
        fh.write('<Lib xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                 'xsi:noNamespaceSchemaLocation="%s">\n' % args.xsd)
        o = Out(fh)
        o.d = 1
        o.leaf("Defn", name=olb)
        write_default_values(o)
        o.open("Package")
        o.defn(alphabeticNumbering=args.numbering, isHomogeneous=0, name=part,
               pcbFootprint=args.footprint, pcbLib="", refdesPrefix=args.refdes,
               timestamp=timestamp, timezone=0)
        for sec in sections:
            emit_section(o, sec, part, value, args.footprint, args)
        o.close("Package")
        fh.write("</Lib>\n")

    for i, sec in enumerate(sections, 1):
        print("  %2d  %-28s %-24s %4d pins"
              % (i, "%s_%s" % (part, sec.suffix), sec.caption, len(sec.pins)))
    print("%s: %d sections, %d pins -> %s"
          % (part, len(sections), sum(len(s.pins) for s in sections), out_path))


if __name__ == "__main__":
    main()
