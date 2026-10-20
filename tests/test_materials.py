import pytest

from spoolmix import design
from spoolmix.materials import all_compatible, category, compatible
from spoolmix.project3mf import DesignFilament
from spoolmix.solver import Component, RecipeTable, Solver, palette_search


@pytest.mark.parametrize("name, cat", [
    ("PLA", "PLA"), ("PLA+", "PLA"), ("Silk PLA+", "PLA"), ("PLA Meta", "PLA"), ("High Speed PLA", "PLA"),
    ("PLA Full Spectrum", "PLA"), ("pla-cf", "PLA"), ("PETG", "PETG"), ("PCTG", "PETG"), ("PETG-CF", "PETG"),
    ("TPU 95A", "TPU"), ("ABS", "ABS"), ("ASA", "ASA"), ("PA6-CF", "PA"), ("PVA", "SUPPORT"), ("", None),
])
def test_category(name, cat):
    assert category(name) == cat


def test_compatibility_matches_snapmaker_table():
    assert compatible("PLA", "PLA+")
    assert not compatible("PLA", "PETG")
    assert not compatible("PLA+", "TPU")
    assert not compatible("PETG", "TPU")  # Snapmaker allows it; removed locally (no flexible layers in rigid mixes)
    assert not compatible("TPU 95A", "PCTG")
    assert compatible("PLA", "PC")
    assert not compatible("PVA", "PLA")
    assert compatible("Wood", "Wood") and not compatible("Wood", "PLA")  # unknown: only itself
    assert compatible(None, "PETG")  # no information
    assert not all_compatible(["PLA", "PLA+", "PETG"])


PLA_W = Component("w", "PLA White", (245, 244, 242), 6.5, material="PLA")
PLA_K = Component("k", "PLA+ Black", (20, 20, 20), 0.1, material="PLA+")
PETG_B = Component("b", "PETG Blue", (40, 80, 224), 3.0, material="PETG")
PETG_Y = Component("y", "PETG Yellow", (250, 231, 39), 9.5, material="PETG")
PLA_R = Component("r", "PLA Red", (172, 54, 56), 5.7, material="PLA")


def test_solver_never_mixes_pla_and_petg():
    target = (120, 140, 150)  # reachable best by mixing blue with white/black
    for r in Solver().solve(target, [PLA_W, PLA_K, PETG_B, PETG_Y, PLA_R], top=20):
        assert all_compatible(p.material for p in r.parts), [p.label for p in r.parts]


def test_palette_is_mutually_compatible():
    targets = [(40, 80, 224), (245, 244, 242), (200, 60, 60)]
    table = RecipeTable.build(Solver(), targets, [PLA_W, PLA_K, PETG_B, PETG_Y, PLA_R])
    res = palette_search(table, 4)
    assert all_compatible(c.material for c in res.slots)


def test_design_uses_only_design_material():
    d = [DesignFilament(1, "#2850E0", "PLA", "", 50.0), DesignFilament(2, "#FFFFFF", "PLA", "", 50.0)]
    base, _ = design.plan(Solver(), d, [PLA_W, PLA_K, PETG_B, PETG_Y, PLA_R])
    assert all(category(c.material) == "PLA" for c in base.result.slots)
    with pytest.raises(ValueError):
        design.plan(Solver(), [DesignFilament(1, "#FFFFFF", "PETG", "", 1.0)], [PLA_W])


def test_multicolor_spools_are_skipped():
    from spoolmix.service import is_multicolor, stock_components
    from spoolmix.spoolman import Filament, Spool

    def fil(i, name, multi=None):
        f = Filament(id=i, name=name, vendor="X", material="PLA", hex="3E3F42", multi_color_hexes=multi)
        f.spools.append(Spool(i, i, 500.0, False, None))
        return f

    fs = [fil(1, "Panchroma Gradient Galaxy Black-Grey"), fil(2, "Rainbow"), fil(3, "Dual Color Silk Gold-Silver"),
          fil(4, "Galaxy Black"), fil(5, "Silk Bronze"), fil(6, "Black", multi="000000,FFFFFF")]
    assert [is_multicolor(f) for f in fs] == [True, True, True, False, False, True]
    assert [c.label for c in stock_components(fs)] == ["X PLA Galaxy Black", "X PLA Silk Bronze"]


def test_filament_without_material_is_not_used():
    from spoolmix.service import stock_components
    from spoolmix.spoolman import Filament, Spool

    f = Filament(id=1, name="Mystery", vendor="X", material=None, hex="FF0000")
    f.spools.append(Spool(1, 1, 500.0, False, None))
    assert stock_components([f]) == []
