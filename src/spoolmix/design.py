"""Plan how to print a multicolor design with your filaments (and what to buy)."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from spoolmix.calibrate import mixed_row_for
from spoolmix.color import delta_e2000, hue_name, srgb_to_lab, to_hex
from spoolmix.materials import category
from spoolmix.project3mf import Assignment, DesignFilament
from spoolmix.solver import SECOND_SPOOL_PENALTY, Component, PaletteResult, RecipeTable, Solver, palette_search

PREFER_SINGLE_DE = 3.0  # use a filament as-is when it's this close: no striping, simpler print
MIN_WEIGHT = 0.02  # tiny details (eyes) still count
IMPORTANT_FACTOR = 5.0
MAX_STOCK_CANDIDATES = 12
SHOP_CANDIDATES = 6
MAX_OPTIONS = 3
MIN_AVG_GAIN = 0.25  # a purchase must lower the area-weighted ΔE (incl. banding) this much...
MIN_WORST_GAIN = 3.0  # ...or fix the worst color by this much without hurting the average
SMALL_DETAIL = 0.05  # colors under this share of the surface (eyes, text) are details...
DETAIL_SLACK = 2.0  # ...that a purchase may not make more than this much worse


@dataclass
class DesignPlan:
    targets: list[DesignFilament]
    weights: list[float]
    result: PaletteResult
    buy: tuple[Component, ...] = ()
    simplest: bool = False  # plain filaments only, no color mixing

    def assignment(self, all_design: Sequence[DesignFilament], slots: int = 4) -> Assignment:
        slot_of = {c.key: i + 1 for i, c in enumerate(self.result.slots)}
        physical = [(to_hex(c.rgb), c.label) for c in self.result.slots]
        while len(physical) < slots:
            physical.append(("#FFFFFF", "unused"))
        targets: dict[int, int] = {}
        rows: list[str] = []
        row_ids: dict[tuple, int] = {}
        for f, r in zip(self.targets, self.result.recipes):
            slots = [slot_of[p.key] for p in r.parts]
            if len(slots) == 1:
                targets[f.id] = slots[0]
                continue
            key = (tuple(slots), tuple(r.weights))
            if key not in row_ids:
                rows.append(mixed_row_for(slots, r.weights, len(rows) + 1))
                row_ids[key] = len(physical) + len(rows)
            targets[f.id] = row_ids[key]
        for f in all_design:
            targets.setdefault(f.id, 1)  # unused design filaments
        return Assignment(physical, targets, rows)


def design_weights(targets: Sequence[DesignFilament], important: set[int]) -> list[float]:
    total = sum(f.area for f in targets) or 1.0
    return [max(f.area / total, MIN_WEIGHT) * (IMPORTANT_FACTOR if f.id in important else 1.0) for f in targets]


def _stock_pool(solver: Solver, targets, stock: Sequence[Component], slots: int = 4) -> list[Component]:
    """Most useful stock filaments to search, up to a cap:
    1. each design color's two closest filaments (round-robin, so every color is covered);
    2. good mixing partners: your most saturated filament per hue family, plus your darkest
       and lightest (e.g. magenta + yellow + black for a brown, though magenta is close to nothing);
    3. then more of each color's next-closest filaments.
    Never truncated by Spoolman ID order."""
    cap = max(MAX_STOCK_CANDIDATES, slots + 6)
    if len(stock) <= cap:
        return list(stock)
    ranked = [solver.default_pool_ranked(t.rgb, stock) for t in targets]
    keep: list[int] = []

    def add(i: int) -> bool:
        if i not in keep:
            keep.append(i)
        return len(keep) >= cap

    def round_robin(depth_from: int, depth_to: int) -> bool:
        for rank in range(depth_from, depth_to):
            for r in ranked:
                if rank < len(r) and add(r[rank]):
                    return True
        return False

    if round_robin(0, 2):
        return [stock[i] for i in sorted(keep)]
    lab = srgb_to_lab([c.rgb for c in stock])
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    partners: dict[str, int] = {}
    for i, c in enumerate(stock):
        fam = hue_name(c.rgb)
        if fam in ("black", "white", "gray"):
            continue
        if fam not in partners or chroma[i] > chroma[partners[fam]]:
            partners[fam] = i
    extremes = [int(np.argmin(lab[:, 0])), int(np.argmax(lab[:, 0]))]
    for i in [*extremes, *sorted(partners.values(), key=lambda i: -chroma[i])]:
        if add(i):
            return [stock[i] for i in sorted(keep)]
    round_robin(2, len(stock))
    return [stock[i] for i in sorted(keep)]


SIMPLE_MAX_WORST = 10.0  # a no-mix plan is offered only if no color is off by more than this...
SIMPLE_SLACK = 1.0  # ...and it is within this much (visible error) of the best plan


def _simplest(table, comps, stock_idx, cand_idx, targets, weights, slots, max_buy, base, options):
    """Best plan that prints every color with one plain filament (no color mixing), buying at
    most `max_buy` spools: e.g. 'buy a brown and call it a day'. Offered when it's nearly as
    good as the best mixing plan, because no mixing means no striping and a simpler print."""
    best: DesignPlan | None = None
    tries = [()] + [(c,) for c in cand_idx] + (list(itertools.combinations(cand_idx, 2)) if max_buy >= 2 else [])
    for req in tries:
        try:
            r = palette_search(table, slots, weights, "weighted", allowed=stock_idx, required=list(req), mixes=False)
        except ValueError:
            continue
        cand = DesignPlan(targets, weights, r, tuple(comps[i] for i in req), simplest=True)
        key = (r.score + SECOND_SPOOL_PENALTY * len(req), r.worst)
        if best is None or key < best_key:
            best, best_key = cand, key
    if best is None or best.result.worst > SIMPLE_MAX_WORST:
        return None
    best_mix = min([base.score] + [o.result.score for o in options])
    if best.result.score > best_mix + SIMPLE_SLACK + SECOND_SPOOL_PENALTY * len(best.buy):
        return None
    return best


def purchase_family(buy: Sequence[Component]) -> tuple[str, ...]:
    """Color family of what you'd buy, e.g. ('yellow',) or ('blue', 'white')."""
    return tuple(sorted(hue_name(c.rgb) for c in buy))


