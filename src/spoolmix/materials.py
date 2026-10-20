"""Material categories and which may be mixed in one print (material_compatibility.yaml)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from ruamel.yaml import YAML

from spoolmix.config import REPO_ROOT

# Keyword order matters: check longer/more specific names first.
_KEYWORDS = (
    ("PCTG", "PETG"), ("PETG", "PETG"), ("PET", "PET"), ("PLA", "PLA"), ("TPU", "TPU"), ("TPE", "TPU"),
    ("ABS", "ABS"), ("ASA", "ASA"), ("PC", "PC"), ("PA", "PA"), ("NYLON", "PA"),
    ("PVA", "SUPPORT"), ("BVOH", "SUPPORT"), ("HIPS", "SUPPORT"),
)


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    with open(path, encoding="utf-8") as fh:
        doc = YAML(typ="safe").load(fh) or {}
    compat: dict[str, set[str]] = {}
    for cat, partners in (doc.get("compatibility") or {}).items():
        compat.setdefault(cat.upper(), set()).add(cat.upper())
        for p in partners or []:
            compat[cat.upper()].add(p.upper())
            compat.setdefault(p.upper(), {p.upper()}).add(cat.upper())
    aliases = {k.upper(): v.upper() for k, v in (doc.get("aliases") or {}).items()}
    return {k: frozenset(v) for k, v in compat.items()}, aliases


def _table():
    path = os.environ.get("SPOOLMIX_MATERIALS", str(REPO_ROOT / "material_compatibility.yaml"))
    return _load(path)


def category(material: str | None) -> str | None:
    """Category for a material name ('PLA+' -> 'PLA', 'Silk PLA' -> 'PLA', 'PCTG' -> 'PETG').
    None for an empty name; the uppercased name itself if unrecognized."""
    if not material or not material.strip():
        return None
    compat, aliases = _table()
    m = material.strip().upper()
    if m in aliases:
        return aliases[m]
    if m in compat:
        return m
    tokens = re.findall(r"[A-Z]+", m)
    for kw, cat in _KEYWORDS:
        if kw in tokens or (len(kw) >= 3 and any(t.startswith(kw) for t in tokens)):
            return cat
    return m


def compatible(a: str | None, b: str | None) -> bool:
    """Whether materials a and b may be mixed. Unknown (None) material is treated as compatible
    with anything (no information); unrecognized names only with themselves."""
    ca, cb = category(a), category(b)
    if ca is None or cb is None or ca == cb:
        return True
    compat, _ = _table()
    return cb in compat.get(ca, frozenset())


def all_compatible(materials: Iterable[str | None]) -> bool:
    ms = list(materials)
    return all(compatible(x, y) for i, x in enumerate(ms) for y in ms[i + 1:])


def categories() -> list[str]:
    compat, _ = _table()
    return sorted(compat)
