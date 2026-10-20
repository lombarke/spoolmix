from pathlib import Path

import pytest

from spoolmix.cli import _parse_selection, _td_owner
from spoolmix.spoolman import Filament
from spoolmix.tddb import TDDatabase, material_family, normalize_brand, normalize_color_name

ENTRIES = [
    dict(brand="Sunlu", material="PLA", color_name="Red", hex="#AC3637", td=5.7, source="filamentcolors",
         source_url="https://filamentcolors.xyz/swatch/sunlu-red-pla-2938/", source_date="2026-10-16"),
    dict(brand="Sunlu", material="PLA+", color_name="Orange", hex="#ED7432", td=5.8, source="filamentcolors",
         source_url="https://filamentcolors.xyz/swatch/x/", source_date="2026-10-16"),
    dict(brand="Sunlu", material="PLA", color_name="Grey", hex="#6B6E6E", td=1.3, source="filamentcolors",
         source_url="https://filamentcolors.xyz/swatch/y/", source_date="2026-10-16"),
    dict(brand="Prusament", material="PLA", color_name="Lipstick Red", hex="#D03036", td=3.3, source="prusa-kb",
         source_url="https://help.prusa3d.com/article/x", source_date="2026-10-16"),
    dict(brand="Snapmaker", material="PLA Full Spectrum", color_name="Semi-Translucent Cyan", hex="#00C3FF",
         td=5.5, source="snapmaker-orca", source_url="https://github.com/x", source_date="2026-10-16"),
]


@pytest.fixture
def db(tmp_path: Path) -> TDDatabase:
    path = tmp_path / "td.yaml"
    TDDatabase.write_new(path, ENTRIES, updated="2026-10-16")
    return TDDatabase.load(path)


def test_normalizers():
    assert normalize_brand("SUNLU") == "sunlu"
    assert normalize_brand("Bambu Labs") == "bambu lab"
    assert material_family("PLA+") == "PLA"
    assert material_family("High Speed PLA") == "PLA"
    assert normalize_color_name("Yellow (Glow Yellow)") == "yellow glow"
    assert normalize_color_name("Magenta PLA", "PLA") == "magenta"
    assert normalize_color_name("Meta Grey") == "meta gray"


def test_exact_match_high_confidence(db):
    m = db.best("SUNLU", "PLA", "Red", "#AC3638")
    assert m.entry.td == 5.7 and m.level == "high" and m.confidence > 0.95


def test_material_family_match_still_high(db):
    m = db.best("Sunlu", "PLA", "Orange", "#F25602")
    assert m.entry.color_name == "Orange" and m.level == "high"


def test_other_brand_capped(db):
    m = db.best("Elegoo", "PLA", "Lipstick Red", "#D03036")
    assert m.confidence <= 0.5 and m.level == "low"


def test_unrelated_color_is_low(db):
    m = db.best("Sunlu", "PLA+", "Cyan", "#39D0F1")
    assert m.level == "low"


def test_partial_name_is_medium(db):
    m = db.best("Sunlu", "PLA", "Meta Grey", "#75787B")
    assert m.entry.color_name == "Grey" and m.level == "medium"


def test_add_user_entry_roundtrip_and_preferred(db):
    db.add_user_entry("Sunlu", "PLA+", "Cyan", 3.9, "#39D0F1", method="td1", notes="spool from 2026-10")
    again = TDDatabase.load(db.path)
    m = again.best("Sunlu", "PLA+", "Cyan", "#39D0F1")
    assert m.entry.source == "user-measured" and m.entry.td == 3.9 and m.entry.method == "td1"
    assert m.level == "high"
    assert again.path.read_text().startswith("# spoolmix TD database")


def _fil(td=None, src=None):
    return Filament(id=1, name="Red", vendor="Sunlu", material="PLA", hex="AC3638", td=td, td_source=src)


def test_td_owner_never_overwrites_user_values():
    assert _td_owner(_fil()) == "none"
    assert _td_owner(_fil(5.7, "lookup:filamentcolors ...")) == "lookup"
    assert _td_owner(_fil(4.0, "measured:td1:2026-10-16")) == "user"
    assert _td_owner(_fil(4.0, None)) == "user"  # typed in Spoolman UI, no source


def test_parse_selection(db):
    rows = [(1, _fil(), db.best("Sunlu", "PLA", "Red", "#AC3638")),
            (2, _fil(), db.best("Sunlu", "PLA", "Meta Grey", "#75787B"))]
    assert _parse_selection("none", rows) == []
    assert [r[0] for r in _parse_selection("high", rows)] == [1]
    assert [r[0] for r in _parse_selection("all", rows)] == [1, 2]
    assert [r[0] for r in _parse_selection("2", rows)] == [2]


