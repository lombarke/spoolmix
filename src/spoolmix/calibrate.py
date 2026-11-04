"""Calibration: swatch-grid 3MF generation, measurement storage and model fitting.

Workflow
  1. new_run(): pick up to 4 in-stock filaments (= Orca filaments 1..4) and build a
     grid of swatches: each filament pure, every pair at 30/70..70/30, and each
     triple at equal and dominant weights.
  2. write_3mf(): copy a Snapmaker Orca project saved by the user (printer, process
     and filament presets exactly as theirs), replace its objects with the swatch
     tiles, set filament colors, and write one mixed filament per swatch into
     `mixed_filament_definitions`. Each tile's `extruder` is its virtual filament ID.
  3. The user prints it and enters measured colors (colorimeter, or a photo with a
     gray card in the web UI).
  4. fit(): per-filament printed colors from the pure swatches, then the TD
     model's alpha by minimizing mean ΔE over the mixed swatches.

Mixed-filament row format (Snapmaker Orca MixedFilamentManager::serialize_custom_entries):
  a,b,enabled,custom,mix_b_percent,pointillism,g<ids>,w<weights>,m<mode>,z0,xa0,xb0,d0,o0,u<stable_id>,cm<ui_mode>
Rows are numbered as virtual filaments num_physical+1.. in order (enabled rows only).
"""

from __future__ import annotations

import datetime as dt
import io
import itertools
import json
import re
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
from ruamel.yaml import YAML

from spoolmix.color import RGB, delta_e2000, parse_hex, srgb_to_lab, to_hex
from spoolmix.model import lerp_batch, mix_sequential_batch

CALIBRATION_FILE = "calibration.local.yaml"
RUNS_DIR = "calibration"
TEMPLATE_FILE = "template.3mf"

# Calibration swatches are blocks measured on their side walls: layered mixing shows on
# walls (every layer is visible there), while a top surface shows mostly the last layers.
TILE = 12.0  # mm, block width
GAP = 6.0  # room to pick blocks off the plate
TILE_HEIGHT = 12.0
COLS = 8
WALLS = "walls"  # run.style for side-wall blocks; older runs are flat "tiles" measured from the top
LABEL_PIX, LABEL_HEIGHT = 0.8, 0.6  # raised 3x5 pixel-font label on each block's top
BED_CENTER = (150.0, 118.0)  # keeps clear of the U1's default prime tower at x 13-43, y 214+


# --- data -------------------------------------------------------------------------


@dataclass
class CalFilament:
    slot: int  # 1-based Orca filament number
    key: str  # e.g. spoolman:4
    label: str
    hex: str
    td: float | None
    material: str | None = None  # so the grid gets the template's preset for this material


@dataclass
class Swatch:
    id: str  # grid label, e.g. "B3"
    slots: list[int]  # 1-based, ascending
    weights: list[int]  # percent, aligned with slots
    predicted: str = ""  # Orca preview hex
    measured: str | None = None
    row: int = 0
    col: int = 0

    @property
    def is_mix(self) -> bool:
        return len(self.slots) > 1

    @property
    def label(self) -> str:
        return " · ".join(f"F{s} {w}%" for s, w in zip(self.slots, self.weights))


@dataclass
class CalRun:
    id: str
    created: str
    filaments: list[CalFilament]
    swatches: list[Swatch]
    tile: float = TILE
    height: float = TILE_HEIGHT
    notes: str = ""
    fitted: bool = False
    template: str | None = None  # printer template id the grid was built from
    style: str = "tiles"  # WALLS for side-wall blocks (current); "tiles" = old flat top-surface grids

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> CalRun:
        return cls(
            id=d["id"], created=d["created"],
            filaments=[CalFilament(**f) for f in d["filaments"]],
            swatches=[Swatch(**s) for s in d["swatches"]],
            tile=d.get("tile", TILE), height=d.get("height", TILE_HEIGHT),
            notes=d.get("notes", ""), fitted=d.get("fitted", False), template=d.get("template"),
            style=d.get("style", "tiles"),
        )

    @property
    def walls(self) -> bool:
        return self.style == WALLS

    @property
    def measured_count(self) -> int:
        return sum(1 for s in self.swatches if s.measured)


# --- grid -------------------------------------------------------------------------


