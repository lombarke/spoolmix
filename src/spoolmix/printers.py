"""Printer templates: projects saved once in the slicer that carry the printer, print
process and filament settings. Several can be stored (different printers, nozzles,
layer heights, subdivide on/off…); each plan, color match and calibration uses one.

Layout: data/templates/<id>/template.3mf, meta.json (name, uploaded, info) and, once
fitted, calibration.local.yaml. The default template id is in settings.json.
A single data/template.3mf from older versions is moved in on first use, along with the
calibration and grids, which were all made with it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from spoolmix import calibrate
from spoolmix.config import Config
from spoolmix.materials import category
from spoolmix.settings import load_settings, save_settings

TEMPLATES_DIR = "templates"
TEMPLATE_FILE = "template.3mf"
_lock = threading.RLock()


@dataclass
class Template:
    id: str
    name: str
    uploaded: str
    info: dict
    dir: Path
    default: bool = False

    @property
    def path(self) -> Path:
        return self.dir / TEMPLATE_FILE

    @property
    def slots(self) -> int:
        return int(self.info.get("slots") or len(self.info.get("filaments") or []) or 4)

    @property
    def categories(self) -> set[str]:
        """Material families of its filament presets, e.g. {'PLA'}."""
        return {c for c in (category(t) for t in self.info.get("filament_types") or []) if c}

    def calibration(self) -> dict | None:
        return calibrate.load_calibration(self.dir)


def root(cfg: Config) -> Path:
    return cfg.data_dir / TEMPLATES_DIR


def _write_meta(d: Path, meta: dict) -> None:
    tmp = d / "meta.json.part"
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(d / "meta.json")


def default_name(info: dict, taken: set[str] = frozenset()) -> str:
    """e.g. 'Snapmaker U1 (0.4 nozzle) · 0.10mm Color Mixing', unique among `taken`."""
    printer = re.sub(r"\s+-\s+.*$", "", str(info.get("printer") or "Printer")).strip()
    process = re.sub(r"\s*@.*$", "", str(info.get("process") or "")).strip()
    name = " · ".join(x for x in (printer, process) if x)
    if info.get("subdivide"):
        name += " · subdivided"
    base, n = name, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    return name


def _migrate(cfg: Config) -> None:
    """Move a pre-multi-template data/template.3mf (and its calibration) into the store."""
    legacy = cfg.data_dir / TEMPLATE_FILE
    if not legacy.is_file() or (root(cfg).is_dir() and any(root(cfg).iterdir())):
        return
    try:
        t = add(cfg, legacy.read_bytes(), _migrating=True)
    except ValueError:
        return
    cal = cfg.data_dir / calibrate.CALIBRATION_FILE
    if cal.is_file():
        shutil.move(str(cal), t.dir / calibrate.CALIBRATION_FILE)
    for run in calibrate.list_runs(cfg.data_dir):
        if run.template is None:
            run.template = t.id
            calibrate.save_run(run, cfg.data_dir)
    legacy.unlink()


def all_templates(cfg: Config) -> list[Template]:
    """Every stored template, default first, then by name."""
    with _lock:
        _migrate(cfg)
        out = []
        if root(cfg).is_dir():
            for d in root(cfg).iterdir():
                try:
                    m = json.loads((d / "meta.json").read_text())
                except (FileNotFoundError, ValueError, NotADirectoryError):
                    continue
                if (d / TEMPLATE_FILE).is_file():
                    out.append(Template(m["id"], m["name"], m.get("uploaded", ""), m.get("info") or {}, d))
        default_id = load_settings(cfg).get("default_template")
        if out and not any(t.id == default_id for t in out):
            default_id = sorted(out, key=lambda t: t.uploaded)[0].id
        for t in out:
            t.default = t.id == default_id
        return sorted(out, key=lambda t: (not t.default, t.name.lower()))


def get(cfg: Config, tid: str | None) -> Template | None:
    if not tid or not re.fullmatch(r"[0-9a-f]{12}", tid):
        return None
    return next((t for t in all_templates(cfg) if t.id == tid), None)


def default(cfg: Config) -> Template | None:
    ts = all_templates(cfg)
    return ts[0] if ts else None


def resolve(cfg: Config, tid: str | None) -> Template | None:
    """The template with this id, else the default (None if there are none)."""
    return get(cfg, tid) or default(cfg)


def add(cfg: Config, data: bytes, name: str | None = None, _migrating: bool = False) -> Template:
    info = calibrate.template_info(data)  # raises ValueError for non-projects
    with _lock:
        taken = {t.name for t in all_templates(cfg)} if not _migrating else set()
        tid = uuid.uuid4().hex[:12]
        d = root(cfg) / tid
        d.mkdir(parents=True)
        (d / TEMPLATE_FILE).write_bytes(data)
        meta = {"id": tid, "name": (name or "").strip()[:80] or default_name(info, taken),
                "uploaded": dt.datetime.now().isoformat(timespec="seconds"), "info": info}
        _write_meta(d, meta)
        if not load_settings(cfg).get("default_template") or _migrating:
            save_settings(cfg, default_template=tid)
    return get(cfg, tid) if not _migrating else Template(tid, meta["name"], meta["uploaded"], info, d, True)


def replace(cfg: Config, tid: str, data: bytes) -> Template:
    info = calibrate.template_info(data)
    with _lock:
        t = get(cfg, tid)
        if t is None:
            raise KeyError(tid)
        tmp = t.dir / (TEMPLATE_FILE + ".part")
        tmp.write_bytes(data)
        tmp.replace(t.path)
        _write_meta(t.dir, {"id": t.id, "name": t.name, "uploaded": dt.datetime.now().isoformat(timespec="seconds"),
                            "info": info})
    return get(cfg, tid)


def rename(cfg: Config, tid: str, name: str) -> None:
    name = re.sub(r"\s+", " ", name or "").strip()[:80]
    with _lock:
        t = get(cfg, tid)
        if t is None:
            raise KeyError(tid)
        if not name:
            raise ValueError("Give the template a name.")
        _write_meta(t.dir, {"id": t.id, "name": name, "uploaded": t.uploaded, "info": t.info})


def set_default(cfg: Config, tid: str) -> None:
    if get(cfg, tid) is None:
        raise KeyError(tid)
    save_settings(cfg, default_template=tid)


def delete(cfg: Config, tid: str) -> None:
    with _lock:
        t = get(cfg, tid)
        if t is None:
            raise KeyError(tid)
        shutil.rmtree(t.dir, ignore_errors=True)
        if load_settings(cfg).get("default_template") == tid:
            rest = all_templates(cfg)
            save_settings(cfg, default_template=rest[0].id if rest else None)


def best_for(cfg: Config, design_categories: set[str]) -> Template | None:
    """The default template, unless another one's filament presets match the design's
    material and the default's don't (e.g. a PETG design and a PETG template)."""
    ts = all_templates(cfg)
    if not ts:
        return None
    if not design_categories or not ts[0].categories or ts[0].categories & design_categories:
        return ts[0]
    return next((t for t in ts if t.categories & design_categories), ts[0])