# --- estimates ---------------------------------------------------------------

from spoolmix import service
from spoolmix.tddb import estimate_td

EST_ENTRIES = [
    dict(brand="Sunlu", material="PLA", color_name=c, hex=h, td=t, source="filamentcolors", source_url="u", source_date="2026-10-16")
    for c, h, t in [("Sky Blue", "#0CB7CC", 4.0), ("Blue", "#006AB8", 3.9), ("Grey", "#6B6E6E", 1.3),
                    ("Glow in the Dark", "#CBCAB8", 11.6), ("Transparent", "#C8C7BF", 100.0)]
] + [dict(brand="Sunlu", material="Glow PLA", color_name="Red", hex="#F25455", td=8.6, source="filamentcolors",
          source_url="u", source_date="2026-10-16"),
     dict(brand="Sunlu", material="PLA", color_name="Red", hex="#AC3637", td=5.7, source="filamentcolors",
          source_url="u", source_date="2026-10-16")]


@pytest.fixture
def edb(tmp_path):
    p = tmp_path / "td.yaml"
    TDDatabase.write_new(p, EST_ENTRIES, updated="2026-10-16")
    return TDDatabase.load(p)


def test_estimate_uses_similar_colors(edb):
    est = estimate_td(edb, "Sunlu", "PLA+", "Cyan", "#39D0F1")
    assert est.neighbors[0][0].color_name == "Sky Blue"
    tds = [e.td for e, _ in est.neighbors]
    assert min(tds) <= est.td <= max(tds)
    assert all(e.td < 50 for e, _ in est.neighbors)  # transparent outlier excluded


def test_estimate_glow_only_from_glow(edb):
    est = estimate_td(edb, "Sunlu", "PLA", "Yellow (Glow Yellow)", "#FFFF00")
    assert {e.color_name for e, _ in est.neighbors} == {"Glow in the Dark", "Red"}
    assert est.td > 8


def test_estimate_other_brands_when_brand_unknown(edb):
    est = estimate_td(edb, "Acme", "PLA", "Cyan", "#39D0F1")
    assert est.scope == "other brands" and est.neighbors[0][0].color_name == "Sky Blue"
    assert estimate_td(edb, "Acme", "PETG", "Cyan", "#39D0F1") is None  # no PETG measured at all


def test_finish_falls_back_to_plain_but_glow_does_not(edb):
    matte = estimate_td(edb, "Sunlu", "PLA", "Matte Blue", "#006AB8")
    assert matte is not None and matte.neighbors[0][0].color_name == "Blue"
    assert estimate_td(edb, "Acme", "PLA", "Glow Green", "#00FF00").neighbors[0][0].color_name in ("Glow in the Dark", "Red")


def _f(td=None, src=None, name="Cyan", hexc="39D0F1", material="PLA+"):
    return Filament(id=1, name=name, vendor="Sunlu", material=material, hex=hexc, td=td, td_source=src)


def test_proposals_precedence(edb):
    p = service.propose_td(edb, _f())
    assert p.kind == "estimate" and p.source.startswith("estimate:")
    assert service.propose_td(edb, _f(td=3.0, src="measured:td1:2026-10-16")) is None  # yours: untouched
    assert service.propose_td(edb, _f(td=p.td, src=p.source)) is None  # same estimate: nothing new
    # A published match replaces an estimate, but an estimate never replaces a published value.
    red = service.propose_td(edb, _f(td=4.4, src="estimate:x", name="Red", hexc="AC3638", material="PLA"))
    assert red.kind == "high" and red.td == 5.7
    assert service.propose_td(edb, _f(td=4.0, src="lookup:x", name="Cyan")) is None


def test_far_same_brand_colors_defer_to_close_other_brands(tmp_path):
    entries = [
        dict(brand="Sunlu", material="PETG", color_name="White", hex="#DBDDD9", td=7.6, source="filamentcolors", source_url="a", source_date="d"),
        dict(brand="Sunlu", material="PETG", color_name="Olive", hex="#707D63", td=0.2, source="filamentcolors", source_url="b", source_date="d"),
        dict(brand="Prusament", material="PETG", color_name="Jet Black", hex="#1A1A1A", td=0.3, source="prusa-kb", source_url="c", source_date="d"),
        dict(brand="Prusament", material="PETG", color_name="Black", hex="#202020", td=0.4, source="prusa-kb", source_url="e", source_date="d"),
    ]
    p = tmp_path / "td.yaml"
    TDDatabase.write_new(p, entries, updated="d")
    est = estimate_td(TDDatabase.load(p), "Sunlu", "PETG", "High Speed Matte PETG - Black", "#151616")
    assert est.scope == "other brands" and est.td < 1.0
