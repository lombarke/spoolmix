"""Purchase catalog: catalog.yaml plus optional data/catalog.*.local.yaml files."""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from spoolmix.color import RGB, parse_hex
from spoolmix.solver import Component
from spoolmix.tddb import TDDatabase

SPOOLMANDB_URL = "https://donkie.github.io/SpoolmanDB/filaments.json"

_yaml = YAML()
_yaml.width = 4096


@dataclass(frozen=True)
class CatalogItem:
    id: str
    brand: str
    material: str
    color_name: str
    hex: str
    td: float | None
    td_source: str | None
    bundle: str | None = None
    url: str | None = None
    price: str | None = None
    notes: str | None = None

    @property
    def rgb(self) -> RGB:
        return parse_hex(self.hex)

    @property
    def label(self) -> str:
        return f"{self.brand} {self.material} {self.color_name}"

    def component(self) -> Component:
        return Component(key=f"shop:{self.id}", label=self.label, rgb=self.rgb, td=self.td, owned=False, ref=self,
                         material=self.material)


def _load_items(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    return [dict(i) for i in doc.get("items") or []]


def load_catalog(path: Path, tddb: TDDatabase | None = None, data_dir: Path | None = None) -> list[CatalogItem]:
    files = [path]
    if data_dir and data_dir.is_dir():
        files += sorted(data_dir.glob("catalog.*.local.yaml"))
    items: dict[str, CatalogItem] = {}
    for f in files:
        for raw in _load_items(f):
            if raw.get("enabled") is False or not raw.get("hex") or not raw.get("id"):
                continue
            td, td_source = raw.get("td"), None
            if td is not None:
                td_source = "catalog"
            elif tddb is not None:
                m = tddb.best(raw.get("brand"), raw.get("material"), raw.get("color_name"), raw.get("hex"))
                if m and m.level == "high":
                    td, td_source = m.entry.td, f"{m.entry.source} ({m.confidence:.2f})"
            item = CatalogItem(
                id=str(raw["id"]),
                brand=str(raw.get("brand") or "?"),
                material=str(raw.get("material") or "?"),
                color_name=str(raw.get("color_name") or raw["id"]),
                hex=str(raw["hex"]),
                td=float(td) if td is not None else None,
                td_source=td_source,
                bundle=raw.get("bundle"),
                url=raw.get("url"),
                price=str(raw["price"]) if raw.get("price") is not None else None,
                notes=raw.get("notes"),
            )
            items.setdefault(item.id, item)  # first definition wins (catalog.yaml over local files)
    return list(items.values())


def import_spoolmandb(
    out_path: Path,
    manufacturers: list[str] | None = None,
    materials: list[str] | None = None,
    translucent_only: bool = False,
) -> int:
    """Write SpoolmanDB single-color filaments matching the filters to a local catalog file."""
    with urllib.request.urlopen(urllib.request.Request(SPOOLMANDB_URL, headers={"User-Agent": "spoolmix"}), timeout=60) as r:
        data = json.load(r)
    mf = {m.lower() for m in manufacturers or []}
    mt = {m.lower() for m in materials or []}
    items, seen = [], set()
    for f in data:
        hx = f.get("color_hex")
        if not hx or f.get("color_hexes") or not re.fullmatch(r"[0-9A-Fa-f]{6}", hx):
            continue
        if mf and (f.get("manufacturer") or "").lower() not in mf:
            continue
        if mt and (f.get("material") or "").lower() not in mt:
            continue
        if f.get("diameter") not in (None, 1.75) or (translucent_only and not f.get("translucent")):
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", f"{f.get('manufacturer')}-{f.get('material')}-{f.get('name')}".lower()).strip("-")
        if slug in seen:
            continue
        seen.add(slug)
        items.append({
            "id": f"spoolmandb-{slug}",
            "brand": f.get("manufacturer"),
            "material": f.get("material"),
            "color_name": f.get("name"),
            "hex": f"#{hx.upper()}",
            "notes": f"SpoolmanDB {f.get('id')}" + ("; translucent" if f.get("translucent") else ""),
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Imported from {SPOOLMANDB_URL}. Regenerate with `spoolmix catalog import-spoolmandb`.\n")
        _yaml.dump({"schema_version": 1, "items": items}, fh)
    return len(items)
