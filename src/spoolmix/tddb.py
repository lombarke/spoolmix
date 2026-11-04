"""Local, versioned TD database (td_database.yaml) and fuzzy matching.

File layout:

    schema_version: 1
    updated: YYYY-MM-DD
    sources: {key: {title, url, description}}
    entries:
      - brand, material, color_name, hex, td, source, source_url, source_date, notes
      [- method: td1 | seashell | other    (user-measured entries only)]
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from rapidfuzz import fuzz
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

import numpy as np

from spoolmix.color import delta_e_rgb, parse_hex, srgb_to_lab

SCHEMA_VERSION = 1

SOURCES: dict[str, dict[str, str]] = {
    "snapmaker-orca": {
        "title": "Snapmaker Orca filament color library (filaments_colours.json)",
        "url": "https://github.com/Snapmaker/OrcaSlicer/blob/main/resources/profiles/Snapmaker/filament/filaments_colours.json",
        "description": "Official Snapmaker TD values shipped with the slicer.",
    },
    "prusa-kb": {
        "title": "Prusa Knowledge Base: HueForge filament transparency values and hexcodes",
        "url": "https://help.prusa3d.com/article/hueforge-filament-transparency-values-and-hexcodes_762314",
        "description": "Manufacturer-published HueForge TD and hex for Prusament.",
    },
    "filamentcolors": {
        "title": "filamentcolors.xyz swatch library",
        "url": "https://filamentcolors.xyz/",
        "description": "Community swatch library; TD measured per physical swatch (one spool, one measurement).",
    },
    "user-measured": {
        "title": "Your own measurements",
        "url": "",
        "description": "Added with `spoolmix td add` (TD-1 meter or Seashell/HueForge test print).",
    },
}

BRAND_ALIASES = {
    "prusa": "prusament",
    "prusa research": "prusament",
    "bambu": "bambu lab",
    "bambulab": "bambu lab",
    "bambu labs": "bambu lab",
    "esun3d": "esun",
    "sunlu3d": "sunlu",
    "snapmaker official": "snapmaker",
}

# Tokens that describe the material, not the color, when they show up in a name.
_MATERIAL_WORDS = {"pla", "pla+", "petg", "abs", "asa", "tpu", "pc", "pa", "plus", "filament", "1kg", "1.75mm"}

HIGH_CONFIDENCE = 0.85
LOW_CONFIDENCE = 0.60

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)


@dataclass(frozen=True)
class TDEntry:
    brand: str
    material: str
    color_name: str
    hex: str | None
    td: float
    source: str
    source_url: str | None
    source_date: str | None
    notes: str | None = None
    method: str | None = None

    @property
    def label(self) -> str:
        return f"{self.brand} {self.material} {self.color_name}"


@dataclass(frozen=True)
class TDMatch:
    entry: TDEntry
    confidence: float
    brand_score: float
    material_score: float
    name_score: float
    hex_score: float | None

    @property
    def level(self) -> str:
        if self.confidence >= HIGH_CONFIDENCE:
            return "high"
        if self.confidence >= LOW_CONFIDENCE:
            return "medium"
        return "low"


def normalize_brand(name: str | None) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    return BRAND_ALIASES.get(n, n)


def material_family(material: str | None) -> str:
    m = (material or "").upper()
    for fam in ("PETG", "PCTG", "PLA", "ABS", "ASA", "TPU", "PVB", "PC", "PA", "PP"):
        if fam in m:
            return fam
    return m.strip()


def _material_tokens(material: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9+]+", (material or "").lower()))


def normalize_color_name(name: str | None, material: str | None = None) -> str:
    """Lowercase, drop material words and duplicated tokens ('Yellow (Glow Yellow)' -> 'yellow glow')."""
    tokens = re.findall(r"[a-z0-9+]+", (name or "").lower().replace("grey", "gray"))
    skip = _MATERIAL_WORDS | _material_tokens(material)
    seen: list[str] = []
    for t in tokens:
        if t not in skip and t not in seen:
            seen.append(t)
    return " ".join(seen)


def _entry_descriptor(entry: TDEntry, query_material: str | None = None) -> str:
    """Color name plus product-line words from the entry's material (e.g. 'PLA Meta' -> 'meta'),
    except words the query's material already states."""
    known = _material_tokens(query_material)
    line = [
        t for t in _material_tokens(entry.material)
        if t not in _MATERIAL_WORDS and t not in known and t.upper() != material_family(entry.material)
    ]
    return normalize_color_name(" ".join([*line, entry.color_name]), None)


def _material_score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.5
    if a.strip().lower() == b.strip().lower():
        return 1.0
    if material_family(a) == material_family(b):
        return 0.8
    return 0.0


