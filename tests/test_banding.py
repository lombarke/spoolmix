from spoolmix.model import OrcaModel
from spoolmix.report import recipe_warnings
from spoolmix.solver import Component, Recipe, Solver

BLACK = Component("k", "Black", (0, 0, 0), 0.1)
YELLOW = Component("y", "Yellow", (255, 255, 0), 10.7)
WHITE = Component("w", "White", (199, 205, 215), 7.1)
FS_CYAN = Component("c", "FS Cyan", (0, 195, 255), 5.5)
FS_YELLOW = Component("fy", "FS Yellow", (250, 231, 39), 9.5)


def _recipe(parts, w):
    rgb = OrcaModel().mix([p.rgb for p in parts], [p.td for p in parts], w)
    return Recipe(tuple(parts), tuple(w), rgb, 0.0)


def test_opaque_contrasting_mix_is_high():
    r = _recipe([WHITE, BLACK, YELLOW], [16, 53, 31])
    assert r.stripe_level == "high"
    w = recipe_warnings(r)
    assert any(x.startswith("Won't blend well: Black (TD 0.1)") for x in w)


def test_translucent_cmy_mix_blends():
    r = _recipe([FS_CYAN, FS_YELLOW], [50, 50])
    assert r.stripe_level == "low"
    assert not any("blend" in x or "banding" in x for x in recipe_warnings(r))


def test_single_filament_has_no_banding():
    r = Recipe((WHITE,), (100,), WHITE.rgb, 1.0)
    assert r.stripe == 0.0 and r.stripe_level == "low"


def test_unknown_td_counts_as_fairly_opaque():
    unknown = Component("u", "Mystery", (0, 0, 0), None)
    assert _recipe([WHITE, unknown], [70, 30]).stripe > _recipe([WHITE, BLACK.__class__("t", "Dark translucent", (0, 0, 0), 9.0)], [70, 30]).stripe


def test_solver_prefers_blending_option_when_colors_are_close():
    # Same target reachable two ways: black+white (opaque black) or grey+white (translucent grey).
    grey = Component("g", "Grey", (128, 128, 128), 8.0)
    white = Component("w2", "White", (245, 244, 242), 6.5)
    target = OrcaModel().mix([grey.rgb, white.rgb], [None, None], [60, 40])
    best = Solver().solve(target, [BLACK, grey, white], top=1)[0]
    assert BLACK not in best.parts
    assert best.stripe_level == "low"


def test_lopsided_pairs_stripe_more_matching_the_wall_test():
    """Side-wall prints (cyan/yellow PLA, 0.1 mm layers): 80/20 striped far more than the
    balanced ratios, and more than 20/80."""
    from spoolmix.model import mix_sequential_batch
    import numpy as np

    cyan = Component("c", "Cyan", (0x39, 0xD0, 0xF1), 3.7)
    yellow = Component("y", "Yellow", (0xF6, 0xFA, 0x00), 6.2)

    def level(p):
        rgb = tuple(int(v) for v in mix_sequential_batch([cyan.rgb, yellow.rgb], np.array([[100 - p, p]], float))[0])
        r = Recipe((cyan, yellow), (100 - p, p), rgb, 0.0)
        return r.stripe, r.stripe_level

    assert level(20)[1] == "high"  # 80/20 cyan/yellow
    assert all(level(p)[1] == "moderate" for p in (35, 50, 65, 80))
    assert level(20)[0] > level(80)[0] > level(50)[0]


def test_lopsided_factor_leaves_triples_alone():
    from spoolmix.solver import lopsided_factor
    import numpy as np

    assert lopsided_factor(np.array([80.0, 10.0, 10.0]), np.array([0.3, 0.3, 0.3])) == 1.0
    assert lopsided_factor(np.array([50.0, 50.0]), np.array([0.3, 0.1])) == 1.0
    assert lopsided_factor(np.array([80.0, 20.0]), np.array([0.3, 0.1])) > 2.0