def distinct_purchases(options: Sequence, key) -> list:
    """Keep only the best option per color family: five slightly different yellows are one
    suggestion, not five. `options` must already be sorted best-first."""
    seen: set[tuple[str, ...]] = set()
    out = []
    for o in options:
        fam = purchase_family(key(o))
        if fam not in seen:
            seen.add(fam)
            out.append(o)
    return out


def plan_fixed(
    solver: Solver,
    design: Sequence[DesignFilament],
    comps: Sequence[Component],
    important: set[int] = frozenset(),
    buy: Sequence[Component] = (),
) -> DesignPlan:
    """A plan with exactly these filaments (what you picked when swapping): spoolmix only
    chooses their slot order and how to print each color with them. Raises ValueError if
    they can't all be loaded together (materials that don't bond)."""
    targets = [f for f in design if f.area > 0] or list(design)
    weights = design_weights(targets, important)
    table = RecipeTable.build(solver, [t.rgb for t in targets], list(comps))
    res = palette_search(table, len(comps), weights, "weighted", required=list(range(len(comps))),
                         prefer_single_de=PREFER_SINGLE_DE)
    return DesignPlan(targets, weights, res, tuple(buy))


def protected_colors(targets: Sequence[DesignFilament], important: set[int]) -> list[bool]:
    """Small details (eyes, text) and colors you ticked as important."""
    total = sum(f.area for f in targets) or 1.0
    return [f.id in important or f.area / total < SMALL_DETAIL for f in targets]


OWNED_LOOKALIKE_DE = 3.0  # don't suggest buying a spool this close to one you have


def _not_owned_lookalikes(catalog: Sequence[Component], stock: Sequence[Component]) -> list[Component]:
    """Drop purchases that nearly duplicate a filament you already own (another white when
    yours is ΔE 2 away): buying one can't meaningfully change a plan."""
    if not catalog or not stock:
        return list(catalog)
    owned = srgb_to_lab(np.array([c.rgb for c in stock], dtype=np.float64))
    keep = []
    for item in catalog:
        de = np.atleast_1d(delta_e2000(owned, srgb_to_lab(item.rgb)))
        if float(de.min()) >= OWNED_LOOKALIKE_DE:
            keep.append(item)
    return keep


