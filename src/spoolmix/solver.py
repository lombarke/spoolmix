"""Full Spectrum recipe search.

Follows Snapmaker Orca's "Color Mixing Match" strategy, reimplemented:

* Pairs: every ratio at 1% steps within [min, max] per component. Orca does a
  5% coarse pass plus a 1% refine; exhaustive 1% finds the same or better.
* Triples: candidates come from a pool of the filaments closest to the target
  (Orca uses 8). Coarse pass at 5%, then a 1% refine (+/-4) around the best
  coarse point for every ordered triple.
* Slot order matters (Orca mixes sequentially in ascending filament ID), so
  every ordering is evaluated and the best order is reported.
* Like Orca, a pair is preferred over a triple unless the triple is more than
  0.5 ΔE better.
* Auto mode caps each component at 70%; that is the default here too.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Iterable, Sequence

import numpy as np

from spoolmix.color import RGB, delta_e2000, srgb_to_lab
from spoolmix.materials import all_compatible, compatible
from spoolmix.model import MixModel, OrcaModel, lerp_batch

ORCA_AUTO_MAX_PERCENT = 70

# Banding (visible stripes) model. A layer shows its own color to the extent the filament
# is opaque: opacity = exp(-TD / OPACITY_TD), so TD 0.1 -> 0.97, TD 1.6 -> 0.59,
# TD 5.5 -> 0.16, TD 9.5 -> 0.04. Unknown TD is treated as fairly opaque (2.0).
OPACITY_TD = 3.0
UNKNOWN_TD = 2.0
STRIPE_MODERATE = 6.0
STRIPE_HIGH = 15.0
# Lopsided mixes stripe more: the minority filament prints as isolated layers between runs
# of the majority (80/20 -> one in five layers), which read as lines, more so when the
# majority filament is the more opaque one and doesn't blur them. Measured on side walls
# (cyan/yellow PLA, 0.1 mm layers): 80/20 striped far more than 65/35-35/65, and more than
# 20/80. Extra risk starts below LOPSIDED_SHARE and scales with LOPSIDED_GAIN (pairs only).
LOPSIDED_SHARE = 0.35
LOPSIDED_GAIN = 2.5
# Banding counts as visible error once a mix starts to band: each unit of banding score
# above STRIPE_MODERATE adds STRIPE_PENALTY ΔE. A clearly striping mix (score ~18) is about
# +4 ΔE, enough that a real spool of that color beats it; blending mixes add nothing.
STRIPE_PENALTY = 0.3
# Mixing itself has a cost (alternating layers, more filament changes, more that can go
# wrong), so a plain filament wins unless the mix is clearly closer.
MIX_PENALTY = 1.0
# What to favor when exact color and smooth blending conflict: (mix penalty, stripe penalty).
PRIORITIES = {
    "color": (0.25, 0.05),  # closest color; accept mixes that may stripe
    "balanced": (MIX_PENALTY, STRIPE_PENALTY),
    "smooth": (2.0, 0.6),  # plain filaments and well-blending mixes, even if colors are further off
}
DEFAULT_PRIORITY = "balanced"
SECOND_SPOOL_PENALTY = 1.5  # ΔE a 2-spool purchase must beat a 1-spool one by to rank first
TRIPLE_POOL_SIZE = 8
TRIPLE_PREFERENCE = 0.5  # ΔE a triple must beat a pair by to be preferred
COARSE_STEP = 5
REFINE_RADIUS = 4
TRIPLE_COARSE_STEP = 10
TRIPLE_REFINE_TOP = 60  # ordered triples per target that get the fine (1%) search


@dataclass(frozen=True)
class Component:
    key: str  # unique, e.g. "spool:4" or "shop:snapmaker-fs-yellow"
    label: str
    rgb: RGB
    td: float | None
    owned: bool = True
    ref: object = field(default=None, compare=False, hash=False, repr=False)
    material: str | None = None  # e.g. "PLA+"; None = unknown (mixes with anything)


def opacity(td: float | None) -> float:
    return float(np.exp(-(td if td and td > 0 else UNKNOWN_TD) / OPACITY_TD))


def lopsided_factor(weights: np.ndarray, op: np.ndarray) -> float:
    """>= 1: how much more a 2-filament mix stripes because its minority filament is sparse.
    1 for balanced mixes; grows as the smaller share drops below LOPSIDED_SHARE, weighted by
    how opaque the majority filament is relative to the minority one. Only measured for
    pairs, so 3+ filament mixes (which the slicer prints as rotating pairs) are left as is."""
    if len(weights) != 2:
        return 1.0
    share = weights / weights.sum()
    lo, hi = int(np.argmin(share)), int(np.argmax(share))
    sparse = max(0.0, (LOPSIDED_SHARE - share[lo]) / LOPSIDED_SHARE)
    if sparse == 0.0 or lo == hi:
        return 1.0
    majority_opaque = 2.0 * op[hi] / (op[hi] + op[lo])  # 1 when equally opaque
    return 1.0 + LOPSIDED_GAIN * sparse * majority_opaque


@dataclass(frozen=True)
class Recipe:
    parts: tuple[Component, ...]  # in slot order (lowest filament number first)
    weights: tuple[int, ...]  # integer percent, sums to 100
    rgb: RGB
    delta_e: float
    penalties: tuple[float, float] = field(default=(MIX_PENALTY, STRIPE_PENALTY), compare=False)

    @property
    def size(self) -> int:
        return len(self.parts)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(p.key for p in self.parts)

    @cached_property
    def stripe_detail(self) -> tuple[float, tuple[float, ...]]:
        """(banding score, per-component visible contrast). Score = weighted RMS of
        ΔE(component, mixed result) x component opacity; 0 for a single filament."""
        if self.size < 2:
            return 0.0, (0.0,)
        mix = srgb_to_lab(self.rgb)
        des = np.atleast_1d(delta_e2000(srgb_to_lab([p.rgb for p in self.parts]), mix))
        op = np.array([opacity(p.td) for p in self.parts])
        contrast = des * op
        w = np.asarray(self.weights, dtype=np.float64)
        base = float(np.sqrt(np.dot(w, contrast**2) / w.sum()))
        return base * lopsided_factor(w, op), tuple(float(c) for c in contrast)

    @property
    def stripe(self) -> float:
        return self.stripe_detail[0]

    @property
    def stripe_level(self) -> str:
        s = self.stripe
        return "high" if s >= STRIPE_HIGH else "moderate" if s >= STRIPE_MODERATE else "low"

    @cached_property
    def score(self) -> float:
        """Visible error: color error plus a penalty for banding beyond 'blends well'.
        This is what ranking, palette selection and purchase decisions minimize."""
        if self.size < 2:
            return self.delta_e
        mix_penalty, stripe_penalty = self.penalties
        return self.delta_e + mix_penalty + stripe_penalty * max(0.0, self.stripe - STRIPE_MODERATE)

    @cached_property
    def rank_score(self) -> float:
        return self.score + (TRIPLE_PREFERENCE if self.size >= 3 else 0.0)

    @property
    def gradient_weights(self) -> str:
        return "/".join(str(w) for w in self.weights)

    @property
    def purchases(self) -> tuple[Component, ...]:
        return tuple(p for p in self.parts if not p.owned)


def _pct_bounds(min_pct: int, max_pct: int, k: int) -> tuple[int, int]:
    lo = max(1, min(min_pct, 100 // k))
    hi = min(max_pct, 100 - lo * (k - 1))
    if hi < lo or hi * k < 100:
        raise ValueError(f"no {k}-filament ratios satisfy min {min_pct}% / max {max_pct}%")
    return lo, hi


class Solver:
    def __init__(
        self,
        model: MixModel | None = None,
        min_pct: int = 0,
        max_pct: int = ORCA_AUTO_MAX_PERCENT,
        pool_size: int = TRIPLE_POOL_SIZE,
        allow_triples: bool = True,
        priority: str = DEFAULT_PRIORITY,
    ):
        if not 50 <= max_pct <= 100:
            raise ValueError("max_pct must be between 50 and 100")
        self.priority = priority if priority in PRIORITIES else DEFAULT_PRIORITY
        self.penalties = PRIORITIES[self.priority]
        self.model = model or OrcaModel()
        self.min_pct = min_pct
        self.max_pct = max_pct
        self.pool_size = pool_size
        self.allow_triples = allow_triples

    # --- low-level batched evaluation -------------------------------------

    def _factors(self, comps: Sequence[Component]) -> np.ndarray:
        per_component = getattr(self.model, "component_factors", None)
        if per_component is not None:
            return per_component(comps)
        return self.model.weight_factors([c.td for c in comps])

    def _pair_eval(self, target_lab, comps: Sequence[Component], combos: list[tuple[int, int]]):
        lo, hi = _pct_bounds(self.min_pct, self.max_pct, 2)
        pcts = np.arange(max(lo, 100 - hi), min(100 - lo, hi) + 1)  # share of the second component
        cols = np.array([c.rgb for c in comps], dtype=np.float64)
        fac = self._factors(comps)
        a = np.array([i for i, _ in combos])
        b = np.array([j for _, j in combos])
        wa = np.repeat((100 - pcts)[None, :], len(combos), 0) * fac[a][:, None]
        wb = np.repeat(pcts[None, :], len(combos), 0) * fac[b][:, None]
        t = (wb / (wa + wb)).ravel()
        rgb = lerp_batch(np.repeat(cols[a], len(pcts), 0), np.repeat(cols[b], len(pcts), 0), t)
        de = delta_e2000(srgb_to_lab(rgb), target_lab).reshape(len(combos), len(pcts))
        best = de.argmin(axis=1)
        out = {}
        for ci, (i, j) in enumerate(combos):
            k = best[ci]
            p = int(pcts[k])
            out[(i, j)] = Recipe((comps[i], comps[j]), (100 - p, p), tuple(int(x) for x in rgb[ci * len(pcts) + k]),
                                 float(de[ci, k]), self.penalties)
        return out

    def _triple_rgb(self, cols, fac, combos: np.ndarray, w: np.ndarray) -> np.ndarray:
        """combos: (N, 3) component indices in slot order; w: (N, 3) percents. Sequential lerp."""
        ew = w * fac[combos]
        t1 = ew[:, 1] / (ew[:, 0] + ew[:, 1])
        r1 = lerp_batch(cols[combos[:, 0]], cols[combos[:, 1]], t1)
        t2 = ew[:, 2] / ew.sum(axis=1)
        return lerp_batch(r1, cols[combos[:, 2]], t2)

    def _triple_eval(self, target_lab, comps: Sequence[Component], combos: list[tuple[int, int, int]]):
        """Best ratio per ordered triple. Two-stage: a coarse grid for every combo, then
        progressively finer refinement only for the most promising combos (the others keep
        their coarse optimum, which is only used to rank them below the refined ones)."""
        if not combos:
            return {}
        lo, hi = _pct_bounds(self.min_pct, self.max_pct, 3)
        cols = np.array([c.rgb for c in comps], dtype=np.float64)
        fac = self._factors(comps)
        cidx = np.array(combos)
        n = len(combos)

        def grid(step: int) -> np.ndarray:
            start = ((lo + step - 1) // step) * step
            g = [(x, y, 100 - x - y) for x in range(start, hi + 1, step)
                 for y in range(start, hi + 1, step) if lo <= 100 - x - y <= hi]
            if not g:
                g = [(x, y, 100 - x - y) for x in range(lo, hi + 1) for y in range(lo, hi + 1) if lo <= 100 - x - y <= hi]
            return np.array(g, dtype=np.float64)

        # Stage 1: 10% grid for every combo.
        g = grid(TRIPLE_COARSE_STEP)
        m = len(g)
        rgb = self._triple_rgb(cols, fac, np.repeat(cidx, m, 0), np.tile(g, (n, 1)))
        de = delta_e2000(srgb_to_lab(rgb), target_lab).reshape(n, m)
        best_k = de.argmin(axis=1)
        best_w = g[best_k].copy()
        best_de = de[np.arange(n), best_k].copy()
        best_rgb = rgb.reshape(n, m, 3)[np.arange(n), best_k].copy()

        # Stage 2: refine the most promising combos around their optimum (5% then 1% steps).
        cand = np.argsort(best_de)[: max(TRIPLE_REFINE_TOP, 1)]
        for radius, step in ((5, 5), (REFINE_RADIUS, 1)):
            offs = [(dx, dy) for dx in range(-radius, radius + 1, step) for dy in range(-radius, radius + 1, step)]
            rows_c, rows_w = [], []
            for ci in cand:
                x0, y0, _ = best_w[ci]
                for dx, dy in offs:
                    x, y = x0 + dx, y0 + dy
                    z = 100 - x - y
                    if lo <= x <= hi and lo <= y <= hi and lo <= z <= hi:
                        rows_c.append(ci)
                        rows_w.append((x, y, z))
            if not rows_c:
                continue
            rc = np.array(rows_c)
            rw = np.array(rows_w, dtype=np.float64)
            rrgb = self._triple_rgb(cols, fac, cidx[rc], rw)
            rde = delta_e2000(srgb_to_lab(rrgb), target_lab)
            for k in np.argsort(rde):
                ci = rc[k]
                if rde[k] < best_de[ci] - 1e-12:
                    best_de[ci], best_w[ci], best_rgb[ci] = rde[k], rw[k], rrgb[k]

        out = {}
        for ci, (i, j, l) in enumerate(combos):
            out[combos[ci]] = Recipe((comps[i], comps[j], comps[l]), tuple(int(v) for v in best_w[ci]),
                                     tuple(int(v) for v in best_rgb[ci]), float(best_de[ci]), self.penalties)
        return out

    # --- public API ---------------------------------------------------------

    def singles(self, target: RGB, comps: Sequence[Component]) -> list[Recipe]:
        tl = srgb_to_lab(target)
        des = delta_e2000(srgb_to_lab([c.rgb for c in comps]), tl)
        rs = [Recipe((c,), (100,), c.rgb, float(d), self.penalties) for c, d in zip(comps, np.atleast_1d(des))]
        return sorted(rs, key=lambda r: r.delta_e)

    def all_recipes(
        self,
        target: RGB,
        comps: Sequence[Component],
        must_include: Iterable[str] | None = None,
        pool: Sequence[int] | None = None,
        require_all: bool = False,
    ) -> dict[tuple[int, ...], Recipe]:
        """Best recipe for every ordered pair/triple (triples restricted to `pool`).

        must_include: component keys; only combos containing at least one of them
        (or all of them, with require_all) are evaluated.
        """
        tl = srgb_to_lab(target)
        n = len(comps)
        req = {i for i, c in enumerate(comps) if must_include and c.key in set(must_include)}
        ok = [[compatible(a.material, b.material) for b in comps] for a in comps]

        def wanted(combo):
            if not all(ok[x][y] for i, x in enumerate(combo) for y in combo[i + 1:]):
                return False  # never mix incompatible materials (e.g. PLA with PETG)
            if not req:
                return True
            return req <= set(combo) if require_all else any(i in req for i in combo)

        pairs = [p for p in itertools.permutations(range(n), 2) if wanted(p)]
        out: dict[tuple[int, ...], Recipe] = self._pair_eval(tl, comps, pairs) if pairs else {}
        if self.allow_triples and n >= 3:
            if pool is None:
                pool = self.default_pool(target, comps, req)
            triples = [t for t in itertools.permutations(pool, 3) if wanted(t)]
            out.update(self._triple_eval(tl, comps, triples))
        return out

    def default_pool_ranked(self, target: RGB, comps: Sequence[Component]) -> list[int]:
        """All component indices, closest to the target first."""
        des = np.atleast_1d(delta_e2000(srgb_to_lab([c.rgb for c in comps]), srgb_to_lab(target)))
        return [int(i) for i in np.argsort(des, kind="stable")]

    def default_pool(self, target: RGB, comps: Sequence[Component], required: set[int] = frozenset()) -> list[int]:
        tl = srgb_to_lab(target)
        des = np.atleast_1d(delta_e2000(srgb_to_lab([c.rgb for c in comps]), tl))
        ranked = [int(i) for i in np.argsort(des, kind="stable")]
        pool = list(dict.fromkeys([*sorted(required), *ranked]))[: max(self.pool_size, len(required))]
        return sorted(pool)

    def solve(
        self,
        target: RGB,
        comps: Sequence[Component],
        top: int = 5,
        must_include: Iterable[str] | None = None,
        require_all: bool = False,
    ) -> list[Recipe]:
        """Best distinct recipes (one per filament set), Orca-style ranking."""
        if len(comps) < 2:
            return []
        recipes = self.all_recipes(target, comps, must_include, require_all=require_all).values()
        return dedupe_best(recipes, top)


def dedupe_best(recipes: Iterable[Recipe], top: int) -> list[Recipe]:
    best: dict[frozenset[str], Recipe] = {}
    for r in recipes:
        cur = best.get(r.keys)
        if cur is None or r.delta_e < cur.delta_e:
            best[r.keys] = r
    return sorted(best.values(), key=lambda r: (r.rank_score, r.delta_e))[:top]


# --- multi-target palette selection (U1: 4 toolheads) -------------------------


@dataclass
class PaletteResult:
    slots: tuple[Component, ...]  # T1..T4 / filament order in Orca
    recipes: list[Recipe]  # one per target, same order as targets
    worst: float
    total: float
    weighted: float = 0.0  # weight-averaged ΔE (equals mean ΔE without weights)
    score: float = 0.0  # weight-averaged visible error (ΔE + banding penalty): what selection minimizes
    worst_score: float = 0.0  # worst color's visible error


@dataclass
class RecipeTable:
    """Best recipe per target for every single filament and ordered pair/triple of `comps`."""

    comps: list[Component]
    per_target: list[dict[tuple[int, ...], Recipe]]
    singles: list[dict[int, Recipe]]

    @classmethod
    def build(cls, solver: Solver, targets: Sequence[RGB], comps: Sequence[Component]) -> RecipeTable:
        comps = list(comps)
        n = len(comps)
        per_target = [solver.all_recipes(t, comps, pool=list(range(n))) for t in targets]
        singles = []
        for t in targets:
            rs = solver.singles(t, comps)
            idx = {c.key: i for i, c in enumerate(comps)}
            singles.append({idx[r.parts[0].key]: r for r in rs})
        return cls(comps, per_target, singles)


def _pick(table: RecipeTable, ti: int, order: tuple[int, ...], prefer_single_de: float, mixes: bool = True) -> Recipe:
    singles = [table.singles[ti][c] for c in order]
    best_single = min(singles, key=lambda r: r.delta_e)
    if best_single.delta_e <= prefer_single_de or not mixes:
        return best_single
    pt = table.per_target[ti]
    cands = singles + [pt[p] for p in itertools.combinations(order, 2) if p in pt]
    cands += [pt[p] for p in itertools.combinations(order, 3) if p in pt]
    return min(cands, key=lambda r: (r.rank_score, r.delta_e))


EXHAUSTIVE_ORDER_MAX = 4  # try every filament order up to this many slots; beyond, heuristic orders
# A color that ends up far off is worse than its area share says (eyes, logos, text are
# small but seen): beyond FAR_OFF_DE of visible error, the worst color adds FAR_OFF_WEIGHT
# per ΔE to the palette's area-weighted score when choosing filaments.
FAR_OFF_DE = 12.0
FAR_OFF_WEIGHT = 0.15


def palette_search(
    table: RecipeTable,
    slots: int = 4,
    weights: Sequence[float] | None = None,
    objective: str = "max",
    allowed: Sequence[int] | None = None,
    required: Sequence[int] = (),
    prefer_single_de: float = 0.0,
    mixes: bool = True,
) -> PaletteResult:
    """Best subset (<= slots) of table.comps and one global order, scored by lookup.
    mixes=False: every color must be printed with one plain filament (no color mixing).

    objective: 'max' (worst ΔE first) or 'weighted' (weight-averaged ΔE first).
    required: comp indices that must be in the palette (e.g. spools you'd buy).
    With more than EXHAUSTIVE_ORDER_MAX slots, each subset is tried in a few sensible
    orders (by lightness, both ways, and as given) and the best subsets are then refined
    by swapping filaments pairwise.
    """
    n_t = len(table.per_target)
    w = np.ones(n_t) if weights is None else np.asarray(weights, dtype=np.float64)
    w = w / w.sum()
    pool = sorted(set(allowed if allowed is not None else range(len(table.comps))) | set(required))
    req = set(required)
    light = {c: float(srgb_to_lab(table.comps[c].rgb)[0]) for c in pool}

    def evaluate(order: tuple[int, ...]) -> tuple[tuple, PaletteResult]:
        chosen = [_pick(table, ti, order, prefer_single_de, mixes) for ti in range(n_t)]
        des = np.array([r.delta_e for r in chosen])
        scores = np.array([r.score for r in chosen])
        res = PaletteResult(tuple(table.comps[c] for c in order), chosen, float(des.max()), float(des.sum()),
                            float(np.dot(w, des)), float(np.dot(w, scores)), float(scores.max()))
        if objective == "max":
            key = (res.worst_score, res.score)
        else:
            key = (res.score + FAR_OFF_WEIGHT * max(0.0, res.worst_score - FAR_OFF_DE), res.worst_score)
        return key, res

    def orders(subset: tuple[int, ...]):
        if not mixes:  # order only matters for mixes
            yield subset
            return
        if len(subset) <= EXHAUSTIVE_ORDER_MAX:
            yield from itertools.permutations(subset)
            return
        by_light = tuple(sorted(subset, key=lambda c: light[c]))
        yield from dict.fromkeys([by_light, by_light[::-1], subset])

    def refine(order: tuple[int, ...], key: tuple, res: PaletteResult):
        improved = True
        while improved:
            improved = False
            for i, j in itertools.combinations(range(len(order)), 2):
                cand = list(order)
                cand[i], cand[j] = cand[j], cand[i]
                k2, r2 = evaluate(tuple(cand))
                if k2 < key:
                    order, key, res, improved = tuple(cand), k2, r2, True
        return order, key, res

    # Prefer using all slots, but fall back to fewer filaments when material compatibility
    # rules out every full set (e.g. stock of 3 PLA + 2 PETG for a 4-slot print).
    for size in range(min(slots, len(pool)), 0, -1):
        found: list[tuple[tuple, tuple[int, ...], PaletteResult]] = []
        for subset in itertools.combinations(pool, size):
            if not req <= set(subset):
                continue
            if not all_compatible(table.comps[c].material for c in subset):
                continue  # everything loaded for one print must be mutually compatible
            for order in orders(subset):
                key, res = evaluate(order)
                found.append((key, order, res))
        if not found:
            continue
        found.sort(key=lambda x: x[0])
        if size > EXHAUSTIVE_ORDER_MAX:
            refined = []
            for k, o, r in found[:5]:  # refine the most promising subsets by pairwise swaps
                o2, k2, r2 = refine(o, k, r)
                refined.append((k2, o2, r2))
            found = sorted(refined, key=lambda x: x[0])
        return _drop_unused(found[0][2], {table.comps[c].key for c in req})
    raise ValueError("no compatible set of filaments for these targets")


def _drop_unused(res: PaletteResult, keep: set[str]) -> PaletteResult:
    """Don't ask for a filament no color uses (e.g. a second black when only three of four
    slots are needed). Required ones (spools to buy) stay. The order of the rest is kept, so
    mixes are unchanged."""
    used = {p.key for r in res.recipes for p in r.parts} | keep
    slots = tuple(c for c in res.slots if c.key in used)
    return res if len(slots) == len(res.slots) else replace(res, slots=slots)


def choose_palette(
    solver: Solver,
    targets: Sequence[RGB],
    comps: Sequence[Component],
    slots: int = 4,
    objective: str = "max",
    max_candidates: int = 10,
    weights: Sequence[float] | None = None,
    prefer_single_de: float = 0.0,
) -> PaletteResult:
    """Pick <= `slots` filaments and one global order that best cover all targets."""
    if len(comps) > max_candidates:
        keep: set[int] = set()
        for t in targets:
            keep.update(solver.default_pool(t, comps)[: max(3, max_candidates // len(targets))])
        comps = [comps[i] for i in sorted(keep)][:max_candidates]
    table = RecipeTable.build(solver, targets, comps)
    return palette_search(table, slots, weights, objective, prefer_single_de=prefer_single_de)


# --- shopping -------------------------------------------------------------------


@dataclass
class ShopOption:
    buy: tuple[Component, ...]
    recipe: Recipe
    improvement: float  # ΔE gain over best with stock


def shop(
    solver: Solver,
    target: RGB,
    stock: Sequence[Component],
    catalog: Sequence[Component],
    max_buy: int = 2,
    top: int = 5,
    two_spool_candidates: int = 6,
    min_gain: float = 0.5,
    min_gain_for_second: float = 1.0,
) -> tuple[Recipe | None, list[ShopOption]]:
    """Best recipe with stock, and the best 1- or 2-spool purchases that improve it."""
    base = solver.solve(target, stock, top=1)
    base_best = base[0] if base else None
    base_de = base_best.score if base_best else min((r.delta_e for r in solver.singles(target, stock)), default=999.0)

    stock_keys = {c.key for c in stock}
    catalog = [c for c in catalog if c.key not in stock_keys]
    single_opts: list[ShopOption] = []
    for item in catalog:
        comps = [*stock, item]
        found = solver.solve(target, comps, top=1, must_include=[item.key])
        cands = [*found, *[r for r in solver.singles(target, [item])]]
        r = min(cands, key=lambda r: (r.rank_score if r.size > 1 else r.delta_e))
        if r.score <= base_de - min_gain:
            single_opts.append(ShopOption((item,), r, base_de - r.score))
    single_opts.sort(key=lambda o: o.recipe.delta_e)

    options = list(single_opts)
    if max_buy >= 2 and len(catalog) >= 2:
        best_one = {o.buy[0].key: o.recipe.score for o in single_opts}
        # Candidates: best single purchases plus catalog colors nearest the target.
        near = [r.parts[0] for r in solver.singles(target, catalog)[:two_spool_candidates]]
        cand = list(dict.fromkeys([*(o.buy[0] for o in single_opts[:two_spool_candidates]), *near]))
        for a, b in itertools.combinations(cand, 2):
            comps = [*stock, a, b]
            found = solver.solve(target, comps, top=1, must_include=[a.key, b.key], require_all=True)
            if not found:
                continue
            r = found[0]
            prior = min(best_one.get(a.key, base_de), best_one.get(b.key, base_de))
            if r.score < prior - min_gain_for_second:
                options.append(ShopOption((a, b), r, base_de - r.score))
    # Buying one spool beats buying two unless the second clearly helps.
    options.sort(key=lambda o: (o.recipe.score + SECOND_SPOOL_PENALTY * (len(o.buy) - 1), len(o.buy)))
    from spoolmix.design import distinct_purchases

    return base_best, distinct_purchases(options, key=lambda o: o.buy)[:top]