def design_grid(n_filaments: int, pair_ratios: Sequence[int] = (30, 40, 50, 60, 70), triples: bool = True) -> list[Swatch]:
    """Swatch list for filaments 1..n. pair_ratios = share of the higher-numbered filament."""
    sw: list[Swatch] = [Swatch("", [s], [100]) for s in range(1, n_filaments + 1)]
    for a, b in itertools.combinations(range(1, n_filaments + 1), 2):
        for p in pair_ratios:
            sw.append(Swatch("", [a, b], [100 - p, p]))
    if triples and n_filaments >= 3:
        for combo in itertools.combinations(range(1, n_filaments + 1), 3):
            for w in ([34, 33, 33], [50, 25, 25], [25, 50, 25], [25, 25, 50]):
                sw.append(Swatch("", list(combo), list(w)))
    for i, s in enumerate(sw):
        s.row, s.col = divmod(i, COLS)
        s.id = f"{chr(ord('A') + s.row)}{s.col + 1}"
    return sw


def predict(swatch: Swatch, filaments: Sequence[CalFilament]) -> RGB:
    by_slot = {f.slot: f for f in filaments}
    cols = [parse_hex(by_slot[s].hex) for s in swatch.slots]
    r, g, b = mix_sequential_batch(cols, np.array([swatch.weights], dtype=np.float64))[0]
    return int(r), int(g), int(b)


def new_run(filaments: Sequence[CalFilament], data_dir: Path, triples: bool = True, notes: str = "",
            template: str | None = None) -> CalRun:
    if not 2 <= len(filaments) <= 4:
        raise ValueError("choose 2 to 4 filaments")
    sw = design_grid(len(filaments), triples=triples)
    for s in sw:
        s.predicted = to_hex(predict(s, filaments))
    now = dt.datetime.now()
    run = CalRun(id=now.strftime("%Y%m%d-%H%M%S"), created=now.isoformat(timespec="seconds"),
                 filaments=list(filaments), swatches=sw, notes=notes, template=template, style=WALLS)
    save_run(run, data_dir)
    return run


# --- storage ----------------------------------------------------------------------


def runs_dir(data_dir: Path) -> Path:
    return data_dir / RUNS_DIR


def save_run(run: CalRun, data_dir: Path) -> None:
    d = runs_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{run.id}.json.part"
    tmp.write_text(json.dumps(run.to_json(), indent=1))
    tmp.replace(d / f"{run.id}.json")


def load_run(run_id: str, data_dir: Path) -> CalRun:
    if not re.fullmatch(r"[0-9-]+", run_id):
        raise ValueError("bad run id")
    return CalRun.from_json(json.loads((runs_dir(data_dir) / f"{run_id}.json").read_text()))


def list_runs(data_dir: Path) -> list[CalRun]:
    d = runs_dir(data_dir)
    if not d.is_dir():
        return []
    return sorted((CalRun.from_json(json.loads(p.read_text())) for p in d.glob("*.json")), key=lambda r: r.id, reverse=True)


def load_calibration(data_dir: Path) -> dict | None:
    path = data_dir / CALIBRATION_FILE
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as fh:
        return dict(YAML(typ="safe").load(fh) or {})


def save_calibration(data_dir: Path, cal: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    y = YAML()
    y.default_flow_style = False
    with open(data_dir / CALIBRATION_FILE, "w", encoding="utf-8") as fh:
        fh.write("# Fitted by spoolmix calibration. Delete this file to go back to defaults.\n")
        y.dump(cal, fh)


# --- 3MF --------------------------------------------------------------------------

_NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_MODEL_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<model unit="millimeter" xml:lang="en-US" xmlns="{_NS_CORE}" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n'
)
_OBJECTS_PATH = "3D/Objects/swatches.model"


def _uuid() -> str:
    return str(uuid.uuid4())


def _box_mesh(sx: float, sy: float, sz: float) -> str:
    x, y, z = sx / 2, sy / 2, sz / 2
    v = [(x, y, -z), (x, -y, -z), (-x, -y, -z), (-x, y, -z), (x, y, z), (-x, y, z), (-x, -y, z), (x, -y, z)]
    t = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 4, 7), (0, 7, 1), (1, 7, 6), (1, 6, 2), (2, 6, 5), (2, 5, 3), (4, 0, 3), (4, 3, 5)]
    verts = "".join(f'     <vertex x="{a:g}" y="{b:g}" z="{c:g}"/>\n' for a, b, c in v)
    tris = "".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in t)
    return f"   <mesh>\n    <vertices>\n{verts}    </vertices>\n    <triangles>\n{tris}    </triangles>\n   </mesh>\n"


