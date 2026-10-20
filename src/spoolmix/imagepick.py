"""Pick target colors from an image: a pixel (3x3 average) or k-means dominant colors."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from spoolmix.color import RGB, srgb_to_lab


def _load(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.float64)


def pick_pixel(path: Path, x: int, y: int, radius: int = 1) -> RGB:
    px = _load(path)
    h, w, _ = px.shape
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError(f"pixel ({x}, {y}) outside image {w}x{h}")
    patch = px[max(0, y - radius) : y + radius + 1, max(0, x - radius) : x + radius + 1].reshape(-1, 3)
    r, g, b = np.rint(patch.mean(axis=0)).astype(int)
    return int(r), int(g), int(b)


def dominant_colors(path: Path, k: int, max_side: int = 200, iters: int = 30, seed: int = 0) -> list[tuple[RGB, float]]:
    """k-means in CIELAB. Returns (color, share of pixels), most common first."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        px = np.asarray(im, dtype=np.float64).reshape(-1, 3)
    lab = srgb_to_lab(px)
    rng = np.random.default_rng(seed)
    k = max(1, min(k, len(np.unique(px, axis=0))))

    # k-means++ init
    centers = [lab[rng.integers(len(lab))]]
    for _ in range(1, k):
        d2 = np.min(((lab[:, None, :] - np.array(centers)[None]) ** 2).sum(-1), axis=1)
        centers.append(lab[rng.choice(len(lab), p=d2 / d2.sum())])
    c = np.array(centers)
    for _ in range(iters):
        labels = ((lab[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
        new = np.array([lab[labels == i].mean(0) if np.any(labels == i) else c[i] for i in range(k)])
        if np.allclose(new, c):
            break
        c = new
    labels = ((lab[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
    out = []
    for i in range(k):
        mask = labels == i
        if not mask.any():
            continue
        r, g, b = np.rint(px[mask].mean(0)).astype(int)
        out.append(((int(r), int(g), int(b)), float(mask.mean())))
    return sorted(out, key=lambda x: -x[1])
