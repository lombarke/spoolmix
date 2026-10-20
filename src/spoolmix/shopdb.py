"""Shopping pool: every single-color 1.75 mm filament in SpoolmanDB (refreshed weekly),
plus the curated list in catalog.yaml / data/catalog.*.local.yaml.

The pool is large (thousands of filaments), so callers first take the nearest candidates
to each target color (`nearest`), and only those get TD values and full evaluation.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from spoolmix.color import RGB, parse_hex, srgb_to_lab
from spoolmix.config import Config
from spoolmix.materials import category
from spoolmix.settings import load_settings, save_settings  # noqa: F401 (re-exported)
from spoolmix.solver import Component

SPOOLMANDB_URL = "https://donkie.github.io/SpoolmanDB/filaments.json"
CACHE_FILE = "spoolmandb.json"
REFRESH_DAYS = 7
SCOPES = {
    "all": "Any brand (SpoolmanDB + your list)",
    "owned_brands": "Only brands I already own",
    "list": "Only my own list",
}

# Clear filament: its listed color (often #FFFFFF) isn't what it looks like printed.
CLEAR = re.compile(r"transparent|translucent|clear\b|crystal|glass", re.I)

# Non-uniform colors: not useful as a mixing component.
TEXTURED = re.compile(r"wood|marble|galaxy|sparkle|glitter|starlight|granite|stone|confetti|gradient|rainbow|silk|"
                      r"metallic|metal\b|silver|chrome|"
                      r"dual[- ]?colou?r|tri[- ]?colou?r|multi[- ]?colou?r|two[- ]tone|color[- ]?chang|"
                      r"colour[- ]?chang|glow", re.I)

_lock = threading.Lock()
_mem: dict = {}


def search_url(label: str) -> str:
    return "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(f"{label} filament 1.75mm")


# --- settings ------------------------------------------------------------------------


def shop_scope(cfg: Config) -> str:
    s = load_settings(cfg).get("shop_scope", "all")
    return s if s in SCOPES else "all"


# --- SpoolmanDB cache ---------------------------------------------------------------------


def _compact(raw: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for f in raw:
        hx = f.get("color_hex")
        if not hx or f.get("color_hexes") or not re.fullmatch(r"[0-9A-Fa-f]{6}", hx):
            continue
        if f.get("diameter") not in (None, 1.75):
            continue
        key = ((f.get("manufacturer") or "").lower(), (f.get("material") or "").lower(), (f.get("name") or "").lower())
        if key in seen:  # same filament in several spool sizes
            continue
        seen.add(key)
        out.append({"id": f.get("id"), "brand": f.get("manufacturer"), "material": f.get("material"),
                    "name": f.get("name"), "hex": "#" + hx.upper(), "translucent": bool(f.get("translucent")),
                    "glow": bool(f.get("glow")), "finish": f.get("finish")})
    return out


def refresh(cfg: Config) -> int:
    with urllib.request.urlopen(urllib.request.Request(SPOOLMANDB_URL, headers={"User-Agent": "spoolmix"}), timeout=60) as r:
        items = _compact(json.load(r))
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.data_dir / (CACHE_FILE + ".part")
    tmp.write_text(json.dumps({"fetched": dt.date.today().isoformat(), "items": items}))
    tmp.replace(cfg.data_dir / CACHE_FILE)
    with _lock:
        _mem.clear()
    return len(items)


def age_days(cfg: Config) -> float | None:
    p = cfg.data_dir / CACHE_FILE
    return None if not p.is_file() else (dt.datetime.now().timestamp() - p.stat().st_mtime) / 86400


def ensure_fresh(cfg: Config) -> None:
    age = age_days(cfg)
    if age is None or age > REFRESH_DAYS:
        try:
            refresh(cfg)
        except Exception as exc:  # offline: keep the old cache (or none)
            print(f"SpoolmanDB refresh failed: {exc}", flush=True)


@dataclass
class Pool:
    items: list[dict]
    lab: np.ndarray  # (N, 3)


def _pool(cfg: Config) -> Pool:
    p = cfg.data_dir / CACHE_FILE
    stamp = p.stat().st_mtime if p.is_file() else None
    with _lock:
        if _mem.get("stamp") == stamp and "pool" in _mem:
            return _mem["pool"]
    items = json.loads(p.read_text())["items"] if p.is_file() else []
    lab = srgb_to_lab(np.array([parse_hex(i["hex"]) for i in items], dtype=np.float64)) if items else np.zeros((0, 3))
    pool = Pool(items, np.atleast_2d(lab))
    with _lock:
        _mem.update(stamp=stamp, pool=pool)
    return pool


def db_count(cfg: Config) -> int:
    return len(_pool(cfg).items)


# --- candidates ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ref:
    url: str
    source: str
    brand: str | None = None
    name: str | None = None
    bundle: str | None = None
    price: str | None = None
    translucent: bool | None = None


def _component(item: dict) -> Component:
    label = " ".join(x for x in (item["brand"], item["material"], item["name"]) if x)
    ref = _Ref(url=search_url(label), source="SpoolmanDB", brand=item["brand"], name=item["name"],
               translucent=item.get("translucent"))
    return Component(key=f"db:{item['id']}", label=label, rgb=parse_hex(item["hex"]), td=None, owned=False,
                     ref=ref, material=item["material"])


def nearest(
    cfg: Config,
    targets: Sequence[RGB],
    per_target: int = 8,
    categories: set[str] | None = None,
    brands: set[str] | None = None,
    exclude_labels: Iterable[str] = (),
) -> list[Component]:
    """The closest SpoolmanDB filaments to each target color (Lab distance), filtered by
    material category and (optionally) brand. Glow, textured and clear spools are skipped
    (your own shopping list can still include semi-translucent mixing filaments)."""
    pool = _pool(cfg)
    if not pool.items or not targets:
        return []
    from spoolmix.tddb import normalize_brand

    excl = {x.lower() for x in exclude_labels}
    ok = np.array([
        (categories is None or category(i["material"]) in categories)
        and (brands is None or normalize_brand(i["brand"]) in brands)
        and not i.get("glow") and not TEXTURED.search(f"{i['name']} {i.get('finish') or ''}")
        and not i.get("translucent") and not CLEAR.search(i["name"] or "")
        for i in pool.items
    ])
    idx = np.nonzero(ok)[0]
    if not len(idx):
        return []
    comps: dict[str, Component] = {}
    for t in targets:
        d = np.linalg.norm(pool.lab[idx] - srgb_to_lab(t), axis=1)
        taken = 0
        for j in idx[np.argsort(d)]:
            c = _component(pool.items[j])
            key = c.label.lower()
            if key in excl or key in comps:
                continue
            comps[key] = c
            taken += 1
            if taken >= per_target:
                break
    return list(comps.values())


def with_td(cfg: Config, comps: Sequence[Component]) -> list[Component]:
    """Fill TD for shopping candidates: published (high confidence) or an estimate."""
    from spoolmix import service
    from spoolmix.color import to_hex
    from spoolmix.tddb import estimate_td

    db = service.td_db(cfg)
    out = []
    for c in comps:
        if c.td is not None:
            out.append(c)
            continue
        brand = getattr(c.ref, "brand", None)
        name = getattr(c.ref, "name", None) or c.label
        m = db.best(brand, c.material, name, to_hex(c.rgb))
        td = m.entry.td if m and m.level == "high" else None
        if td is None:
            est = estimate_td(db, brand, c.material, name, to_hex(c.rgb))
            td = est.td if est else None
        out.append(replace(c, td=td))
    return out


def candidates(cfg: Config, targets: Sequence[RGB], categories: set[str] | None,
               stock: Sequence[Component], per_target: int = 8) -> list[Component]:
    """Shopping candidates for these target colors, honoring the shop scope setting:
    your own list (catalog.yaml + imports) plus, unless scope is 'list', the nearest
    SpoolmanDB filaments. Filaments you already own are excluded. TDs filled in."""
    from spoolmix import service
    from spoolmix.catalog import load_catalog
    from spoolmix.design import filter_materials
    from spoolmix.tddb import normalize_brand

    scope = shop_scope(cfg)
    own = [i.component() for i in load_catalog(cfg.catalog, service.td_db(cfg), cfg.data_dir)]
    owned_labels = {c.label.lower() for c in stock}
    own = [c for c in own if c.label.lower() not in owned_labels]
    if categories:
        own, _ = filter_materials(own, categories)
    brands = None
    if scope == "owned_brands":
        brands = {normalize_brand(getattr(c.ref, "vendor", None) or c.label.split(" ")[0]) for c in stock}
        own = [c for c in own if normalize_brand(c.label.split(" ")[0]) in brands
               or normalize_brand(" ".join(c.label.split(" ")[:2])) in brands]
    found: list[Component] = []
    if scope != "list":
        ensure_fresh(cfg)
        found = nearest(cfg, targets, per_target, categories, brands, owned_labels | {c.label.lower() for c in own})
    return own + with_td(cfg, found)


# --- browsing (the "add filament" form) ------------------------------------------------


def brands(cfg: Config) -> list[dict]:
    """Every brand in SpoolmanDB with how many single-color filaments it has."""
    from collections import Counter

    counts = Counter(i["brand"] for i in _pool(cfg).items if i.get("brand"))
    return [{"brand": b, "count": n} for b, n in sorted(counts.items(), key=lambda x: x[0].lower())]


def materials(cfg: Config, brand: str) -> list[str]:
    b = (brand or "").strip().lower()
    found = {i["material"] for i in _pool(cfg).items if (i.get("brand") or "").lower() == b and i.get("material")}
    return sorted(found, key=str.lower)


def colors(cfg: Config, brand: str, material: str) -> list[dict]:
    b, m = (brand or "").strip().lower(), (material or "").strip().lower()
    out = [{"id": i["id"], "name": i["name"], "hex": i["hex"], "translucent": i.get("translucent"),
            "finish": i.get("finish"), "glow": i.get("glow")}
           for i in _pool(cfg).items
           if (i.get("brand") or "").lower() == b and (i.get("material") or "").lower() == m and i.get("name")]
    return sorted(out, key=lambda c: c["name"].lower())