def _brand_score(a: str | None, b: str | None) -> float:
    na, nb = normalize_brand(a), normalize_brand(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return fuzz.ratio(na, nb) / 100.0


def score_match(
    entry: TDEntry, brand: str | None, material: str | None, color_name: str | None, hex_color: str | None
) -> TDMatch:
    b = _brand_score(brand, entry.brand)
    m = _material_score(material, entry.material)
    query = normalize_color_name(color_name, material)
    n = fuzz.token_sort_ratio(query, _entry_descriptor(entry, material)) / 100.0 if query else 0.0
    h: float | None = None
    if hex_color and entry.hex:
        h = max(0.0, 1.0 - delta_e_rgb(parse_hex(hex_color), parse_hex(entry.hex)) / 30.0)
    weights = {"b": 0.30, "m": 0.20, "n": 0.40, "h": 0.10}
    if h is None:
        conf = (weights["b"] * b + weights["m"] * m + weights["n"] * n) / (1.0 - weights["h"])
    else:
        conf = weights["b"] * b + weights["m"] * m + weights["n"] * n + weights["h"] * h
    # A different brand's TD is not a reliable stand-in.
    if b < 0.9:
        conf = min(conf, 0.5)
    if m == 0.0:
        conf = min(conf, 0.4)
    # Same brand and material but a different color is not a match at all.
    if n < 0.6:
        conf = min(conf, 0.45)
    # Glow/silk/matte/... behave very differently from plain filament.
    if _special(f"{material or ''} {color_name or ''}") != _special(f"{entry.material} {entry.color_name}"):
        conf = min(conf, 0.55)
    return TDMatch(entry, round(conf, 3), b, m, n, h)


class TDDatabase:
    """The bundled database plus, optionally, a separate file of your own measurements.

    With `user_path` set, `add_user_entry` writes there (e.g. data/td_user.yaml on a
    Docker volume) and the bundled file is never modified.
    """

    def __init__(self, path: Path, doc: CommentedMap, user_path: Path | None = None, user_doc: CommentedMap | None = None):
        self.path = path
        self.doc = doc
        self.user_path = user_path
        self.user_doc = user_doc
        self.entries = [self._to_entry(e) for e in doc.get("entries") or []]
        if user_doc is not None:
            self.entries += [self._to_entry(e) for e in user_doc.get("entries") or []]

    # --- io ------------------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> CommentedMap:
        with open(path, encoding="utf-8") as fh:
            doc = _yaml.load(fh) or CommentedMap()
        version = doc.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"{path}: unsupported schema_version {version}")
        return doc

    @classmethod
    def load(cls, path: Path, user_path: Path | None = None, published_path: Path | None = None) -> TDDatabase:
        """published_path: values fetched at runtime (data/td_published.yaml); they replace bundled
        entries with the same source URL, so newer measurements win."""
        user_doc = None
        if user_path is not None:
            if user_path.is_file():
                user_doc = cls._read(user_path)
            else:
                user_doc = CommentedMap()
                user_doc["schema_version"] = SCHEMA_VERSION
                user_doc["entries"] = CommentedSeq()
                user_doc.yaml_set_start_comment(
                    "Your own TD measurements (spoolmix td add / web UI). Merged with td_database.yaml."
                )
        db = cls(path, cls._read(path), user_path, user_doc)
        if published_path is not None and published_path.is_file():
            fresh = [cls._to_entry(e) for e in (cls._read(published_path).get("entries") or [])]
            urls = {e.source_url for e in fresh if e.source_url}
            db.entries = [e for e in db.entries if e.source_url not in urls] + fresh
        return db

    @staticmethod
    def write_new(path: Path, entries: Iterable[dict[str, Any]], updated: str) -> None:
        doc = CommentedMap()
        doc["schema_version"] = SCHEMA_VERSION
        doc["updated"] = updated
        doc["sources"] = SOURCES
        seq = CommentedSeq()
        for e in sorted(entries, key=lambda e: (e["source"], e["brand"].lower(), e["material"].lower(), e["color_name"].lower())):
            seq.append(CommentedMap((k, v) for k, v in e.items() if v is not None))
        doc["entries"] = seq
        doc.yaml_set_start_comment(
            "spoolmix TD database. Every entry cites its source and retrieval date.\n"
            "Regenerate published sources with scripts/build_td_database.py;\n"
            "add your own measurements with `spoolmix td add` (kept across rebuilds)."
        )
        with open(path, "w", encoding="utf-8") as fh:
            _yaml.dump(doc, fh)

    def save(self) -> None:
        if self.user_path is not None and self.user_doc is not None:
            self.user_doc["updated"] = dt.date.today().isoformat()
            self.user_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.user_path, "w", encoding="utf-8") as fh:
                _yaml.dump(self.user_doc, fh)
            return
        self.doc["updated"] = dt.date.today().isoformat()
        with open(self.path, "w", encoding="utf-8") as fh:
            _yaml.dump(self.doc, fh)

    def raw_entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self.doc.get("entries") or []]

    @staticmethod
    def _to_entry(e: dict[str, Any]) -> TDEntry:
        return TDEntry(
            brand=str(e["brand"]),
            material=str(e["material"]),
            color_name=str(e["color_name"]),
            hex=e.get("hex"),
            td=float(e["td"]),
            source=str(e["source"]),
            source_url=e.get("source_url"),
            source_date=str(e["source_date"]) if e.get("source_date") else None,
            notes=e.get("notes"),
            method=e.get("method"),
        )

    # --- queries -------------------------------------------------------------

    def match(
        self,
        brand: str | None,
        material: str | None,
        color_name: str | None,
        hex_color: str | None = None,
        limit: int = 3,
    ) -> list[TDMatch]:
        scored = [score_match(e, brand, material, color_name, hex_color) for e in self.entries]
        # Prefer user measurements on ties.
        scored.sort(key=lambda m: (m.confidence, m.entry.source == "user-measured"), reverse=True)
        return scored[:limit]

    def best(self, *args, **kwargs) -> TDMatch | None:
        found = self.match(*args, limit=1, **kwargs)
        return found[0] if found else None

    # --- writes --------------------------------------------------------------

    def add_user_entry(
        self,
        brand: str,
        material: str,
        color_name: str,
        td: float,
        hex_color: str | None = None,
        method: str = "other",
        notes: str | None = None,
    ) -> TDEntry:
        item = CommentedMap()
        item["brand"] = brand
        item["material"] = material
        item["color_name"] = color_name
        if hex_color:
            item["hex"] = "#{:02X}{:02X}{:02X}".format(*parse_hex(hex_color))
        item["td"] = float(td)
        item["source"] = "user-measured"
        item["source_date"] = dt.date.today().isoformat()
        item["method"] = method
        if notes:
            item["notes"] = notes
        target = self.user_doc if self.user_doc is not None else self.doc
        if target.get("entries") is None:
            target["entries"] = CommentedSeq()
        target["entries"].append(item)
        entry = self._to_entry(item)
        self.entries.append(entry)
        self.save()
        return entry


