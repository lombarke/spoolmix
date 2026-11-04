"""Color utilities: hex parsing, sRGB -> CIELAB (D65), CIEDE2000, terminal swatches.

The sRGB -> Lab conversion uses the same constants as Snapmaker Orca's
ColorSpaceConvert (sRGB companding, D65 white 95.047/100/108.883, 0.008856
threshold), so Lab values agree with the slicer.

CIEDE2000 is the standard Sharma/Wu/Dalal (2005) formulation. Orca's own
DeltaE00 differs only in a hue-wraparound edge case (it always adds 360 when
|h1 - h2| > 180), which can shift results slightly for pairs straddling
red/magenta hues.
"""

from __future__ import annotations

import re

import numpy as np

RGB = tuple[int, int, int]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

_REF_WHITE = np.array([95.047, 100.000, 108.883])
_M_RGB_TO_XYZ = np.array(
    [
        [0.412453, 0.357580, 0.180423],
        [0.212671, 0.715160, 0.072169],
        [0.019334, 0.119193, 0.950227],
    ]
)


def parse_hex(value: str) -> RGB:
    """Parse '#RRGGBB', 'RRGGBB' or '#RRGGBBAA' (alpha dropped) into an RGB tuple."""
    m = _HEX_RE.match(value.strip())
    if not m:
        raise ValueError(f"not a hex color: {value!r}")
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def srgb_to_lab(rgb) -> np.ndarray:
    """Convert sRGB (0-255) to CIELAB. Accepts shape (3,) or (N, 3)."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92) * 100.0
    xyz = lin @ _M_RGB_TO_XYZ.T
    f = xyz / _REF_WHITE
    f = np.where(f > 0.008856, np.cbrt(f), 7.787 * f + 16.0 / 116.0)
    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def delta_e2000(lab1, lab2) -> np.ndarray | float:
    """CIEDE2000 between Lab arrays (broadcastable, last axis = L,a,b)."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar7 = ((C1 + C2) / 2.0) ** 7
    G = 0.5 * (1.0 - np.sqrt(Cbar7 / (Cbar7 + 25.0**7)))
    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p
    zero = (C1p * C2p) == 0
    dh = h2p - h1p
    dh = np.where(dh > 180.0, dh - 360.0, np.where(dh < -180.0, dh + 360.0, dh))
    dh = np.where(zero, 0.0, dh)
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh) / 2.0)

    Lbarp = (L1 + L2) / 2.0
    Cbarp = (C1p + C2p) / 2.0
    hsum = h1p + h2p
    hbarp = np.where(
        zero,
        hsum,
        np.where(
            np.abs(h1p - h2p) <= 180.0,
            hsum / 2.0,
            np.where(hsum < 360.0, (hsum + 360.0) / 2.0, (hsum - 360.0) / 2.0),
        ),
    )
    T = (
        1.0
        - 0.17 * np.cos(np.radians(hbarp - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * hbarp))
        + 0.32 * np.cos(np.radians(3.0 * hbarp + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * hbarp - 63.0))
    )
    dtheta = 30.0 * np.exp(-(((hbarp - 275.0) / 25.0) ** 2))
    Cbarp7 = Cbarp**7
    RC = 2.0 * np.sqrt(Cbarp7 / (Cbarp7 + 25.0**7))
    SL = 1.0 + (0.015 * (Lbarp - 50.0) ** 2) / np.sqrt(20.0 + (Lbarp - 50.0) ** 2)
    SC = 1.0 + 0.045 * Cbarp
    SH = 1.0 + 0.015 * Cbarp * T
    RT = -np.sin(np.radians(2.0 * dtheta)) * RC

    de = np.sqrt(
        (dLp / SL) ** 2 + (dCp / SC) ** 2 + (dHp / SH) ** 2 + RT * (dCp / SC) * (dHp / SH)
    )
    return float(de) if de.ndim == 0 else de


def delta_e_rgb(rgb1: RGB, rgb2: RGB) -> float:
    return float(delta_e2000(srgb_to_lab(rgb1), srgb_to_lab(rgb2)))


def swatch(rgb: RGB, width: int = 4) -> str:
    """A truecolor block for rich markup."""
    return f"[on {to_hex(rgb).lower()}]{' ' * width}[/]"


def is_near_black(rgb: RGB, l_threshold: float = 20.0) -> bool:
    return float(srgb_to_lab(rgb)[0]) < l_threshold


def lightness(rgb: RGB) -> float:
    return float(srgb_to_lab(rgb)[0])


def hue_name(rgb: RGB) -> str:
    """Rough color-family name, used for shopping hints ('Add a yellow')."""
    r, g, b = (x / 255.0 for x in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 0.08:
        return "black" if mx < 0.2 else "white" if mx > 0.85 else "gray"
    if mx == r:
        h = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        h = (b - r) / (mx - mn) + 2
    else:
        h = (r - g) / (mx - mn) + 4
    deg = h * 60.0
    for limit, name in (
        (15, "red"), (40, "orange"), (70, "yellow"), (160, "green"),
        (200, "cyan"), (255, "blue"), (290, "purple"), (335, "magenta"), (360, "red"),
    ):
        if deg < limit:
            return name
    return "red"


__all__ = [
    "RGB", "parse_hex", "to_hex", "srgb_to_lab", "delta_e2000", "delta_e_rgb",
    "swatch", "is_near_black", "lightness", "hue_name",
]
