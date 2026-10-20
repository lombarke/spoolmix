from spoolmix import design
from spoolmix.project3mf import DesignFilament
from spoolmix.solver import Component, Solver

STOCK = [Component("s:c", "Cyan", (0, 195, 255), 5.5), Component("s:m", "Magenta", (245, 67, 153), 5.5),
         Component("s:y", "Yellow", (250, 231, 39), 9.5), Component("s:w", "White", (245, 244, 242), 6.5),
         Component("s:k", "Black", (20, 20, 20), 0.1), Component("s:r", "Red", (172, 54, 56), 5.7)]


def _design():
    return [DesignFilament(1, "#F5F4F2", "PLA", "", 900.0),  # white body (almost exact match)
            DesignFilament(2, "#2E8B57", "PLA", "", 80.0),  # green
            DesignFilament(3, "#7A2E8C", "PLA", "", 15.0),  # purple detail
            DesignFilament(4, "#141414", "PLA", "", 5.0),  # eyes
            DesignFilament(5, "#FF0000", "PLA", "", 0.0)]  # unused


def test_plan_uses_filament_directly_when_close_and_maps_all():
    base, _ = design.plan(Solver(), _design(), STOCK)
    assert len(base.result.slots) == 4
    rec = dict(zip([t.id for t in base.targets], base.result.recipes))
    assert rec[1].size == 1 and rec[1].parts[0].label == "White"
    asg = base.assignment(_design())
    assert set(asg.targets) == {1, 2, 3, 4, 5}
    assert all(1 <= v <= 4 + len(asg.mixed_rows) for v in asg.targets.values())
    assert len(asg.physical) == 4


def test_important_color_gets_better():
    d = _design()
    plain, _ = design.plan(Solver(), d, STOCK)
    imp, _ = design.plan(Solver(), d, STOCK, important={3})
    de = lambda p: dict(zip([t.id for t in p.targets], [r.delta_e for r in p.result.recipes]))[3]
    assert de(imp) <= de(plain)


def test_shop_option_for_missing_color():
    d = [DesignFilament(1, "#2850E0", "PLA", "", 500.0), DesignFilament(2, "#FFFFFF", "PLA", "", 500.0)]
    stock = [STOCK[3], STOCK[4], STOCK[5]]
    blue = Component("shop:blue", "Blue", (40, 80, 224), 3.0, owned=False)
    base, opts = design.plan(Solver(), d, stock, [blue], max_buy=1)
    assert opts and opts[0].buy == (blue,)
    assert opts[0].result.weighted < base.result.weighted - 0.5


def test_more_slots_use_more_filaments_and_number_mixes_after_them():
    d = _design()
    four, _ = design.plan(Solver(), d, STOCK, slots=4)
    six, _ = design.plan(Solver(), d, STOCK, slots=6)
    assert len(six.result.slots) <= 6 and six.result.weighted <= four.result.weighted + 1e-9
    asg = six.assignment(d, 6)
    assert len(asg.physical) == 6
    assert all(v <= 6 or v >= 7 for v in asg.targets.values())
    mixes = [v for v in asg.targets.values() if v > 6]
    assert all(7 <= v <= 6 + len(asg.mixed_rows) for v in mixes)


def test_two_slots_still_plans():
    base, _ = design.plan(Solver(), _design(), STOCK, slots=2)
    assert len(base.result.slots) == 2


def test_only_one_purchase_suggestion_per_color_family():
    d = [DesignFilament(1, "#6F5034", "PLA", "", 60.0), DesignFilament(2, "#FEC600", "PLA", "", 40.0)]
    stock = [STOCK[3], STOCK[4], STOCK[5]]
    yellows = [Component(f"shop:y{i}", f"Yellow {i}", rgb, 5.0, owned=False)
               for i, rgb in enumerate([(254, 198, 0), (252, 194, 0), (253, 199, 0), (255, 204, 0)])]
    _, opts = design.plan(Solver(), d, stock, yellows, max_buy=1)
    assert len(opts) == 1 and design.purchase_family(opts[0].buy) == ("yellow",)


def test_simplest_no_mix_option_buy_the_missing_color():
    # Brown body (most of the surface) + colors you already own: the sane answer is
    # "buy a brown, print everything plain", not mix brown from black/red/yellow.
    d = [DesignFilament(1, "#6F5034", "PLA", "", 63.0), DesignFilament(2, "#000000", "PLA", "", 1.0),
         DesignFilament(3, "#FEC600", "PLA", "", 4.0), DesignFilament(4, "#FFFFFF", "PLA", "", 32.0)]
    stock = [Component("s:k", "Black", (0, 0, 0), 0.1, material="PLA"),
             Component("s:w", "White", (255, 255, 255), 7.0, material="PLA"),
             Component("s:y", "Light Gold", (250, 221, 71), 6.0, material="PLA"),
             Component("s:r", "Red", (172, 54, 56), 5.7, material="PLA"),
             Component("s:m", "Magenta", (255, 0, 255), 3.5, material="PLA")]
    brown = Component("shop:brown", "Cocoa Brown", (111, 80, 52), 1.0, owned=False, material="PLA")
    yellow = Component("shop:yel", "Sunflower Yellow", (254, 198, 0), 7.0, owned=False, material="PLA")
    _, opts = design.plan(Solver(), d, stock, [brown, yellow], max_buy=2)
    first = opts[0]
    assert first.simplest and first.buy == (brown,)
    assert all(r.size == 1 for r in first.result.recipes)


