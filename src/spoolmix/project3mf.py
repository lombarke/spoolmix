"""Read and convert multicolor 3MF projects (the common slicer project format with per-part
filament assignments and per-triangle `paint_color` painting).

analyze()  -> the designer's filament colors and how much visible surface each covers
              (whole objects/parts assigned to a filament, plus per-triangle painting).
convert()  -> a Snapmaker Orca project for the U1: printer/process/filament presets from
              the user's template, 4 physical filaments, one mixed filament per remaining
              design color, and every object/part/painted triangle re-pointed accordingly.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from spoolmix import paint
from spoolmix.color import parse_hex, to_hex

CHUNK = 1 << 24
_RE_OBJ = re.compile(rb'<object\s[^>]*?\bid="(\d+)"')
_RE_VERT = re.compile(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"')
_RE_TRI = re.compile(rb'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"([^>]*)/>')
_RE_PAINT = re.compile(rb'paint_color="([0-9A-Fa-f]*)"')
_RE_COMP = re.compile(r'<component\s[^>]*?(?:p:path="([^"]+)")?[^>]*?objectid="(\d+)"[^>]*/>')
Progress = Callable[[str], None]


@dataclass
class DesignFilament:
    id: int  # 1-based, as in the project
    hex: str  # #RRGGBB
    type: str
    settings_id: str
    area: float = 0.0  # mm² of visible surface

    @property
    def rgb(self):
        return parse_hex(self.hex)


@dataclass
class Analysis:
    filaments: list[DesignFilament]
    printer: str | None
    process: str | None
    app: str | None
    objects: int
    plates: int
    painted_triangles: int
    total_triangles: int
    warnings: list[str] = field(default_factory=list)

    @property
    def used(self) -> list[DesignFilament]:
        return [f for f in self.filaments if f.area > 0]

    def to_json(self) -> dict:
        total = sum(f.area for f in self.filaments) or 1.0
        return {
            "filaments": [dict(id=f.id, hex=f.hex, type=f.type, settings_id=f.settings_id, area=round(f.area, 1),
                               share=round(f.area / total, 4)) for f in self.filaments],
            "printer": self.printer, "process": self.process, "app": self.app, "objects": self.objects,
            "plates": self.plates, "painted_triangles": self.painted_triangles,
            "total_triangles": self.total_triangles, "warnings": self.warnings,
        }


@lru_cache(maxsize=100_000)
def _state_areas(s: bytes) -> tuple[tuple[int, float], ...]:
    return tuple(paint.state_areas(s.decode()).items())


def _scan_model(fh, meshes: dict[int, list[tuple[int, np.ndarray]]], area: np.ndarray, counts: list[int]) -> None:
    """Stream a .model file and add visible surface area per filament into `area`.

    meshes: mesh object id -> [(default filament, 3x3 linear transform)] for each placed copy.
    Vertices of an object always precede its triangles, so triangles are processed per chunk
    as they arrive; memory stays proportional to the vertex count only.
    """
    buf = b""
    cur: int | None = None
    vparts: list[np.ndarray] = []
    V: np.ndarray | None = None

    def tris(seg: bytes) -> None:
        nonlocal V
        if cur not in meshes:
            return
        found = _RE_TRI.findall(seg)
        if not found:
            return
        if V is None:
            V = np.concatenate(vparts) if vparts else np.zeros((0, 3))
        t = np.array([(int(x), int(y), int(z)) for x, y, z, _ in found], dtype=np.int64)
        counts[1] += len(found)
        painted_idx = [i for i, (_, _, _, rest) in enumerate(found) if b"paint_color" in rest]
        for default, M in meshes[cur]:
            W = V @ M.T
            tri_area = 0.5 * np.linalg.norm(np.cross(W[t[:, 1]] - W[t[:, 0]], W[t[:, 2]] - W[t[:, 0]]), axis=1)
            plain = np.ones(len(found), dtype=bool)
            for i in painted_idx:
                m = _RE_PAINT.search(found[i][3])
                if not m or not m.group(1):
                    continue
                plain[i] = False
                for st, frac in _state_areas(m.group(1).upper()):
                    area[st if st else default] += frac * tri_area[i]
            area[default] += float(tri_area[plain].sum())
        counts[0] += sum(1 for i in painted_idx if _RE_PAINT.search(found[i][3]).group(1))

    def verts(seg: bytes) -> None:
        if cur in meshes:
            found = _RE_VERT.findall(seg)
            if found:
                vparts.append(np.array(found, dtype=np.float64))

    while True:
        chunk = fh.read(CHUNK)
        buf += chunk
        cut = len(buf) if not chunk else buf.rfind(b">") + 1
        work, buf = buf[:cut], buf[cut:]
        pos = 0
        for m in _RE_OBJ.finditer(work):
            seg = work[pos:m.start()]
            verts(seg)
            tris(seg)
            cur, vparts, V, pos = int(m.group(1)), [], None, m.end()
        seg = work[pos:]
        verts(seg)
        tris(seg)
        if not chunk:
            break


def _matrix(transform: str | None) -> np.ndarray:
    """3MF transform 'm00 m01 m02 m10 ... m32' (row-vector convention) -> 3x3 for column vectors."""
    if not transform:
        return np.eye(3)
    v = [float(x) for x in transform.split()]
    return np.array(v[:9]).reshape(3, 3).T


def _read_settings(z: zipfile.ZipFile) -> dict:
    return json.loads(z.read("Metadata/project_settings.config"))


def _model_settings(z: zipfile.ZipFile) -> ET.Element | None:
    try:
        return ET.fromstring(z.read("Metadata/model_settings.config"))
    except KeyError:
        return None


def _extruder_map(ms: ET.Element | None) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """object extruder by wrapper object id, and part extruder by (wrapper id, part id)."""
    objs: dict[str, int] = {}
    parts: dict[tuple[str, str], int] = {}
    if ms is None:
        return objs, parts
    for o in ms.findall("object"):
        oid = o.get("id")
        for md in o.findall("metadata"):
            if md.get("key") == "extruder":
                objs[oid] = int(md.get("value") or 1)
        for p in o.findall("part"):
            for md in p.findall("metadata"):
                if md.get("key") == "extruder":
                    parts[(oid, p.get("id"))] = int(md.get("value") or 1)
    return objs, parts


def _mesh_owners(z: zipfile.ZipFile, ms: ET.Element | None) -> dict[str, dict[int, list[tuple[int, np.ndarray]]]]:
    """model file -> mesh object id -> [(default filament, 3x3 transform)] per placed copy.

    Default filament: part override > object setting > 1. Transform: build item x component.
    """
    objs, parts = _extruder_map(ms)
    main = z.read("3D/3dmodel.model").decode("utf-8", errors="replace")
    comps: dict[str, list[tuple[str, int, np.ndarray]]] = {}
    for om in re.finditer(r'<object\s[^>]*?\bid="(\d+)"(.*?)</object>', main, re.S):
        wid, body = om.group(1), om.group(2)
        for c in re.finditer(r"<component\s([^>]*)/>", body):
            attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', c.group(1)))
            path = (attrs.get("p:path") or "/3D/3dmodel.model").lstrip("/")
            comps.setdefault(wid, []).append((path, int(attrs["objectid"]), _matrix(attrs.get("transform"))))
        if "<mesh" in body:
            comps.setdefault(wid, []).append(("3D/3dmodel.model", int(wid), np.eye(3)))
    out: dict[str, dict[int, list]] = {}
    for item in re.finditer(r"<item\s([^>]*)/>", main):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', item.group(1)))
        wid = attrs.get("objectid")
        T = _matrix(attrs.get("transform"))
        for path, mid, C in comps.get(wid, []):
            default = parts.get((wid, str(mid)), objs.get(wid, 1))
            out.setdefault(path, {}).setdefault(mid, []).append((default, T @ C))
    return out


def surface_by_filament(path: Path) -> dict[int, float]:
    """Visible surface per filament ID, including virtual (mixed) IDs. For verification."""
    with zipfile.ZipFile(path) as z:
        owners = _mesh_owners(z, _model_settings(z))
        area = np.zeros(128)
        for name, meshes in owners.items():
            with z.open(name) as fh:
                _scan_model(fh, meshes, area, [0, 0])
    return {i: float(a) for i, a in enumerate(area) if a > 0}


def analyze(path: Path, progress: Progress | None = None) -> Analysis:
    progress = progress or (lambda _m: None)
    with zipfile.ZipFile(path) as z:
        s = _read_settings(z)
        ms = _model_settings(z)
        cols = [c[:7].upper() for c in s.get("filament_colour") or []]
        types = list(s.get("filament_type") or []) + [""] * len(cols)
        ids = list(s.get("filament_settings_id") or []) + [""] * len(cols)
        fils = [DesignFilament(i + 1, h, types[i], ids[i]) for i, h in enumerate(cols)]
        owners = _mesh_owners(z, ms)
        warnings: list[str] = []
        counts = [0, 0]  # painted, total triangles
        area = np.zeros(max(len(fils), 1) + 64)
        for name, meshes in sorted(owners.items()):
            if name not in z.namelist():
                warnings.append(f"missing model file {name}")
                continue
            progress(f"reading {name}")
            with z.open(name) as fh:
                _scan_model(fh, meshes, area, counts)
        painted, total = counts
        for f in fils:
            f.area = float(area[f.id])
        stray = [i for i in range(len(fils) + 1, len(area)) if area[i] > 0]
        if stray:
            warnings.append(f"Painting references filament(s) {stray} that the project doesn't define; ignored.")
        app = None
        try:
            m = re.search(r'name="Application">([^<]*)<', z.read("3D/3dmodel.model").decode(errors="replace"))
            app = m.group(1) if m else None
        except KeyError:
            pass
        plates = len(ms.findall("plate")) if ms is not None else 1
        n_objects = len(ms.findall("object")) if ms is not None else 0
    return Analysis(fils, s.get("printer_settings_id"), s.get("print_settings_id"), app, n_objects, plates,
                    painted, total, warnings)


# --- conversion -----------------------------------------------------------------


@dataclass
class Assignment:
    """Where each design filament goes in the converted project."""

    physical: list[tuple[str, str]]  # 4 (hex, label) in slot order (F1..F4)
    targets: dict[int, int]  # design filament id -> new filament id (1..4 physical, 5.. mixed)
    mixed_rows: list[str]  # mixed_filament_definitions rows, in virtual-ID order
    materials: list[str | None] | None = None  # material of the filament in each slot (None: unknown/unused)


# Settings that belong to a filament preset (Snapmaker Orca's Preset.cpp
# s_Preset_filament_options), plus the preset's identity. In a project they're lists with
# one entry per filament slot; printer/nozzle settings (also per-slot lists on a
# multi-toolhead printer) are deliberately not in here.
FILAMENT_PRESET_KEYS = frozenset([
    "filament_colour", "default_filament_colour", "filament_diameter", "pellet_flow_coefficient",
    "filament_type", "filament_soluble", "filament_is_support", "filament_max_volumetric_speed",
    "filament_flow_ratio", "filament_density", "filament_adhesiveness_category", "filament_cost",
    "filament_minimal_purge_on_wipe_tower", "nozzle_temperature",
    "nozzle_temperature_initial_layer", "cool_plate_temp", "textured_cool_plate_temp",
    "eng_plate_temp", "hot_plate_temp", "textured_plate_temp", "cool_plate_temp_initial_layer",
    "textured_cool_plate_temp_initial_layer", "eng_plate_temp_initial_layer",
    "hot_plate_temp_initial_layer", "textured_plate_temp_initial_layer",
    "supertack_plate_temp_initial_layer", "supertack_plate_temp", "graphic_effect_plate_temp",
    "graphic_effect_plate_temp_initial_layer", "bed_type", "temperature_vitrification",
    "filament_is_high_temperature", "reduce_fan_stop_start_freq", "dont_slow_down_outer_wall",
    "slow_down_for_layer_cooling", "fan_min_speed", "fan_max_speed", "enable_overhang_bridge_fan",
    "overhang_fan_speed", "overhang_fan_threshold", "close_fan_the_first_x_layers",
    "full_fan_speed_layer", "fan_cooling_layer_time", "slow_down_layer_time", "slow_down_min_speed",
    "filament_start_gcode", "filament_end_gcode", "activate_air_filtration",
    "during_print_exhaust_fan_speed", "complete_print_exhaust_fan_speed",
    "filament_retraction_length", "filament_z_hop", "filament_z_hop_types",
    "filament_retract_lift_above", "filament_retract_lift_below", "filament_retract_lift_enforce",
    "filament_retraction_speed", "filament_deretraction_speed", "filament_retract_restart_extra",
    "filament_retraction_minimum_travel", "filament_retract_when_changing_layer", "filament_wipe",
    "filament_retract_before_wipe", "filament_retract_length_toolchange",
    "filament_retract_restart_extra_toolchange", "filament_vendor", "compatible_prints",
    "compatible_prints_condition", "compatible_printers", "compatible_printers_condition",
    "inherits", "filament_wipe_distance", "additional_cooling_fan_speed",
    "nozzle_temperature_range_low", "nozzle_temperature_range_high", "enable_pressure_advance",
    "pressure_advance", "adaptive_pressure_advance", "adaptive_pressure_advance_model",
    "adaptive_pressure_advance_overhangs", "adaptive_pressure_advance_bridges",
    "chamber_temperature", "filament_shrink", "filament_shrinkage_compensation_z",
    "support_material_interface_fan_speed", "internal_bridge_fan_speed", "filament_notes",
    "filament_seam_gap", "ironing_fan_speed", "filament_loading_speed",
    "filament_loading_speed_start", "filament_unloading_speed", "filament_unloading_speed_start",
    "filament_toolchange_delay", "filament_cooling_moves", "filament_stamping_loading_speed",
    "filament_stamping_distance", "filament_cooling_initial_speed", "filament_cooling_final_speed",
    "filament_ramming_parameters", "filament_multitool_ramming",
    "filament_multitool_ramming_volume", "filament_multitool_ramming_flow",
    "activate_chamber_temp_control", "filament_long_retractions_when_cut",
    "filament_retraction_distances_when_cut", "idle_temperature", "filament_tower_ironing_area",
    "filament_tower_interface_pre_extrusion_dist", "filament_tower_interface_pre_extrusion_length",
    "filament_tower_interface_print_temp", "filament_flow_support",
    "filament_settings_id", "filament_ids", "filament_vendor",
]) - {"filament_colour", "default_filament_colour"}


def match_presets_to_materials(settings: dict, materials: list[str | None]) -> list[str]:
    """Give each slot the template's filament preset for the material loaded there: a PLA
    filament in a slot the template set up for PETG gets the template's PLA preset (copied
    from a slot that has one). Changes `settings` in place; returns what it did, in words."""
    import copy

    from spoolmix.materials import category

    n = len(settings.get("filament_colour") or [])
    types = list(settings.get("filament_type") or [])
    if n == 0 or len(types) != n or len(materials) != n:
        return []
    orig = copy.deepcopy(settings)
    notes = []
    for i, mat in enumerate(materials):
        want = category(mat) if mat else None
        if not want or category(types[i]) == want:
            continue
        j = next((k for k in range(n) if category(types[k]) == want), None)
        if j is None:
            notes.append(f"slot {i + 1} holds {mat}, but the template has no {want} filament preset, so it keeps the "
                         f"{types[i]} one (check temperatures in the slicer)")
            continue
        for key in FILAMENT_PRESET_KEYS:
            v = orig.get(key)
            if isinstance(v, list) and len(v) == n:
                settings[key][i] = copy.deepcopy(v[j])
        diff = orig.get("different_settings_to_system")
        if isinstance(diff, list) and len(diff) == n + 2:  # [process, filament 1..n, printer]
            settings["different_settings_to_system"][i + 1] = diff[j + 1]
        notes.append(f"slot {i + 1} uses the template's {want} preset from slot {j + 1} instead of {types[i]}")
    return notes


def _remap_paint_stream(src, dst, mapping: dict[int, int]) -> None:
    cache: dict[bytes, bytes] = {}

    def sub(m: re.Match) -> bytes:
        s = m.group(1)
        r = cache.get(s)
        if r is None:
            r = cache[s] = paint.remap(s.decode(), mapping).encode() if s else s
        return b'paint_color="' + r + b'"'

    buf = b""
    while True:
        chunk = src.read(CHUNK)
        buf += chunk
        cut = len(buf) if not chunk else buf.rfind(b">") + 1
        dst.write(_RE_PAINT.sub(sub, buf[:cut]))
        buf = buf[cut:]
        if not chunk:
            break


# Process settings that must stay as the Color Mixing template sets them.
_KEEP_TEMPLATE = re.compile(
    r"^(layer_height|initial_layer_print_height|.*line_width|.*wipe_tower.*|enable_prime_tower|prime_tower_.*|"
    r"mixed_.*|filament_.*|flush_.*|print_settings_id|inherits|nozzle_.*|printer_.*|wall_filament|"
    r"sparse_infill_filament|solid_infill_filament|support_filament|support_interface_filament)$"
)


def carry_process_overrides(source: dict, target: dict) -> list[str]:
    """Copy process settings the designer changed from the system preset (e.g. supports,
    brim, infill, walls) into `target`. Returns the keys copied."""
    diff = (source.get("different_settings_to_system") or [""])[0] or ""
    copied = []
    for key in [k for k in diff.split(";") if k]:
        if key in source and not _KEEP_TEMPLATE.match(key):
            target[key] = source[key]
            copied.append(key)
    if copied:
        tdiff = list(target.get("different_settings_to_system") or [""])
        have = [k for k in (tdiff[0] or "").split(";") if k]
        tdiff[0] = ";".join(dict.fromkeys(have + copied))
        target["different_settings_to_system"] = tdiff
    return copied


def _zinfo(name: str, date_time) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=date_time)
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def convert(source: Path, template: Path, assign: Assignment, out: Path, progress: Progress | None = None) -> None:
    progress = progress or (lambda _m: None)
    with zipfile.ZipFile(template) as zt:
        tset = _read_settings(zt)
    n_phys = len(tset.get("filament_colour") or [])
    if n_phys != len(assign.physical):
        raise ValueError(
            f"Your template project has {n_phys} filament slots, but this plan uses {len(assign.physical)}. "
            f"Re-plan with {n_phys} filament slots, or save a template with {len(assign.physical)} filaments "
            "(Calibrate page) so the printer settings match."
        )

    settings = dict(tset)
    with zipfile.ZipFile(source) as zs:
        carried = carry_process_overrides(_read_settings(zs), settings)
    if carried:
        progress(f"kept designer settings: {', '.join(carried)}")
    settings["filament_colour"] = [h for h, _ in assign.physical]
    if isinstance(settings.get("filament_multi_colors"), list):
        settings["filament_multi_colors"] = [h for h, _ in assign.physical]
    settings["mixed_filament_definitions"] = ";".join(assign.mixed_rows)
    if assign.materials:
        for note in match_presets_to_materials(settings, list(assign.materials)):
            progress(note)

    skip = {"Metadata/project_settings.config", "Metadata/model_settings.config", "Metadata/filament_sequence.json",
            "Metadata/slice_info.config"}
    skip |= {n for n in zipfile.ZipFile(source).namelist() if re.fullmatch(r"Metadata/(filament_settings_\d+\.config|plate_\d+\.gcode.*)", n)}
    mapping = dict(assign.targets)

    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        for info in zin.infolist():
            name = info.filename
            if name in skip or info.is_dir():
                continue
            if name.endswith(".model"):
                progress(f"re-mapping colors in {name}")
                with zin.open(info) as src, zout.open(_zinfo(name, info.date_time), "w", force_zip64=True) as dst:
                    dst = io.BufferedWriter(dst, buffer_size=CHUNK)
                    _remap_paint_stream(src, dst, mapping)
                    dst.flush()
                continue
            with zin.open(info) as src, zout.open(_zinfo(name, info.date_time), "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, CHUNK)
        zout.writestr("Metadata/project_settings.config", json.dumps(settings, indent=4))
        try:
            ms = zin.read("Metadata/model_settings.config").decode()
            ms = re.sub(r'(<metadata key="extruder" value=")(\d+)(")',
                        lambda m: f"{m.group(1)}{mapping.get(int(m.group(2)), int(m.group(2)))}{m.group(3)}", ms)
            ms = re.sub(r'(<metadata key="filament_maps" value=")[^"]*(")',
                        lambda m: m.group(1) + " ".join(["1"] * n_phys) + m.group(2), ms)
            zout.writestr("Metadata/model_settings.config", ms)
        except KeyError:
            pass
        with zipfile.ZipFile(template) as zt:
            if "Metadata/slice_info.config" in zt.namelist():
                zout.writestr("Metadata/slice_info.config", zt.read("Metadata/slice_info.config"))
