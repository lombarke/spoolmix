"""Where your filaments come from: a Spoolman server, or spoolmix's own list.

Both backends offer the same small interface as SpoolmanClient (filaments, set_filament_td,
missing_fields, create_field), so callers don't care which one is in use. The choice is made
on the setup page and stored in data/settings.json; SPOOLMAN_URL in the environment counts as
"Spoolman at that URL" until something is saved.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spoolmix.config import Config
from spoolmix.settings import load_settings, save_settings
from spoolmix.spoolman import Filament, SpoolmanClient

LOCAL_FILE = "filaments.json"
TD_OVERLAY_FILE = "td_spoolman.json"  # TDs for Spoolman filaments when spoolmix may not write to Spoolman
KINDS = ("spoolman", "local")


class NotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    kind: str  # spoolman | local
    url: str | None = None
    td_in_spoolman: bool = True
    from_env: bool = False

    @property
    def description(self) -> str:
        return f"Spoolman at {self.url}" if self.kind == "spoolman" else "your filament list in spoolmix"


def source(cfg: Config) -> Source | None:
    """The configured filament source, or None before setup."""
    s = load_settings(cfg)
    kind = s.get("inventory")
    if kind == "local":
        return Source("local")
    if kind == "spoolman" and s.get("spoolman_url"):
        return Source("spoolman", s["spoolman_url"], bool(s.get("td_in_spoolman", True)))
    if cfg.spoolman_url:
        return Source("spoolman", cfg.spoolman_url, True, from_env=True)
    return None


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"https?://", url, re.I):
        url = "http://" + url
    return re.sub(r"/api/v1$", "", url)


def save_source(cfg: Config, kind: str, url: str | None = None, td_in_spoolman: bool = True) -> Source:
    if kind not in KINDS:
        raise ValueError(kind)
    changes: dict[str, Any] = {"inventory": kind}
    if kind == "spoolman":
        changes |= {"spoolman_url": normalize_url(url or ""), "td_in_spoolman": bool(td_in_spoolman)}
    save_settings(cfg, **changes)
    return source(cfg)


def open_inventory(cfg: Config, src: Source | None = None):
    src = src or source(cfg)
    if src is None:
        raise NotConfigured("Choose where your filaments come from on the Settings page first.")
    if src.kind == "local":
        return LocalInventory(cfg.data_dir / LOCAL_FILE)
    return SpoolmanInventory(src.url, td_in_spoolman=src.td_in_spoolman, overlay=cfg.data_dir / TD_OVERLAY_FILE)


def test_spoolman(url: str) -> dict:
    """Check a Spoolman URL: {'ok', 'version', 'filaments', 'in_stock', 'error'}."""
    url = normalize_url(url)
    if not url:
        return {"ok": False, "error": "Enter the address of your Spoolman server."}
    try:
        with SpoolmanClient(url, timeout=8.0) as sm:
            info = sm.info()
            fils = sm.filaments()
            missing = sm.missing_fields()
    except Exception as exc:
        return {"ok": False, "url": url, "error": _friendly_error(exc, url)}
    return {"ok": True, "url": url, "version": info.get("version"), "filaments": len(fils),
            "in_stock": sum(1 for f in fils if f.in_stock), "needs_fields": sorted(missing)}


SPOOLMAN_DEFAULT_PORT = 7912


def _friendly_error(exc: Exception, url: str = "") -> str:
    import httpx
    from urllib.parse import urlsplit

    try:
        port = urlsplit(url).port
    except ValueError:
        port = None
    hint = (f" Spoolman's default port is {SPOOLMAN_DEFAULT_PORT}; this address uses {port or 'none'}."
            if port != SPOOLMAN_DEFAULT_PORT else "")
    if isinstance(exc, httpx.ConnectError):
        return "Couldn't connect. Check the address and port, and that Spoolman is running." + hint
    if isinstance(exc, httpx.TimeoutException):
        return "No answer from that address (timed out)." + hint
    if isinstance(exc, httpx.HTTPStatusError):
        return f"That address answered, but not like Spoolman does (HTTP {exc.response.status_code})."
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "That address answered, but it doesn't look like Spoolman."
    return f"{type(exc).__name__}: {exc}"


# --- Spoolman ---------------------------------------------------------------------------


class _Closing:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        pass


class SpoolmanInventory(_Closing):
    kind = "spoolman"

    def __init__(self, url: str, td_in_spoolman: bool = True, overlay: Path | None = None):
        self.base_url = url
        self.td_in_spoolman = td_in_spoolman
        self._overlay = overlay
        self._sm = SpoolmanClient(url)

    def close(self) -> None:
        self._sm.close()

    def missing_fields(self) -> dict:
        return self._sm.missing_fields() if self.td_in_spoolman else {}

    def create_field(self, key: str, definition: dict) -> None:
        if self.td_in_spoolman:
            self._sm.create_field(key, definition)

    def filaments(self) -> list[Filament]:
        fils = self._sm.filaments()
        if not self.td_in_spoolman:
            ov = self._load_overlay()
            for f in fils:
                if f.td is None and str(f.id) in ov:
                    f.td, f.td_source = ov[str(f.id)]["td"], ov[str(f.id)]["td_source"]
        return fils

    def in_stock_filaments(self) -> list[Filament]:
        return [f for f in self.filaments() if f.in_stock]

    def set_filament_td(self, filament_id: int, td: float, source: str) -> None:
        if self.td_in_spoolman:
            self._sm.set_filament_td(filament_id, td, source)
            return
        with _file_lock:
            ov = self._load_overlay()
            ov[str(filament_id)] = {"td": float(td), "td_source": source}
            _write_json(self._overlay, ov)

    def _load_overlay(self) -> dict:
        try:
            return json.loads(self._overlay.read_text()) if self._overlay else {}
        except (FileNotFoundError, ValueError):
            return {}


# --- spoolmix's own list ----------------------------------------------------------------

_file_lock = threading.RLock()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def clean_hex(value: str | None) -> str | None:
    v = (value or "").strip().lstrip("#").upper()
    if re.fullmatch(r"[0-9A-F]{3}", v):
        v = "".join(c * 2 for c in v)
    return v if re.fullmatch(r"[0-9A-F]{6}", v) else None


class LocalInventory(_Closing):
    """Filaments kept by spoolmix in data/filaments.json, for people without Spoolman."""

    kind = "local"
    base_url = "spoolmix"
    FIELDS = ("brand", "material", "name", "hex", "td", "td_source", "have", "db_id")

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault("filaments", [])
        data.setdefault("next_id", 1 + max((r["id"] for r in data["filaments"]), default=0))
        return data

    @staticmethod
    def _filament(r: dict) -> Filament:
        return Filament(id=r["id"], name=r.get("name") or f"Filament {r['id']}", vendor=r.get("brand") or None,
                        material=r.get("material") or None, hex=r.get("hex"), td=r.get("td"),
                        td_source=r.get("td_source"), have=bool(r.get("have", True)), source="local")

    def filaments(self) -> list[Filament]:
        return [self._filament(r) for r in self._load()["filaments"]]

    def in_stock_filaments(self) -> list[Filament]:
        return [f for f in self.filaments() if f.in_stock]

    def missing_fields(self) -> dict:
        return {}

    def create_field(self, key: str, definition: dict) -> None:
        pass

    def get(self, fid: int) -> dict | None:
        return next((r for r in self._load()["filaments"] if r["id"] == fid), None)

    def add(self, **values) -> int:
        with _file_lock:
            data = self._load()
            fid = data["next_id"]
            row = {"id": fid, "have": True, "added": dt.date.today().isoformat()}
            row |= self._clean(values)
            data["filaments"].append(row)
            data["next_id"] = fid + 1
            _write_json(self.path, data)
        return fid

    def update(self, fid: int, **values) -> None:
        with _file_lock:
            data = self._load()
            row = next((r for r in data["filaments"] if r["id"] == fid), None)
            if row is None:
                raise KeyError(fid)
            row |= self._clean(values)
            _write_json(self.path, data)

    def delete(self, fid: int) -> None:
        with _file_lock:
            data = self._load()
            data["filaments"] = [r for r in data["filaments"] if r["id"] != fid]
            _write_json(self.path, data)

    def set_filament_td(self, filament_id: int, td: float, source: str) -> None:
        self.update(filament_id, td=float(td), td_source=source)

    def _clean(self, values: dict) -> dict:
        out = {}
        for k, v in values.items():
            if k not in self.FIELDS:
                raise ValueError(f"unknown field {k}")
            if k == "hex":
                v = clean_hex(v)
                if v is None:
                    raise ValueError("not a hex color")
            elif k in ("brand", "material", "name"):
                v = re.sub(r"\s+", " ", str(v or "")).strip()[:80] or None
            elif k == "have":
                v = bool(v)
            out[k] = v
        return out


def import_from_spoolman(cfg: Config, url: str) -> int:
    """Copy Spoolman's filaments into spoolmix's own list (skipping ones already there)."""
    local = LocalInventory(cfg.data_dir / LOCAL_FILE)
    have = {(f.vendor or "", f.material or "", f.name, f.hex) for f in local.filaments()}
    n = 0
    with SpoolmanClient(normalize_url(url)) as sm:
        for f in sm.filaments():
            if not f.hex or (f.vendor or "", f.material or "", f.name, clean_hex(f.hex)) in have:
                continue
            local.add(brand=f.vendor, material=f.material, name=f.name, hex=f.hex, have=f.in_stock,
                      td=f.td, td_source=f.td_source)
            n += 1
    return n
