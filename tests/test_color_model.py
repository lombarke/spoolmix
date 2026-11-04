import numpy as np
import pytest

from spoolmix.color import delta_e2000, delta_e_rgb, parse_hex, srgb_to_lab, to_hex
from spoolmix.model import OrcaModel, TDModel, lerp, lerp_batch, mix_sequential_batch

# --- hex parsing -----------------------------------------------------------


def test_parse_hex_variants():
    assert parse_hex("#1F4D2B") == (0x1F, 0x4D, 0x2B)
    assert parse_hex("1f4d2b") == (0x1F, 0x4D, 0x2B)
    assert parse_hex("#1F4D2BFF") == (0x1F, 0x4D, 0x2B)
    assert to_hex((31, 77, 43)) == "#1F4D2B"
    with pytest.raises(ValueError):
        parse_hex("#12345")


# --- Lab -------------------------------------------------------------------


@pytest.mark.parametrize(
    "rgb, lab",
    [
        ((255, 255, 255), (100.0, 0.0, 0.0)),
        ((0, 0, 0), (0.0, 0.0, 0.0)),
        ((255, 0, 0), (53.24, 80.09, 67.20)),
        ((0, 0, 255), (32.30, 79.19, -107.86)),
    ],
)
def test_srgb_to_lab(rgb, lab):
    assert np.allclose(srgb_to_lab(rgb), lab, atol=0.05)


# --- CIEDE2000: Sharma, Wu, Dalal (2005) test data -------------------------

SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((50.0, 2.4900, -0.0010), (50.0, -2.4900, 0.0009), 7.1792),
    ((50.0, 2.4900, -0.0010), (50.0, -2.4900, 0.0010), 7.1792),
    ((50.0, 2.4900, -0.0010), (50.0, -2.4900, 0.0011), 7.2195),
    ((50.0, -0.0010, 2.4900), (50.0, 0.0009, -2.4900), 4.8045),
    ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((50.0, 2.5, 0.0), (61.0, -5.0, 29.0), 22.8977),
    ((50.0, 2.5, 0.0), (56.0, -27.0, -3.0), 31.9030),
    ((50.0, 2.5, 0.0), (58.0, 24.0, 15.0), 19.4535),
    ((50.0, 2.5, 0.0), (50.0, 3.1736, 0.5854), 1.0000),
    ((50.0, 2.5, 0.0), (50.0, 3.2972, 0.0), 1.0000),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
]


@pytest.mark.parametrize("lab1, lab2, expected", SHARMA)
def test_ciede2000_sharma(lab1, lab2, expected):
    assert delta_e2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)
    assert delta_e2000(lab2, lab1) == pytest.approx(expected, abs=1e-4)


def test_ciede2000_vectorized():
    a = np.array([s[0] for s in SHARMA])
    b = np.array([s[1] for s in SHARMA])
    assert np.allclose(delta_e2000(a, b), [s[2] for s in SHARMA], atol=1e-4)


def test_delta_e_identity():
    assert delta_e_rgb((12, 200, 99), (12, 200, 99)) == 0.0


# --- FilamentMixer golden values (pinned by Snapmaker Orca's
#     tests/libslic3r/test_mixed_filament_color_golden.cpp) -------------------


def test_lerp_documented_example():
    # blue + yellow -> green
    assert lerp((0, 33, 133), (252, 211, 0), 0.5) == (47, 141, 56)


@pytest.mark.parametrize(
    "t, expected",
    [(0.0, (10, 20, 30)), (1.0, (200, 210, 220)), (-0.5, (10, 20, 30)), (1.5, (200, 210, 220))],
)
def test_lerp_endpoints_and_clamp(t, expected):
    assert lerp((10, 20, 30), (200, 210, 220), t) == expected


def test_lerp_batch_matches_scalar():
    c1 = np.array([[0, 33, 133], [255, 0, 0], [0, 195, 255]])
    c2 = np.array([[252, 211, 0], [255, 255, 255], [245, 67, 153]])
    t = np.array([0.5, 0.3, 0.7])
    out = lerp_batch(c1, c2, t)
    for i in range(3):
        assert tuple(out[i]) == lerp(tuple(c1[i]), tuple(c2[i]), float(t[i]))


def test_lerp_output_in_range():
    rng = np.random.default_rng(0)
    c1 = rng.integers(0, 256, (500, 3))
    c2 = rng.integers(0, 256, (500, 3))
    t = rng.random(500)
    out = lerp_batch(c1, c2, t)
    assert out.dtype == np.uint8 and out.shape == (500, 3)


# --- multi-filament sequential mixing --------------------------------------


def test_two_component_sequential_equals_lerp():
    a, b = (0, 195, 255), (250, 231, 39)
    out = mix_sequential_batch([a, b], np.array([[60, 40]]))[0]
    assert tuple(out) == lerp(a, b, 40 / 100)


def test_sequential_triple_is_pairwise_chain():
    a, b, c = (0, 195, 255), (245, 67, 153), (250, 231, 39)
    first = lerp(a, b, 30 / (40 + 30))
    expected = lerp(first, c, 30 / 100)
    assert tuple(mix_sequential_batch([a, b, c], np.array([[40, 30, 30]]))[0]) == expected


def test_triple_order_dependence():
    a, b, c = (0, 195, 255), (245, 67, 153), (250, 231, 39)
    abc = mix_sequential_batch([a, b, c], np.array([[34, 33, 33]]))[0]
    cba = mix_sequential_batch([c, b, a], np.array([[33, 33, 34]]))[0]
    # Same proportions, different slot order: Orca's result is order-dependent.
    assert tuple(abc) != tuple(cba)


def test_zero_weight_component_skipped():
    a, b, c = (0, 195, 255), (245, 67, 153), (250, 231, 39)
    out = mix_sequential_batch([a, b, c], np.array([[50, 0, 50]]))[0]
    assert tuple(out) == lerp(a, c, 0.5)


# --- TD model ---------------------------------------------------------------


def test_td_model_alpha_zero_equals_orca():
    cols = [(0, 195, 255), (250, 231, 39)]
    w = [45, 55]
    assert TDModel(alpha=0.0).mix(cols, [5.5, 9.5], w) == OrcaModel().mix(cols, [5.5, 9.5], w)


def test_td_model_favors_opaque_component():
    cyan, yellow = (0, 195, 255), (250, 231, 39)
    orca = OrcaModel().mix([cyan, yellow], [1.0, 9.5], [50, 50])
    td = TDModel(alpha=1.0).mix([cyan, yellow], [1.0, 9.5], [50, 50])
    # Low-TD cyan should pull the TD prediction closer to cyan than Orca's.
    assert delta_e_rgb(td, cyan) < delta_e_rgb(orca, cyan)