FONT = {  # 3x5 pixel glyphs for block labels (rows top to bottom)
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "001", "001", "001"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    "A": ["010", "101", "111", "101", "101"], "B": ["110", "101", "110", "101", "110"],
    "C": ["111", "100", "100", "100", "111"], "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "111", "100", "111"], "F": ["111", "100", "111", "100", "100"],
    "G": ["111", "100", "101", "101", "111"], "H": ["101", "101", "111", "101", "101"],
}


def _label_mesh(text: str, top_z: float) -> str:
    """Raised label centred on a block's top face (top at top_z, block-local coordinates):
    one box per run of pixels in a glyph row, rows overlapping slightly so each glyph is solid."""
    pix = LABEL_PIX
    cols = 4 * len(text) - 1
    left, top = -cols * pix / 2, 5 * pix / 2
    z0, z1 = top_z - 0.01, top_z + LABEL_HEIGHT  # sink 0.01 mm in so it fuses with the top
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    faces = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 4, 7), (0, 7, 1), (1, 7, 6), (1, 6, 2),
             (2, 6, 5), (2, 5, 3), (4, 0, 3), (4, 3, 5)]
    for ci, ch in enumerate(text):
        for r, bits in enumerate(FONT.get(ch, FONT["0"])):
            c = 0
            while c < 3:
                if bits[c] != "1":
                    c += 1
                    continue
                start = c
                while c < 3 and bits[c] == "1":
                    c += 1
                xa, xb = left + (4 * ci + start) * pix, left + (4 * ci + c) * pix
                yb, ya = top - r * pix + (0.02 if r else 0.0), top - (r + 1) * pix
                o = len(verts)
                verts += [(xb, yb, z0), (xb, ya, z0), (xa, ya, z0), (xa, yb, z0),
                          (xb, yb, z1), (xa, yb, z1), (xa, ya, z1), (xb, ya, z1)]
                tris += [(o + a, o + b, o + d) for a, b, d in faces]
    vs = "".join(f'     <vertex x="{a:.3f}" y="{b:.3f}" z="{c:.3f}"/>\n' for a, b, c in verts)
    ts = "".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in tris)
    return f"   <mesh>\n    <vertices>\n{vs}    </vertices>\n    <triangles>\n{ts}    </triangles>\n   </mesh>\n"


def mixed_row(swatch: Swatch, stable_id: int) -> str:
    """One mixed_filament_definitions row, as Snapmaker Orca's Ratio mode writes it."""
    return mixed_row_for(swatch.slots, swatch.weights, stable_id)


def mixed_row_for(slots: Sequence[int], w: Sequence[int], stable_id: int) -> str:
    """Row for physical filaments `slots` (ascending, 1-based) at percents `w`."""
    if len(slots) == 2:
        return f"{slots[0]},{slots[1]},1,1,{w[1]},0,g,w,m2,z0,xa0,xb0,d0,o0,u{stable_id},cm0"
    ids = "".join(str(s) for s in slots) if max(slots) <= 9 else "/".join(str(s) for s in slots)
    return (f"{slots[0]},{slots[1]},1,1,50,0,g{ids},w{'/'.join(str(x) for x in w)},"
            f"m0,z0,xa0,xb0,d0,o0,u{stable_id},cm0")


@dataclass
class _Obj:
    name: str
    extruder: int
    x: float
    y: float
    sx: float
    sy: float
    sz: float
    mesh_id: int = 0
    obj_id: int = 0
    extras: dict = field(default_factory=dict)
    label: str = ""  # raised text on top (wall blocks)
    label_id: int = 0


