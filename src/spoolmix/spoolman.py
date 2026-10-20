"""Minimal Spoolman REST client (v0.27, no auth)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from spoolmix.color import RGB, parse_hex

TD_FIELD = "td"
TD_SOURCE_FIELD = "td_source"

# Extra fields spoolmix needs on filaments, with their Spoolman definitions.
REQUIRED_FILAMENT_FIELDS: dict[str, dict[str, Any]] = {
    TD_FIELD: {"name": "Transmission Distance (TD)", "field_type": "float", "unit": "mm", "order": 100},
    TD_SOURCE_FIELD: {"name": "TD source", "field_type": "text", "order": 101},
}


@dataclass
class Filament:
    id: int
    name: str
    vendor: str | None
    material: str | None
    hex: str | None
    td: float | None = None
    td_source: str | None = None
    multi_color_hexes: str | None = None
    spools: list[Spool] = field(default_factory=list)
    have: bool | None = None  # spoolmix's own list: "I have this" (Spoolman: decided by spools)
    source: str = "spoolman"  # spoolman | local

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def rgb(self) -> RGB | None:
        return parse_hex(self.hex) if self.hex else None

    @property
    def remaining_weight(self) -> float:
        return sum(s.remaining_weight or 0.0 for s in self.spools)

    @property
    def in_stock(self) -> bool:
        if self.have is not None:
            return self.have
        return any(s.usable for s in self.spools)

    @property
    def label(self) -> str:
        parts = [p for p in (self.vendor, self.material, self.name) if p]
        return " ".join(parts)


@dataclass
class Spool:
    id: int
    filament_id: int
    remaining_weight: float | None
    archived: bool
    location: str | None

    @property
    def usable(self) -> bool:
        # Unknown remaining weight (None) counts as usable; Spoolman only knows it
        # when the initial weight was entered.
        return not self.archived and (self.remaining_weight is None or self.remaining_weight > 0)


def decode_extra(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def encode_extra(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


class SpoolmanClient:
    def __init__(self, base_url: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=f"{self.base_url}/api/v1", timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SpoolmanClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, **params) -> Any:
        r = self._http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    # --- reads -------------------------------------------------------------

    def info(self) -> dict:
        return self._get("/info")

    def filament_fields(self) -> list[dict]:
        return self._get("/field/filament")

    def missing_fields(self) -> dict[str, dict[str, Any]]:
        existing = {f["key"] for f in self.filament_fields()}
        return {k: v for k, v in REQUIRED_FILAMENT_FIELDS.items() if k not in existing}

    def filaments(self) -> list[Filament]:
        """All filaments with their non-archived spools attached."""
        raw_filaments = self._get("/filament")
        raw_spools = self._get("/spool", allow_archived="false")
        by_id: dict[int, Filament] = {}
        for f in raw_filaments:
            extra = f.get("extra") or {}
            td = decode_extra(extra.get(TD_FIELD))
            by_id[f["id"]] = Filament(
                id=f["id"],
                name=f.get("name") or f"Filament {f['id']}",
                vendor=(f.get("vendor") or {}).get("name"),
                material=f.get("material"),
                hex=f.get("color_hex"),
                td=float(td) if isinstance(td, (int, float)) else None,
                td_source=decode_extra(extra.get(TD_SOURCE_FIELD)),
                multi_color_hexes=f.get("multi_color_hexes"),
            )
        for s in raw_spools:
            fid = s["filament"]["id"]
            if fid in by_id:
                by_id[fid].spools.append(
                    Spool(
                        id=s["id"],
                        filament_id=fid,
                        remaining_weight=s.get("remaining_weight"),
                        archived=bool(s.get("archived")),
                        location=s.get("location"),
                    )
                )
        return sorted(by_id.values(), key=lambda f: f.id)

    def in_stock_filaments(self) -> list[Filament]:
        return [f for f in self.filaments() if f.in_stock]

    # --- writes (callers must confirm with the user first) -------------------

    def create_field(self, key: str, definition: dict[str, Any]) -> None:
        r = self._http.post(f"/field/filament/{key}", json=definition)
        r.raise_for_status()

    def set_filament_td(self, filament_id: int, td: float, source: str) -> None:
        payload = {"extra": {TD_FIELD: encode_extra(float(td)), TD_SOURCE_FIELD: encode_extra(source)}}
        r = self._http.patch(f"/filament/{filament_id}", json=payload)
        r.raise_for_status()