SHOP_PER_FAMILY = 2  # shortlist at most this many lookalikes (e.g. greens) on overall gain...
SHOP_PER_COLOR_GAIN = 3.0  # ...plus the best spool for each design color it would fix by this much


def _shortlist(scored: list, n_colors: int) -> list[Component]:
    """Spools worth evaluating properly: the best on overall gain (at most SHOP_PER_FAMILY
    per color family, so six near-identical greens can't crowd out everything else), plus the
    best one for each design color a purchase could clearly fix (the red, when a big green
    area dominates the overall gain)."""
    out: list[Component] = []
    per_family: dict[str, int] = {}
    for gain, item, _ in sorted(scored, key=lambda s: -s[0]):
        if gain <= 0.05 or len(out) >= SHOP_CANDIDATES:
            break
        fam = hue_name(item.rgb)
        if per_family.get(fam, 0) >= SHOP_PER_FAMILY:
            continue
        per_family[fam] = per_family.get(fam, 0) + 1
        out.append(item)
    for ti in range(n_colors):
        gain, item, improve = max(scored, key=lambda s: (s[2][ti], s[0]), default=(0, None, [0] * n_colors))
        if item is not None and improve[ti] >= SHOP_PER_COLOR_GAIN and all(item.key != o.key for o in out):
            out.append(item)
    return out


def _worth_buying(r: PaletteResult, base: PaletteResult, protected: Sequence[bool] = ()) -> bool:
    """Judge on visible error (color error + banding): a clearly lower average, or a much
    better worst color without hurting the average. Either way, no small detail or important
    color may get noticeably worse; a larger area may, when the rest gains more than it loses
    (e.g. buy the brown that covers half the model, even if a small green patch now mixes)."""
    for keep, new, old in zip(protected, r.recipes, base.recipes):
        if keep and new.score > old.score + DETAIL_SLACK:
            return False
    if r.score <= base.score - MIN_AVG_GAIN:
        return True
    return r.worst_score <= base.worst_score - MIN_WORST_GAIN and r.score <= base.score + 0.1


def design_categories(design: Sequence[DesignFilament]) -> set[str]:
    """Material categories the designer used (only filaments that are actually used)."""
    used = [f for f in design if f.area > 0] or list(design)
    return {c for c in (category(f.type) for f in used) if c}


def filter_materials(comps: Sequence[Component], allowed: set[str]) -> tuple[list[Component], list[Component]]:
    """(kept, excluded): keep components whose material category is in `allowed` (or unknown)."""
    if not allowed:
        return list(comps), []
    kept, excluded = [], []
    for c in comps:
        cat = category(c.material)
        (kept if cat is None or cat in allowed else excluded).append(c)
    return kept, excluded


