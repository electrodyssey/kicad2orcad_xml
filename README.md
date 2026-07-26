# kicad2orcad-xml

Convert a KiCad 6/7 symbol library (`.kicad_sym`) into an OrCAD Capture
library XML (`olb.xsd` dialect) that Capture can turn into an `.OLB` via
**File > Import > Library XML**.

Pure Python 3, standard library only — no KiCad, no Capture, no dependencies.

```sh
python3 kicad2orcad_xml.py xcku060.kicad_sym -o xcku060.xml --cellname indexed
```

Then in Capture: `File > Import > Library XML`, select the XML, and accept or
choose the output OLB path.

## What it converts

| KiCad | OrCAD |
| --- | --- |
| symbol | `Package` |
| unit | section (`LibPart` + `NormalView` + `PhysicalPart`) |
| pin `(at x y angle)` | `SymbolPinScalar/@hotpt*` (wire end) and `@start*` (body end) |
| pin number | `PhysicalPart/PinNumber@number`, keyed by `@position` |
| polyline / rectangle | `Line` |
| `Reference` / `Value` property | `SymbolDisplayProp` + `Reference` / `PartValue` |
| `Footprint` property | `Package@pcbFootprint` |
| other properties | `SymbolUserProp` (KiCad-internal `ki_*` are dropped) |

Geometry: 1 OrCAD unit = 10 mil = 0.254 mm, Y axis inverted (KiCad Y grows up,
OrCAD Y grows down). Each section is placed with its own body outline's
top-left corner at the origin.

Pin electrical type maps to the OrCAD `PortType` enum:

| KiCad | OrCAD |
| --- | --- |
| `input` | 0 Input |
| `bidirectional` | 1 Bidirectional |
| `output` | 2 Output |
| `open_collector` | 3 Open Collector |
| `passive`, `free`, `unspecified`, `no_connect` | 4 Passive |
| `tri_state` | 5 3 State |
| `open_emitter` | 6 Open Emitter |
| `power_in`, `power_out` | 7 Power |

`no_connect` additionally sets `IsNoConnect`.

## Options

| Flag | Meaning |
| --- | --- |
| `-o, --output` | output path (default: input name with `.xml`) |
| `--symbol NAME` | convert a single symbol instead of the whole library |
| `--olb-path` | value written to the library `<Defn name=...>` |
| `--cellname same\|indexed` | section `CellName`: package name, or suffixed `_1`, `_2`, … |
| `--numbering 1\|2` | section numbering: 1 alphabetic (`U1A`…), 2 numeric |
| `--homogeneous` | mark a multi-unit package homogeneous |
| `--xsd` | schema path written into the `xsi:noNamespaceSchemaLocation` attribute |

**Use `--cellname indexed` for multi-unit symbols.** Capture rejects an import
whose sections all carry the same cell name ("a cell with that name already
exists"). `same` is kept for single-unit libraries and for round-tripping
libraries exported by Capture itself, which reuse the package name.

## Known limitations

- Multi-unit symbols are emitted as a heterogeneous package
  (`isHomogeneous="0"`, one `LibPart` per unit). Identical repeated units are
  not collapsed into a homogeneous package.
- OrCAD has no distinct power-output pin type, so `power_out` becomes Power.
- DeMorgan alternate body styles are ignored; only the primary body is used.
- Arcs, circles, bezier curves and text items in the symbol body are not
  converted — only polylines and rectangles.
- Bus pins (`SymbolPinBus`) are not emitted; every pin becomes a scalar pin.
- Hidden pins are exported as visible.

## `xilinxpkg2orcad_xml.py` — banked FPGA symbols from a package pinout

A KiCad FPGA symbol usually splits its units by pin *count*, not by I/O bank,
so a converted symbol has every bank smeared across several sections. This
companion script skips KiCad entirely and builds the OrCAD XML straight from
the AMD/Xilinx package pin file, one section per bank:

```sh
python3 xilinxpkg2orcad_xml.py xcku060ffva1156pkg.csv \
    --part XCKU060-2FFVA1156I --footprint FFVA1156_AMD -o xcku060.xml

python3 xilinxpkg2orcad_xml.py xc7k325tffg900pkg.txt \
    --part XC7K325T-2FFG900I --footprint FFG900 -o xc7k325t.xml
```

Both UltraScale and 7 series parts work, in either the `.csv` or the
whitespace-aligned `.txt` form — the two families order their columns
differently and 7 series files carry an extra `VCCAUX Group`, so columns are
located by header name rather than by position. Package files live under
`https://www.xilinx.com/support/packagefiles/`; 7 series parts are published
as `.txt` only.

Section order: bank 0 (configuration), HP I/O banks ascending, HR I/O banks
ascending, transceiver quads (GTX / GTH / GTY / GTP), system monitor / misc,
transceiver supplies, core supplies, ground. On 7 series the system monitor
pins belong to bank 0 and are absorbed by the configuration section rather
than getting one of their own. Each section gets

- a `CellName` suffix naming the bank and its type — `..._B44HP`, `..._B64HR`,
  `..._Q227`, `..._COREPWR`;
- a caption drawn inside the body (`BANK 44  HP I/O`);
- `Bank` and `IO Type` part properties (`--no-bank-props` to omit them).

Bank I/O goes on the left, `VREF`/`VCCO` on the right. I/O is ordered the way
the family numbers it: UltraScale by byte group then nibble (`T0L` `N0` …
`T3U` `N12`, P before N), 7 series by index within the bank (`IO_0`, then
`L1P`/`L1N` … `L24P`/`L24N`, then `IO_25`). Transceiver quads put the
reference clocks and RX lanes on the left, TX lanes on the right. Ground is
spread over `--gnd-sections` sections (3 by default) so no single section is
metres tall.

Pin electrical types come from the pin name (`IO_*` bidirectional,
`MGT?TX*` output, `MGT?RX*`/`MGTREFCLK*` input, `VCC*`/`GND*`/`VREF*` power,
`MGTRREF`/`MGTAVTTRCAL`/`DXP`/`DXN` passive, JTAG and mode pins per the
configuration user guide), not from the pinout file — it has no type column.

It reuses the XML writer from `kicad2orcad_xml.py`, so keep the two files
together.

## Format reference

The `olb.xsd` schema ships with Capture at
`C:\Cadence\SPB_17.x\tools\capture\tclscripts\capDB\olb.xsd`. Useful
cross-references while working on this format:

- [fjullien/orlib2ki](https://github.com/fjullien/orlib2ki) — the opposite
  direction (OrCAD XML → KiCad), with a genuine Capture library export in
  `sample/led.xml`.
- [Werni2A/OpenOrCadParser](https://github.com/Werni2A/OpenOrCadParser) —
  binary `.OLB`/`.DSN` parser; `src/Enums/PortType.hpp` documents the pin-type
  enum used above.

## License

Public domain, via [the Unlicense](https://unlicense.org) — see `LICENSE`.
Do whatever you want with it; no attribution required.
