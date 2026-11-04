"""Color mixing models.

OrcaModel reproduces Snapmaker Orca's Full Spectrum preview color exactly:

* Pair mix: FilamentMixer polynomial lerp(c1, c2, t) with t = share of c2,
  computed on sRGB 0-255 inputs, each output channel truncated toward zero
  and clamped to 0..255. t <= 0 / t >= 1 return the endpoint unchanged.
* 3+ filaments: sequential pairwise lerp in ascending slot order. Start with
  the first color; for each next color t = w_next / (w_accumulated + w_next),
  truncating to uint8 after every step. Order matters, so callers pass
  components already sorted by slot.

TDModel is a spoolmix extension (Orca does not use TD in its prediction).
It rescales each component's weight by TD^-alpha before calling the same
pipeline: a low-TD (more opaque) filament hides what is beneath it and so
contributes more to the visible color than its layer share. alpha = 0 is
identical to OrcaModel. alpha is fitted by `spoolmix calibrate fit`.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np

from spoolmix.color import RGB
from spoolmix.mixer_coeffs import COEF, INTERCEPT

def _build_feature_plan() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Monomials in sklearn PolynomialFeatures order, plus (parent, var) so that
    feature[i] = feature[parent[i]] * x[var[i]]."""
    combos = [c for d in range(5) for c in itertools.combinations_with_replacement(range(7), d)]
    index = {c: i for i, c in enumerate(combos)}
    powers = np.array([[c.count(v) for v in range(7)] for c in combos], dtype=np.int64)
    parent = np.array([index[c[:-1]] if c else -1 for c in combos], dtype=np.int64)
    var = np.array([c[-1] if c else -1 for c in combos], dtype=np.int64)
    return powers, parent, var


_POWERS, _PARENT, _VAR = _build_feature_plan()
_COEF = np.array(COEF, dtype=np.float64)  # (330, 3)
_INTERCEPT = np.array(INTERCEPT, dtype=np.float64)
assert _POWERS.shape == (330, 7) and _COEF.shape == (330, 3)

DEFAULT_TD = 5.0  # assumed for filaments with unknown TD in the TD model
DEFAULT_TD_ALPHA = 0.5  # uncalibrated guess; replaced by calibration
_CHUNK = 16384


def _poly(x: np.ndarray) -> np.ndarray:
    feats = np.empty((x.shape[0], len(_PARENT)), dtype=np.float64)
    feats[:, 0] = 1.0
    for i in range(1, len(_PARENT)):
        np.multiply(feats[:, _PARENT[i]], x[:, _VAR[i]], out=feats[:, i])
    return feats @ _COEF + _INTERCEPT