def _hex(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def test_buy_the_big_missing_color_but_keep_the_eyes():
    """A duck that's half brown, with black eyes, and no brown in stock: buying brown is
    worth it (most of the print becomes solid color) even though the small green patch
    then mixes worse, but not at the cost of the eyes."""
    stock = [Component(f"s:{n}", n, _hex(h), td, material="PLA") for n, h, td in (
        ("Jade White", "FFFFFF", 6.4), ("Black", "000000", 0.3), ("Cyan", "0086D6", 3.6), ("Magenta", "EC008C", 4.6),
        ("Yellow", "F4EE2A", 6.1), ("Red", "C12E1F", 2.9), ("Blue", "0A2989", 2.1), ("Orange", "FF6A13", 6.5),
        ("Bambu Green", "00AE42", 3.1), ("Pink", "F55A74", 5.2), ("Beige", "F7E6DE", 6.5), ("Silk Gold", "E5B03D", 2.7))]
    duck = [DesignFilament(1, "#000000", "PLA", "", 0.5), DesignFilament(2, "#FFFFFF", "PLA", "", 31.3),
            DesignFilament(3, "#EFBA3D", "PLA", "", 12.4), DesignFilament(4, "#008000", "PLA", "", 6.8),
            DesignFilament(5, "#5F3618", "PLA", "", 49.0)]
    brown = Component("shop:brown", "Brown", _hex("55331A"), 1.0, owned=False, material="PLA")
    base, opts = design.plan(Solver(), duck, stock, [brown], max_buy=1)
    assert opts and opts[0].buy == (brown,)
    best = opts[0].result
    assert best.score < base.result.score
    eyes = best.recipes[0]
    assert eyes.size == 1 and eyes.parts[0].label == "Black"


def test_purchase_may_not_wreck_a_small_detail():
    d = [DesignFilament(1, "#000000", "PLA", "", 1.0), DesignFilament(2, "#5F3618", "PLA", "", 99.0)]
    assert design.protected_colors(d, set()) == [True, False]
    assert design.protected_colors(d, {2}) == [True, True]


def _green_duck():
    """A mostly green duck, some white and red, black eyes; stock has only white
    and two blacks in that material."""
    duck = [DesignFilament(1, "#21FF06", "PETG", "", 77.4), DesignFilament(2, "#FFFFFF", "PETG", "", 12.6),
            DesignFilament(3, "#FF0000", "PETG", "", 9.5), DesignFilament(4, "#000000", "PETG", "", 0.5)]
    stock = [Component("s:w", "Overture PETG White", _hex("F5F5F4"), 3.8, material="PETG"),
             Component("s:k1", "Sunlu PETG Black", _hex("151616"), 0.3, material="PETG"),
             Component("s:k2", "Overture PETG Black", _hex("000000"), 0.3, material="PETG")]
    greens = [Component(f"shop:g{i}", f"Green {i}", _hex(h), 3.0, owned=False, material="PETG")
              for i, h in enumerate(["21F006", "2AE80F", "19FF20", "30F000", "22EE22", "1EF510"])]
    red = Component("shop:red", "Red", _hex("F01010"), 3.0, owned=False, material="PETG")
    return duck, stock, greens + [red]


def test_unused_filaments_are_not_loaded():
    duck, stock, _ = _green_duck()
    base, _ = design.plan(Solver(), duck, stock)
    used = {p.key for r in base.result.recipes for p in r.parts}
    assert {c.key for c in base.result.slots} == used  # no second black just to fill a slot


def test_shortlist_gives_every_bad_color_a_candidate():
    duck, stock, catalog = _green_duck()
    _, opts = design.plan(Solver(), duck, stock, catalog, max_buy=2)
    # first suggestion: a green and the red, printed plain (before, the red was never evaluated)
    best = opts[0]
    assert len(best.buy) == 2 and "Red" in {c.label for c in best.buy} and best.simplest
    assert best.result.worst < 5


def test_no_purchase_of_a_color_you_already_have():
    duck, stock, catalog = _green_duck()
    near_white = Component("shop:white", "Another White", _hex("FFFFFF"), 4.0, owned=False, material="PETG")
    _, opts = design.plan(Solver(), duck, stock, [*catalog, near_white], max_buy=2)
    assert not any(c.label == "Another White" for o in opts for c in o.buy)
