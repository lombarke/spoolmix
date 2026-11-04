import json

from spoolmix import shopdb
from spoolmix.config import load_config
from spoolmix.solver import Component

RAW = [
    {"id": "a1", "manufacturer": "Acme", "name": "Forest Green", "material": "PLA", "color_hex": "1F6B3A", "diameter": 1.75},
    {"id": "a1-2kg", "manufacturer": "Acme", "name": "Forest Green", "material": "PLA", "color_hex": "1F6B3A", "diameter": 1.75},
    {"id": "a2", "manufacturer": "Acme", "name": "Grass Green", "material": "PETG", "color_hex": "2E8B57", "diameter": 1.75},
    {"id": "a3", "manufacturer": "Acme", "name": "Rainbow", "material": "PLA", "color_hex": "FF00FF", "diameter": 1.75},
    {"id": "a4", "manufacturer": "Acme", "name": "Thick Green", "material": "PLA", "color_hex": "1F6B3A", "diameter": 2.85},
    {"id": "a5", "manufacturer": "Acme", "name": "Duo", "material": "PLA", "color_hex": "000000", "color_hexes": ["000000", "FFFFFF"]},
    {"id": "b1", "manufacturer": "Bolt", "name": "Leaf Green", "material": "PLA+", "color_hex": "2A7A40", "diameter": 1.75},
    {"id": "b2", "manufacturer": "Bolt", "name": "Glow Green", "material": "PLA", "color_hex": "2A7A40", "diameter": 1.75, "glow": True},
    {"id": "b3", "manufacturer": "Bolt", "name": "Transparent Green", "material": "PLA", "color_hex": "1F6B3A", "diameter": 1.75},
    {"id": "b4", "manufacturer": "Bolt", "name": "Moss", "material": "PLA", "color_hex": "1F6B3A", "diameter": 1.75, "translucent": True},
    {"id": "b5", "manufacturer": "Bolt", "name": "Silver", "material": "PLA", "color_hex": "1F6B3A", "diameter": 1.75},
]


def _cfg(tmp_path, monkeypatch, scope=None):
    monkeypatch.setenv("SPOOLMIX_DATA_DIR", str(tmp_path))
    cfg = load_config()
    (tmp_path / shopdb.CACHE_FILE).write_text(json.dumps({"fetched": "2026-10-16", "items": shopdb._compact(RAW)}))
    if scope:
        shopdb.save_settings(cfg, shop_scope=scope)
    return cfg


def test_compact_dedupes_and_filters():
    items = shopdb._compact(RAW)
    names = [i["name"] for i in items]
    assert names.count("Forest Green") == 1  # spool sizes collapsed
    assert "Thick Green" not in names and "Duo" not in names  # 2.85 mm / multicolor


def test_nearest_filters_material_textures_and_glow(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    got = shopdb.nearest(cfg, [(31, 107, 58)], per_target=5, categories={"PLA"})
    labels = [c.label for c in got]
    assert labels[0] == "Acme PLA Forest Green"
    assert "Bolt PLA+ Leaf Green" in labels
    assert not any("PETG" in x or "Rainbow" in x or "Glow" in x for x in labels)
    assert not any(x.endswith(("Transparent Green", "Moss", "Silver")) for x in labels)  # clear / metallic
    assert got[0].owned is False and got[0].ref.url.startswith("https://duckduckgo.com/?q=")


def test_candidates_scopes_and_owned(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    owned = [Component("spoolman:1", "Acme PLA Forest Green", (31, 107, 58), 2.0, material="PLA")]
    labels = [c.label for c in shopdb.candidates(cfg, [(31, 107, 58)], {"PLA"}, owned)]
    assert "Acme PLA Forest Green" not in labels  # already owned
    assert "Bolt PLA+ Leaf Green" in labels
    shopdb.save_settings(cfg, shop_scope="owned_brands")
    labels = [c.label for c in shopdb.candidates(cfg, [(31, 107, 58)], {"PLA"}, owned)]
    assert not any(x.startswith("Bolt") for x in labels)
    shopdb.save_settings(cfg, shop_scope="list")
    assert all(not c.key.startswith("db:") for c in shopdb.candidates(cfg, [(31, 107, 58)], {"PLA"}, owned))