# --- estimates ------------------------------------------------------------------

# Product types whose translucency differs a lot from plain filament; only compare like with like.
SPECIAL_TYPES = ("glow", "silk", "matte", "marble", "galaxy", "transparent", "clear", "translucent", "metal", "wood", "sparkle")
# Of those, these change translucency fundamentally and must match exactly; the others are surface
# finishes that may fall back to plain filament of the same material when nothing closer is measured.
STRICT_TYPES = frozenset({"glow", "transparent", "clear", "translucent"})
SAME_BRAND_MAX_DE = 35.0  # Lab distance


@dataclass(frozen=True)
class TDEstimate:
    td: float
    neighbors: tuple[tuple[TDEntry, float], ...]  # (entry, ΔE to the filament's color)
    scope: str = "brand"  # "brand": same brand's filaments; "other brands": none of this brand fit

    @property
    def basis(self) -> str:
        return "; ".join(f"{e.brand} {e.material} {e.color_name} {e.td:g}" for e, _ in self.neighbors)


def _special(text: str) -> frozenset[str]:
    t = text.lower()
    return frozenset(w for w in SPECIAL_TYPES if w in t)


def estimate_td(db: TDDatabase, brand: str | None, material: str | None, color_name: str | None,
                hex_color: str | None, k: int = 3) -> TDEstimate | None:
    """Estimate TD from measured filaments of the same material family and product type
    (glow, silk, translucent... only compared with their own kind), nearest in color.
    Uses the same brand's filaments when there are at least 2, otherwise other brands'."""
    if not hex_color:
        return None
    fam = material_family(material)
    kind = _special(f"{material or ''} {color_name or ''}")
    def pool(want: frozenset[str]) -> list[TDEntry]:
        return [
            e for e in db.entries
            if material_family(e.material) == fam and e.hex and e.td < 50  # skip clear/transparent outliers
            and _special(f"{e.material} {e.color_name}") == want
        ]

    target = parse_hex(hex_color)

    tlab = srgb_to_lab(target)

    def nearest(entries: list[TDEntry]) -> list[tuple[TDEntry, float]]:
        # Plain Lab distance (ΔE76): CIEDE2000 compresses large chroma differences, which would
        # make a vivid magenta look "close" to a grey.
        d = [(e, float(np.linalg.norm(srgb_to_lab(parse_hex(e.hex)) - tlab))) for e in entries]
        return sorted(d, key=lambda x: x[1])[:k]

    near: list[tuple[TDEntry, float]] = []
    scope = "brand"
    kinds = [kind] if (kind & STRICT_TYPES or not kind) else [kind, frozenset()]
    for want in kinds:
        similar = pool(want)
        same_brand = nearest([e for e in similar if normalize_brand(e.brand) == normalize_brand(brand)])
        # Same brand only if it has at least 2 filaments reasonably close in color; a brand's
        # white says little about its black.
        if len(same_brand) >= 2 and same_brand[1][1] <= SAME_BRAND_MAX_DE:
            near, scope = same_brand, "brand"
        else:
            near, scope = nearest(similar), "other brands"
        if len(near) >= 2:
            break
    if len(near) < 2:
        return None
    w = [1.0 / (d + 1.0) for _, d in near]
    td = sum(wi * e.td for wi, (e, _) in zip(w, near)) / sum(w)
    return TDEstimate(round(td, 1), tuple(near), scope)