def plan(
    solver: Solver,
    design: Sequence[DesignFilament],
    stock: Sequence[Component],
    catalog: Sequence[Component] = (),
    important: set[int] = frozenset(),
    max_buy: int = 0,
    slots: int = 4,
    progress=lambda _m: None,
    materials: set[str] | None = None,
) -> tuple[DesignPlan, list[DesignPlan]]:
    """Best plan with stock, plus purchase options (sorted, best first).

    Only filaments of the design's material categories are used (a PLA design is printed in
    PLA-family filament), and mixes/palettes are always material-compatible.
    """
    allowed = materials or design_categories(design)  # print in another material if asked
    stock, _ = filter_materials(stock, allowed)
    catalog, _ = filter_materials(catalog, allowed)
    catalog = _not_owned_lookalikes(catalog, stock)
    if len(stock) < 1:
        raise ValueError(f"No in-stock filament of this design's material ({', '.join(sorted(allowed))}).")
    targets = [f for f in design if f.area > 0] or list(design)
    weights = design_weights(targets, important)
    protected = protected_colors(targets, important)
    pool = _stock_pool(solver, targets, stock, slots)
    rgbs = [t.rgb for t in targets]

    progress("searching your filaments")
    base_table = RecipeTable.build(solver, rgbs, pool)
    base = palette_search(base_table, slots, weights, "weighted", prefer_single_de=PREFER_SINGLE_DE)
    base_plan = DesignPlan(targets, weights, base)
    if not catalog or max_buy <= 0:
        return base_plan, []

    # Shortlist purchases with a cheap estimate: best pair (catalog item + one of the
    # current palette's filaments) or the item alone, per design color.
    progress("shortlisting spools to buy")
    w = np.asarray(weights) / sum(weights)
    base_de = np.array([r.score for r in base.recipes])  # visible error incl. banding
    pair_solver = Solver(model=solver.model, min_pct=solver.min_pct, max_pct=solver.max_pct, allow_triples=False,
                         priority=solver.priority)
    scored = []
    for item in catalog:
        comps = [*base.slots, item]
        est = []
        for t in rgbs:
            single = float(delta_e2000(srgb_to_lab(item.rgb), srgb_to_lab(t)))
            rs = pair_solver.solve(t, comps, top=1, must_include=[item.key])
            est.append(min([single] + [r.score for r in rs]))
        improve = np.maximum(0.0, base_de - np.array(est))
        gain = float(np.dot(w, improve)) + 0.05 * float(improve.max())  # small bonus for fixing one bad color
        scored.append((gain, item, improve))
    shortlist = _shortlist(scored, len(rgbs))
    if not shortlist:
        return base_plan, []

    progress("evaluating purchases")
    comps = [*pool, *shortlist]
    table = RecipeTable.build(solver, rgbs, comps)
    stock_idx = list(range(len(pool)))
    cand_idx = list(range(len(pool), len(comps)))
    # Filaments the small details / important colors use now: if the best palette with a
    # purchase gives one of them up (e.g. the black for the eyes), try again keeping them.
    index = {c.key: i for i, c in enumerate(comps)}
    keep = sorted({index[p.key] for rec, prot in zip(base.recipes, protected) if prot for p in rec.parts})

    def evaluate(buy: list[int]) -> PaletteResult | None:
        """Best palette with this purchase (one that's worth buying, if keeping the detail
        filaments makes it so), or None if it can't be used at all."""
        tries = [buy]
        with_keep = sorted(set(buy) | set(keep))
        if with_keep != sorted(buy) and len(with_keep) <= slots:
            tries.append(with_keep)
        best = None
        for required in tries:
            try:
                r = palette_search(table, slots, weights, "weighted", allowed=stock_idx, required=required,
                                   prefer_single_de=PREFER_SINGLE_DE)
            except ValueError:  # not material-compatible with anything usable
                continue
            if _worth_buying(r, base, protected):
                return r
            best = best or r
        return best

    options: list[DesignPlan] = []
    single_best: dict[int, float] = {}
    for ci in cand_idx:
        r = evaluate([ci])
        single_best[ci] = r.score if r else float("inf")
        if r is not None and _worth_buying(r, base, protected):
            options.append(DesignPlan(targets, weights, r, (comps[ci],)))
    if max_buy >= 2:
        for a, b in itertools.combinations(cand_idx, 2):
            r = evaluate([a, b])
            if r is None:
                continue
            better_than_singles = r.score <= min(single_best[a], single_best[b]) - MIN_AVG_GAIN
            if _worth_buying(r, base, protected) and better_than_singles:
                options.append(DesignPlan(targets, weights, r, (comps[a], comps[b])))
    options.sort(key=lambda p: (p.result.score + SECOND_SPOOL_PENALTY * (len(p.buy) - 1), p.result.worst_score))
    options = distinct_purchases(options, key=lambda p: p.buy)[:MAX_OPTIONS]
    simple = _simplest(table, comps, stock_idx, cand_idx, targets, weights, slots, max_buy, base, options)
    if simple is not None:
        options = [simple] + [o for o in options if {c.key for c in o.buy} != {c.key for c in simple.buy}]
    return base_plan, options[:MAX_OPTIONS]
