import json

import numpy as np
import pytest
from typer.testing import CliRunner

from spoolmix.color import delta_e_rgb, to_hex
from spoolmix.model import OrcaModel, TDModel, lerp, mix_sequential_batch
from spoolmix.solver import Component, Solver, choose_palette, shop

CYAN = Component("s:cyan", "Cyan", (0, 195, 255), 5.5)
MAGENTA = Component("s:magenta", "Magenta", (245, 67, 153), 5.5)
YELLOW = Component("s:yellow", "Yellow", (250, 231, 39), 9.5)
WHITE = Component("s:white", "White", (245, 244, 242), 6.5)
BLACK = Component("s:black", "Black", (20, 20, 20), 0.1)
RED = Component("s:red", "Red", (172, 54, 56), 5.7)
STOCK = [CYAN, MAGENTA, YELLOW, WHITE, BLACK, RED]


def test_recovers_known_pair_mix():
    target = lerp(CYAN.rgb, YELLOW.rgb, 0.35)
    r = Solver().solve(target, STOCK, top=1)[0]
    assert r.delta_e < 0.3
    assert r.keys == {CYAN.key, YELLOW.key}


def test_recovers_known_triple_mix():
    target = tuple(int(v) for v in mix_sequential_batch([CYAN.rgb, MAGENTA.rgb, YELLOW.rgb], np.array([[40, 35, 25]]))[0])
    r = Solver().solve(target, [CYAN, MAGENTA, YELLOW, WHITE], top=1)[0]
    assert r.delta_e < 0.5


@pytest.mark.parametrize("max_pct", [60, 70, 100])
def test_max_percent_cap(max_pct):
    target = (10, 150, 240)  # nearly pure cyan: tempts a >70% cyan share
    for r in Solver(max_pct=max_pct).solve(target, STOCK, top=10):
        assert max(r.weights) <= max_pct
        assert sum(r.weights) == 100


def test_min_percent():
    for r in Solver(min_pct=15).solve((120, 160, 90), STOCK, top=10):
        assert min(r.weights) >= 15


def test_predicted_color_matches_model():
    for r in Solver().solve((90, 120, 60), STOCK, top=5):
        assert OrcaModel().mix([p.rgb for p in r.parts], [p.td for p in r.parts], r.weights) == r.rgb
        assert delta_e_rgb(r.rgb, (90, 120, 60)) == pytest.approx(r.delta_e, abs=1e-6)


def test_td_model_changes_prediction():
    target = (60, 140, 120)
    orca = Solver(model=OrcaModel()).solve(target, STOCK, top=1)[0]
    td = Solver(model=TDModel(alpha=1.0)).solve(target, STOCK, top=1)[0]
    assert TDModel(alpha=1.0).mix([p.rgb for p in td.parts], [p.td for p in td.parts], td.weights) == td.rgb
    assert (orca.keys, orca.weights) != (td.keys, td.weights) or orca.rgb != td.rgb


def test_pair_preferred_over_marginally_better_triple():
    rs = Solver().solve((120, 200, 140), STOCK, top=10)
    best = rs[0]
    for r in rs[1:]:
        assert best.rank_score <= r.rank_score


def test_pairs_only():
    assert all(r.size == 2 for r in Solver(allow_triples=False).solve((100, 100, 200), STOCK, top=10))


def test_palette_uses_at_most_four_and_consistent_order():
    targets = [(40, 120, 80), (200, 90, 140), (230, 200, 120)]
    res = choose_palette(Solver(), targets, STOCK, slots=4)
    assert len(res.slots) == 4
    slot = {c.key: i for i, c in enumerate(res.slots)}
    for r in res.recipes:
        idx = [slot[p.key] for p in r.parts]
        assert idx == sorted(idx), "recipe must follow the global filament order"
    assert res.worst == max(r.delta_e for r in res.recipes)


def test_shop_finds_missing_color():
    stock = [WHITE, BLACK, RED]
    catalog = [Component("shop:cyan", "Cyan", CYAN.rgb, 5.5, owned=False),
               Component("shop:yellow", "Yellow", YELLOW.rgb, 9.5, owned=False)]
    target = lerp(CYAN.rgb, YELLOW.rgb, 0.5)  # green: impossible from white/black/red
    base, options = shop(Solver(), target, stock, catalog)
    assert base.delta_e > 10
    best = options[0]
    assert {c.key for c in best.buy} == {"shop:cyan", "shop:yellow"}
    assert best.recipe.delta_e < 1.0
    assert best.improvement == pytest.approx(base.score - best.recipe.score)  # visible error incl. mixing/banding


def test_shop_nothing_when_stock_matches():
    target = lerp(CYAN.rgb, YELLOW.rgb, 0.5)
    _, options = shop(Solver(), target, STOCK, [Component("shop:red2", "Red", (200, 30, 30), 4.0, owned=False)])
    assert options == []


def test_cli_match_json(monkeypatch):
    from spoolmix import cli

    monkeypatch.setattr(cli, "_stock_components", lambda sm: STOCK)

    class _Dummy:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(cli, "_client", lambda: _Dummy())
    target = to_hex(lerp(CYAN.rgb, YELLOW.rgb, 0.4))
    res = CliRunner().invoke(cli.app, ["match", target, "--json", "--top", "2"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    rec = data["targets"][0]["recipes"][0]
    assert rec["delta_e"] < 0.5
    assert sum(c["percent"] for c in rec["components"]) == 100
    assert rec["gradient_component_weights"].count("/") == len(rec["components"]) - 1