def lerp_batch(c1: np.ndarray, c2: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Vectorized FilamentMixer lerp. c1, c2: (N, 3) ints 0-255; t: (N,). Returns (N, 3) uint8.

    Floating-point summation order differs from the C++ loop, so a channel can
    differ by 1/255 in rare cases where the raw value lands within ~1e-9 of an
    integer before truncation.
    """
    c1 = np.asarray(c1, dtype=np.float64).reshape(-1, 3)
    c2 = np.asarray(c2, dtype=np.float64).reshape(-1, 3)
    # Orca passes t as a C float; round-trip through float32 to match.
    t = np.asarray(t, dtype=np.float32).astype(np.float64).reshape(-1)
    n = max(len(c1), len(c2), len(t))
    c1 = np.broadcast_to(c1, (n, 3))
    c2 = np.broadcast_to(c2, (n, 3))
    t = np.broadcast_to(t, (n,))

    x = np.concatenate([c1, c2, t[:, None]], axis=1)  # (N, 7)
    out = np.empty((n, 3), dtype=np.float64)
    for start in range(0, n, _CHUNK):
        out[start : start + _CHUNK] = _poly(x[start : start + _CHUNK])
    out = np.clip(np.trunc(out), 0, 255)

    lo = t <= 0.0
    hi = t >= 1.0
    out[lo] = c1[lo]
    out[hi] = c2[hi]
    return out.astype(np.uint8)


def lerp(c1: RGB, c2: RGB, t: float) -> RGB:
    r, g, b = lerp_batch(np.array([c1]), np.array([c2]), np.array([t]))[0]
    return int(r), int(g), int(b)


def mix_sequential_batch(colors: Sequence[RGB], weights: np.ndarray) -> np.ndarray:
    """Mix k colors (in slot order) for N weight rows. weights: (N, k). Returns (N, 3) uint8.

    Matches Orca's blend_multi_filament_mixer: zero-weight components are skipped.
    """
    weights = np.asarray(weights, dtype=np.float64)
    if weights.ndim == 1:
        weights = weights[None, :]
    n, k = weights.shape
    cols = np.array(colors, dtype=np.float64)
    out = np.zeros((n, 3), dtype=np.float64)
    acc = np.zeros(n, dtype=np.float64)
    for i in range(k):
        w = np.maximum(weights[:, i], 0.0)
        active = w > 0
        first = active & (acc <= 0)
        out[first] = cols[i]
        acc[first] = w[first]
        rest = active & ~first
        if rest.any():
            total = acc[rest] + w[rest]
            t = w[rest] / total
            out[rest] = lerp_batch(out[rest], np.broadcast_to(cols[i], (int(rest.sum()), 3)), t)
            acc[rest] = total
    return out.astype(np.uint8)


class MixModel(Protocol):
    name: str

    def weight_factors(self, tds: Sequence[float | None]) -> np.ndarray: ...

    def mix_batch(self, colors: Sequence[RGB], tds: Sequence[float | None], weights: np.ndarray) -> np.ndarray: ...


@dataclass
class OrcaModel:
    """Exact Snapmaker Orca preview prediction (TD ignored)."""

    name: str = "orca"

    def weight_factors(self, tds: Sequence[float | None]) -> np.ndarray:
        return np.ones(len(tds), dtype=np.float64)

    def mix_batch(self, colors, tds, weights):
        return mix_sequential_batch(colors, weights)

    def mix(self, colors: Sequence[RGB], tds: Sequence[float | None], weights: Sequence[float]) -> RGB:
        r, g, b = self.mix_batch(colors, tds, np.array([weights], dtype=np.float64))[0]
        return int(r), int(g), int(b)


@dataclass
class TDModel:
    """TD-weighted variant: effective weight = w * TD^-alpha."""

    alpha: float = DEFAULT_TD_ALPHA
    default_td: float = DEFAULT_TD
    calibrated: bool = False
    name: str = "td"

    def weight_factors(self, tds: Sequence[float | None]) -> np.ndarray:
        td = np.array([t if t and t > 0 else self.default_td for t in tds], dtype=np.float64)
        return td ** (-self.alpha)

    def effective_weights(self, tds: Sequence[float | None], weights: np.ndarray) -> np.ndarray:
        return np.asarray(weights, dtype=np.float64) * self.weight_factors(tds)[None, :]

    def mix_batch(self, colors, tds, weights):
        return mix_sequential_batch(colors, self.effective_weights(tds, np.atleast_2d(weights)))

    def mix(self, colors: Sequence[RGB], tds: Sequence[float | None], weights: Sequence[float]) -> RGB:
        r, g, b = self.mix_batch(colors, tds, np.array([weights], dtype=np.float64))[0]
        return int(r), int(g), int(b)


@dataclass
class CalibratedModel(TDModel):
    """Per-filament strengths fitted from printed swatches.

    A filament with a fitted strength k contributes weight * k. Filaments without
    one fall back to TD^-alpha, scaled to the fitted filaments so the two kinds
    of factor are comparable.
    """

    strengths: dict[str, float] = field(default_factory=dict)
    td_scale: float = 1.0
    name: str = "calibrated"

    def component_factors(self, comps) -> np.ndarray:
        out = []
        for c in comps:
            if c.key in self.strengths:
                out.append(self.strengths[c.key])
            else:
                td = c.td if c.td and c.td > 0 else self.default_td
                out.append(self.td_scale * td ** (-self.alpha))
        return np.array(out, dtype=np.float64)


def get_model(name: str, alpha: float | None = None, calibrated: bool = False) -> OrcaModel | TDModel:
    if name == "orca":
        return OrcaModel()
    if name == "td":
        return TDModel(alpha=DEFAULT_TD_ALPHA if alpha is None else alpha, calibrated=calibrated)
    if name == "calibrated":
        return CalibratedModel(alpha=DEFAULT_TD_ALPHA if alpha is None else alpha, calibrated=calibrated)
    raise ValueError(f"unknown model {name!r} (expected 'orca' or 'td')")
