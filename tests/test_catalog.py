from pathlib import Path

from spoolmix.catalog import load_catalog
from spoolmix.config import REPO_ROOT
from spoolmix.tddb import TDDatabase

CATALOG = """\
schema_version: 1
items:
  - id: fs-cyan
    brand: Snapmaker
    material: PLA Full Spectrum
    color_name: Semi-Translucent Cyan
    hex: "#00C3FF"
  - id: explicit
    brand: Acme
    material: PLA
    color_name: Teal
    hex: "#008080"
    td: 3.3
  - id: unknown
    brand: Acme
    material: PLA
    color_name: Mystery
    hex: "#123456"
  - id: hidden
    brand: Acme
    material: PLA
    color_name: Hidden
    hex: "#FFFFFF"
    enabled: false
"""


def _db(tmp_path: Path) -> TDDatabase:
    p = tmp_path / "td.yaml"
    TDDatabase.write_new(p, [dict(brand="Snapmaker", material="PLA Full Spectrum", color_name="Semi-Translucent Cyan",
                                  hex="#00C3FF", td=5.5, source="snapmaker-orca", source_url="u", source_date="2026-10-16")],
                         updated="2026-10-16")
    return TDDatabase.load(p)


def test_catalog_td_fill_and_flags(tmp_path):
    cat = tmp_path / "catalog.yaml"
    cat.write_text(CATALOG)
    items = {i.id: i for i in load_catalog(cat, _db(tmp_path))}
    assert set(items) == {"fs-cyan", "explicit", "unknown"}
    assert items["fs-cyan"].td == 5.5 and items["fs-cyan"].td_source.startswith("snapmaker-orca")
    assert items["explicit"].td == 3.3 and items["explicit"].td_source == "catalog"
    assert items["unknown"].td is None
    comp = items["fs-cyan"].component()
    assert comp.owned is False and comp.key == "shop:fs-cyan"


def test_local_catalogs_merged_and_main_wins(tmp_path):
    cat = tmp_path / "catalog.yaml"
    cat.write_text(CATALOG)
    data = tmp_path / "data"
    data.mkdir()
    (data / "catalog.extra.local.yaml").write_text(
        'items:\n  - {id: explicit, brand: X, material: PLA, color_name: Other, hex: "#000000"}\n'
        '  - {id: extra-1, brand: X, material: PLA, color_name: Blue, hex: "#0000FF"}\n'
    )
    items = {i.id: i for i in load_catalog(cat, None, data)}
    assert "extra-1" in items
    assert items["explicit"].brand == "Acme"


def test_repo_catalog_and_td_database_load():
    db = TDDatabase.load(REPO_ROOT / "td_database.yaml")
    assert all(e.source_url or e.source == "user-measured" for e in db.entries)
    assert all(e.source_date for e in db.entries)
    items = load_catalog(REPO_ROOT / "catalog.yaml", db)
    fs = [i for i in items if i.id.startswith("snapmaker-fs-")]
    assert {i.color_name.split()[-1] for i in fs} >= {"Cyan", "Magenta", "Yellow", "Gray"}
    assert all(i.td is not None for i in fs)