def write_3mf(run: CalRun, template: Path, n_physical: int | None = None) -> bytes:
    """Build the calibration project from a user-saved Snapmaker Orca template."""
    with zipfile.ZipFile(template) as zin:
        settings = json.loads(zin.read("Metadata/project_settings.config"))
        keep = {
            n: zin.read(n) for n in zin.namelist()
            if n in ("Metadata/slice_info.config",) or n.startswith("Metadata/") and n.endswith((".xml",))
        }

    colours = list(settings.get("filament_colour") or [])
    n_phys = n_physical or len(colours)
    if n_phys < len(run.filaments):
        raise ValueError(f"template has {n_phys} filaments; this grid needs {len(run.filaments)}")
    for f in run.filaments:
        colours[f.slot - 1] = to_hex(parse_hex(f.hex))
    settings["filament_colour"] = colours
    from spoolmix.project3mf import match_presets_to_materials

    materials: list[str | None] = [None] * len(colours)
    for f in run.filaments:
        materials[f.slot - 1] = f.material
    match_presets_to_materials(settings, materials)  # e.g. a PLA filament in the template's PETG slot
    if isinstance(settings.get("filament_multi_colors"), list) and len(settings["filament_multi_colors"]) == len(colours):
        settings["filament_multi_colors"] = list(colours)

    mixes = [s for s in run.swatches if s.is_mix]
    settings["mixed_filament_definitions"] = ";".join(mixed_row(s, i + 1) for i, s in enumerate(mixes))
    vid = {id(s): n_phys + 1 + i for i, s in enumerate(mixes)}

    pitch = run.tile + GAP
    rows = max(s.row for s in run.swatches) + 1
    x0 = BED_CENTER[0] - (COLS - 1) * pitch / 2
    y0 = BED_CENTER[1] + (rows - 1) * pitch / 2  # row A at the back (top of the plate view)
    objs: list[_Obj] = []
    for s in run.swatches:
        ext = vid[id(s)] if s.is_mix else s.slots[0]
        objs.append(_Obj(f"{s.id} {s.label}", ext, x0 + s.col * pitch, y0 - s.row * pitch, run.tile, run.tile, run.height,
                         label=s.id if run.walls else ""))
    # Orientation marker left of A1, printed in filament 1.
    objs.append(_Obj("marker (left of A1)", 1, x0 - pitch * 0.75, y0, 6.0, 6.0, run.height / 2))
    for i, o in enumerate(objs):
        o.mesh_id, o.obj_id, o.label_id = 3 * i + 1, 3 * i + 2, 3 * i + 3

    objects_model = _MODEL_HEAD + ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
    for o in objs:
        objects_model += f'  <object id="{o.mesh_id}" p:UUID="{_uuid()}" type="model">\n{_box_mesh(o.sx, o.sy, o.sz)}  </object>\n'
        if o.label:
            objects_model += f'  <object id="{o.label_id}" p:UUID="{_uuid()}" type="model">\n{_label_mesh(o.label, o.sz / 2)}  </object>\n'
    objects_model += " </resources>\n <build/>\n</model>\n"

    main = _MODEL_HEAD + ' <metadata name="Application">Snapmaker_Orca-2.4.0</metadata>\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
    main += f' <metadata name="Title">spoolmix calibration {run.id}</metadata>\n <resources>\n'
    for o in objs:
        label_part = (f'    <component p:path="/{_OBJECTS_PATH}" objectid="{o.label_id}" p:UUID="{_uuid()}" '
                      'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n') if o.label else ""
        main += (f'  <object id="{o.obj_id}" p:UUID="{_uuid()}" type="model">\n   <components>\n'
                 f'    <component p:path="/{_OBJECTS_PATH}" objectid="{o.mesh_id}" p:UUID="{_uuid()}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
                 + label_part + '   </components>\n  </object>\n')
    main += f' </resources>\n <build p:UUID="{_uuid()}">\n'
    for o in objs:
        main += (f'  <item objectid="{o.obj_id}" p:UUID="{_uuid()}" '
                 f'transform="1 0 0 0 1 0 0 0 1 {o.x:.3f} {o.y:.3f} {o.sz / 2:g}" printable="1"/>\n')
    main += " </build>\n</model>\n"

    cfg = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
    for o in objs:
        label_part = (f'    <part id="{o.label_id}" subtype="normal_part">\n      <metadata key="name" value="label {_xml(o.label)}"/>\n'
                      '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n') if o.label else ""
        cfg += (f'  <object id="{o.obj_id}">\n    <metadata key="name" value="{_xml(o.name)}"/>\n'
                f'    <metadata key="extruder" value="{o.extruder}"/>\n'
                f'    <part id="{o.mesh_id}" subtype="normal_part">\n      <metadata key="name" value="{_xml(o.name)}"/>\n'
                '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n    </part>\n'
                + label_part + '  </object>\n')
    cfg += ('  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value="spoolmix calibration"/>\n'
            '    <metadata key="locked" value="false"/>\n')
    for i, o in enumerate(objs):
        cfg += (f'    <model_instance>\n      <metadata key="object_id" value="{o.obj_id}"/>\n'
                f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{100 + i}"/>\n'
                '    </model_instance>\n')
    cfg += "  </plate>\n  <assemble>\n"
    for o in objs:
        cfg += (f'   <assemble_item object_id="{o.obj_id}" instance_id="0" '
                f'transform="1 0 0 0 1 0 0 0 1 {o.x:.3f} {o.y:.3f} {o.sz / 2:g}" offset="0 0 0" />\n')
    cfg += "  </assemble>\n</config>\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            ' <Default Extension="png" ContentType="image/png"/>\n'
            ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n'))
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>\n'))
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/_rels/3dmodel.model.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            f' <Relationship Target="/{_OBJECTS_PATH}" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>\n'))
        z.writestr(_OBJECTS_PATH, objects_model)
        z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=4))
        z.writestr("Metadata/model_settings.config", cfg)
        for name, data in keep.items():
            z.writestr(name, data)
    return buf.getvalue()


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def template_info(template: Path | bytes) -> dict:
    """What a template project sets up: printer, process, filament slots and the
    color-mixing settings that matter to spoolmix. Raises ValueError if it isn't one."""
    src = io.BytesIO(template) if isinstance(template, (bytes, bytearray)) else template
    try:
        with zipfile.ZipFile(src) as z:
            s = json.loads(z.read("Metadata/project_settings.config"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise ValueError("That isn't a slicer project: save it with File → Save Project As (a .3mf with project settings).")
    colours = list(s.get("filament_colour") or [])
    if not colours:
        raise ValueError("That project has no filaments. Add one filament per slot your printer has, then save it again.")
    nozzle = s.get("nozzle_diameter")
    nozzle = nozzle[0] if isinstance(nozzle, list) and nozzle else nozzle
    return {
        "printer": s.get("printer_settings_id"),
        "process": s.get("print_settings_id"),
        "layer_height": s.get("layer_height"),
        "nozzle": nozzle,
        "filaments": list(zip(s.get("filament_settings_id") or [], colours)),
        "filament_types": list(s.get("filament_type") or []),
        "slots": len(colours),
        "version": s.get("version"),
        # Without these keys the slicer can't read spoolmix's mixed filaments.
        "mixing": "mixed_filament_definitions" in s,
        "subdivide": str(s.get("dithering_local_z_mode", "0")) in ("1", "true", "True"),
        # "Full domain": subdivision also applies to whole objects/parts assigned to a mix.
        "subdivide_whole": str(s.get("dithering_local_z_whole_objects", "0")) in ("1", "true", "True"),
    }


# --- fitting ----------------------------------------------------------------------


class _Objective:
    """Vectorized mean ΔE of measured mixed swatches for given per-filament log-strengths."""

    def __init__(self, rows, keys: list[str]):
        self.keys = keys
        self.groups = []
        by_size: dict[int, list] = {}
        for cols, w, meas_lab, ks in rows:
            by_size.setdefault(len(cols), []).append((cols, w, meas_lab, ks))
        for n, rs in by_size.items():
            self.groups.append((
                n,
                np.array([r[0] for r in rs], dtype=np.float64),  # (m, n, 3)
                np.array([r[1] for r in rs], dtype=np.float64),  # (m, n)
                np.array([r[2] for r in rs]),  # (m, 3)
                np.array([[keys.index(k) for k in r[3]] for r in rs]),  # (m, n)
            ))
        self.count = len(rows)

    def __call__(self, logk: np.ndarray) -> float:
        k = np.exp(np.asarray(logk, dtype=np.float64))
        total = 0.0
        for n, cols, w, meas, idx in self.groups:
            ew = w * k[idx]
            out = cols[:, 0, :]
            acc = ew[:, 0]
            for j in range(1, n):
                t = ew[:, j] / (acc + ew[:, j])
                out = lerp_batch(out, cols[:, j, :], t).astype(np.float64)
                acc = acc + ew[:, j]
            total += float(np.sum(delta_e2000(srgb_to_lab(out), meas)))
        return total / self.count


def _nelder_mead(f, x0: np.ndarray, step: float = 0.5, iters: int = 400, tol: float = 1e-4):
    n = len(x0)
    simplex = [x0] + [x0 + step * np.eye(n)[i] for i in range(n)]
    vals = [f(x) for x in simplex]
    for _ in range(iters):
        order = np.argsort(vals)
        simplex = [simplex[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        centroid = np.mean(simplex[:-1], axis=0)
        xr = centroid + (centroid - simplex[-1])
        fr = f(xr)
        if fr < vals[0]:
            xe = centroid + 2 * (centroid - simplex[-1])
            fe = f(xe)
            simplex[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            simplex[-1], vals[-1] = xr, fr
        else:
            xc = centroid + 0.5 * (simplex[-1] - centroid)
            fc = f(xc)
            if fc < vals[-1]:
                simplex[-1], vals[-1] = xc, fc
            else:
                simplex = [simplex[0]] + [simplex[0] + 0.5 * (x - simplex[0]) for x in simplex[1:]]
                vals = [vals[0]] + [f(x) for x in simplex[1:]]
    i = int(np.argmin(vals))
    return simplex[i], vals[i]


def fit(runs: Sequence[CalRun], passes: int = 4) -> dict:
    """Fit printed filament colors (pure swatches) and per-filament strengths (mixed swatches).

    Strength k scales a filament's share before Orca's mixing pipeline: k > 1 means it
    shows more than its layer share (typical for low-TD / opaque filament). Strengths
    are relative, so the first filament is fixed at 1. alpha (TD exponent for
    filaments never calibrated) is regressed from strengths of filaments with a TD.
    """
    colors: dict[str, list[RGB]] = {}
    tds: dict[str, float | None] = {}
    for run in runs:
        by_slot = {f.slot: f for f in run.filaments}
        for f in run.filaments:
            tds[f.key] = f.td
        for s in run.swatches:
            if s.measured and not s.is_mix:
                colors.setdefault(by_slot[s.slots[0]].key, []).append(parse_hex(s.measured))
    eff = {k: tuple(int(round(x)) for x in np.mean(v, axis=0)) for k, v in colors.items()}

    nominal_rows, eff_rows = [], []
    for run in runs:
        by_slot = {f.slot: f for f in run.filaments}
        for s in run.swatches:
            if s.measured and s.is_mix:
                fs = [by_slot[x] for x in s.slots]
                keys = [f.key for f in fs]
                meas = srgb_to_lab(parse_hex(s.measured))
                nominal_rows.append(([parse_hex(f.hex) for f in fs], s.weights, meas, keys))
                eff_rows.append(([eff.get(f.key, parse_hex(f.hex)) for f in fs], s.weights, meas, keys))
    if not eff_rows:
        raise ValueError("no measured mixed swatches yet")

    keys = sorted({k for row in eff_rows for k in row[3]})
    baseline = _Objective(nominal_rows, keys)(np.zeros(len(keys)))
    obj = _Objective(eff_rows, keys)
    colors_only = obj(np.zeros(len(keys)))

    # Strengths are relative: fix the first filament at 1 and search the rest (log space),
    # from several starting points to avoid local minima.
    def f_free(x):
        return obj(np.concatenate([[0.0], np.clip(x, -4, 4)]))

    best_x, best = np.zeros(len(keys) - 1), colors_only
    rng = np.random.default_rng(0)
    starts = [np.zeros(len(keys) - 1)] + [rng.uniform(-1.5, 1.5, len(keys) - 1) for _ in range(5)]
    for x0 in starts:
        x, v = _nelder_mead(f_free, x0)
        if v < best:
            best_x, best = np.clip(x, -4, 4), v
    logk = dict(zip(keys, np.concatenate([[0.0], best_x]).tolist()))
    strengths = {k: round(float(np.exp(v)), 4) for k, v in logk.items()}

    # TD fallback for uncalibrated filaments: log k = -alpha * log TD + c
    known = [(np.log(tds[k]), logk[k]) for k in keys if tds.get(k)]
    alpha, td_scale = 0.5, 1.0
    if len(known) >= 2 and np.ptp([x for x, _ in known]) > 0.1:
        slope, intercept = np.polyfit([x for x, _ in known], [y for _, y in known], 1)
        alpha = float(np.clip(-slope, 0.0, 2.0))
        td_scale = float(np.exp(intercept))
    elif known:
        td_scale = float(np.exp(np.mean([y + alpha * x for x, y in known])))
    else:
        td_scale = float(np.exp(np.mean(list(logk.values()))) * 5.0 ** alpha)

    return {
        "model": "strengths-v1",
        "alpha": round(alpha, 3),
        "td_scale": round(td_scale, 4),
        "strengths": strengths,
        "colors": {k: to_hex(v) for k, v in eff.items()},
        "fitted_at": dt.datetime.now().isoformat(timespec="seconds"),
        "runs": [r.id for r in runs],
        "mixed_swatches": len(eff_rows),
        "mean_de_orca": round(baseline, 3),
        "mean_de_colors_only": round(colors_only, 3),
        "mean_de_fitted": round(best, 3),
    }
