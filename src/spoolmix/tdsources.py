"""Fetch published TD values: Snapmaker Orca's color library, Prusa's HueForge table,
and filamentcolors.xyz measured swatches. Used by scripts/build_td_database.py (bundled
database) and at runtime to refresh data/td_published.yaml for the brands you own."""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.request

UA = {"User-Agent": "spoolmix-td-database-builder (+https://github.com/lombarke/spoolmix)"}
SNAPMAKER_COMMIT = "main"
SNAPMAKER_RAW = (
    "https://raw.githubusercontent.com/Snapmaker/OrcaSlicer/"
    f"{SNAPMAKER_COMMIT}/resources/profiles/Snapmaker/filament/filaments_colours.json"
)
SNAPMAKER_URL = (
    f"https://github.com/Snapmaker/OrcaSlicer/blob/{SNAPMAKER_COMMIT}"
    "/resources/profiles/Snapmaker/filament/filaments_colours.json"
)
PRUSA_URL = "https://help.prusa3d.com/article/hueforge-filament-transparency-values-and-hexcodes_762314"
FC_API = "https://filamentcolors.xyz/api"
DEFAULT_FC_MANUFACTURERS = ["Sunlu", "Bambu Lab", "Polymaker", "eSUN", "Elegoo", "Overture"]


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def snapmaker_entries(today: str) -> list[dict]:
    data = json.loads(fetch(SNAPMAKER_RAW))
    out = []
    for fil in data["filaments"]:
        ftype = fil.get("filament_type", "")
        for col in fil.get("filament_color", []):
            td = col.get("TD")
            if not td or len(col.get("filament_color", [])) != 1:
                continue
            out.append(
                {
                    "brand": "Snapmaker",
                    "material": ftype,
                    "color_name": col["color_name"]["en"],
                    "hex": col["filament_color"][0].upper(),
                    "td": float(td),
                    "source": "snapmaker-orca",
                    "source_url": SNAPMAKER_URL,
                    "source_date": today,
                    "notes": f"SKU {col.get('sku')}" if col.get("sku") else None,
                }
            )
    return out


def prusa_entries(today: str) -> list[dict]:
    page = fetch(PRUSA_URL).decode("utf-8")
    table = re.search(r"<table.*?</table>", page, re.S).group(0)
    out = []
    for row in re.findall(r"<tr.*?</tr>", table, re.S)[1:]:
        cells = [
            html.unescape(re.sub("<[^>]+>", "", c)).replace("\xa0", " ").strip()
            for c in re.findall(r"<t[dh].*?</t[dh]>", row, re.S)
        ]
        if len(cells) < 4 or not cells[2]:
            continue
        material, product, td, hexcode = cells[:4]
        note = cells[4] if len(cells) > 4 else ""
        name = re.sub(r"\s+\d+(\.\d+)?\s*(kg|g)\b", "", product, flags=re.I).strip()
        name = re.sub(r"^Prusament\s+", "", name)
        name = re.sub(rf"^{re.escape(material)}\s+", "", name, flags=re.I)
        hx = hexcode.rstrip("*")[:6].upper()
        notes = "; ".join(n for n in (note, "hex marked * on source page" if hexcode.endswith("*") else "") if n)
        out.append(
            {
                "brand": "Prusament",
                "material": material,
                "color_name": name,
                "hex": f"#{hx}" if re.fullmatch(r"[0-9A-F]{6}", hx) else None,
                "td": float(td),
                "source": "prusa-kb",
                "source_url": PRUSA_URL,
                "source_date": today,
                "notes": notes or None,
            }
        )
    return out


def _fc_paginate(url: str) -> list[dict]:
    out = []
    while url:
        d = json.loads(fetch(url))
        out += d["results"]
        url = d["next"]
    return out


def filamentcolors_entries(manufacturers: list[str], today: str) -> list[dict]:
    mfrs = {m["name"].lower(): m["id"] for m in _fc_paginate(f"{FC_API}/manufacturer/")}
    out = []
    for name in manufacturers:
        mid = mfrs.get(name.lower())
        if mid is None:
            print(f"filamentcolors: manufacturer {name!r} not found, skipping", file=sys.stderr)
            continue
        for s in _fc_paginate(f"{FC_API}/swatch/?m=all&manufacturer={mid}&page_size=100"):
            if s.get("td") is None:
                continue
            lo, hi = (s.get("td_range") or [s["td"], s["td"]])[:2]
            notes = [f"filamentcolors swatch {s['id']}"]
            if lo != hi:
                notes.append(f"TD range {lo}-{hi}")
            if not s.get("is_available", True):
                notes.append("discontinued")
            out.append(
                {
                    "brand": s["manufacturer"]["name"],
                    "material": s["filament_type"]["name"],
                    "color_name": s["color_name"],
                    "hex": f"#{s['hex_color'].upper()}",
                    "td": float(s["td"]),
                    "source": "filamentcolors",
                    "source_url": f"https://filamentcolors.xyz/swatch/{s['slug']}/",
                    "source_date": today,
                    "notes": "; ".join(notes),
                }
            )
    return out




def fc_manufacturers() -> dict[str, int]:
    """filamentcolors.xyz manufacturer name (lowercase) -> id."""
    return {m["name"].lower(): m["id"] for m in _fc_paginate(f"{FC_API}/manufacturer/")}
