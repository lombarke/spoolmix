"""Shared operations for the web UI (and anything else that isn't the terminal)."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from dataclasses import dataclass, replace
from typing import Sequence

from spoolmix import report
from spoolmix.color import RGB, lightness, to_hex
from spoolmix.config import Config
from spoolmix.model import get_model
from spoolmix.solver import Component, Recipe, Solver, choose_palette, shop
from spoolmix.inventory import open_inventory
from spoolmix.spoolman import Filament
from spoolmix.tddb import TDDatabase, TDMatch

LOOKUP_PREFIX = "lookup:"
ESTIMATE_PREFIX = "estimate:"


@dataclass
class MatchOptions:
    shop: bool = False
    max_buy: int = 2
    model: str = "orca"  # orca | calibrated
    min_pct: int = 0
    max_pct: int = 70
    pairs_only: bool = False
    top: int = 3
    slots: int = 4
    material: str = "any"  # 'any' (compatible mixes only) or a category such as 'PLA'
    priority: str = "balanced"  # color | balanced | smooth (see solver.PRIORITIES)
    template: str | None = None  # printer template id: its calibration is used for "calibrated"


def td_db(cfg: Config) -> TDDatabase:
    return TDDatabase.load(cfg.td_database, cfg.td_user, cfg.data_dir / PUBLISHED_FILE)


# --- automatic TD -------------------------------------------------------------------

PUBLISHED_FILE = "td_published.yaml"
BRANDS_FILE = "td_published.brands.json"  # brands the last fetch covered
REFRESH_DAYS = 7
_td_lock = threading.Lock()


def refresh_published(cfg: Config, brands: Sequence[str], progress=lambda _m: None) -> int:
    """Fetch current published TDs for these brands (filamentcolors.xyz) plus Snapmaker's and
    Prusa's tables into data/td_published.yaml. Returns the number of entries cached."""
    from spoolmix import tdsources
    from spoolmix.tddb import TDDatabase, normalize_brand

    today = dt.date.today().isoformat()
    entries: list[dict] = []
    errors: list[str] = []
    for name, fn in (("Snapmaker", lambda: tdsources.snapmaker_entries(today)),
                     ("Prusa", lambda: tdsources.prusa_entries(today))):
        try:
            progress(f"fetching {name} TD table")
            entries += fn()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    try:
        progress("fetching filamentcolors.xyz brands")
        known = tdsources.fc_manufacturers()
        wanted = sorted({b for b in brands if b and normalize_brand(b) in {normalize_brand(k) for k in known}})
        for b in wanted:
            progress(f"fetching filamentcolors.xyz: {b}")
            fc_name = next(k for k in known if normalize_brand(k) == normalize_brand(b))
            entries += tdsources.filamentcolors_entries([fc_name], today)
    except Exception as exc:
        errors.append(f"filamentcolors.xyz: {exc}")
    if not entries:
        raise RuntimeError("; ".join(errors) or "no published TD values fetched")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    TDDatabase.write_new(cfg.data_dir / PUBLISHED_FILE, entries, updated=today)
    (cfg.data_dir / BRANDS_FILE).write_text(json.dumps(sorted({normalize_brand(b) for b in brands if b})))
    return len(entries)


def checked_brands(cfg: Config) -> set[str]:
    try:
        return set(json.loads((cfg.data_dir / BRANDS_FILE).read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def published_age_days(cfg: Config) -> float | None:
    p = cfg.data_dir / PUBLISHED_FILE
    if not p.is_file():
        return None
    return (dt.datetime.now().timestamp() - p.stat().st_mtime) / 86400


def auto_td(cfg: Config, refresh: bool | None = None, progress=lambda _m: None) -> dict:
    """Give every filament a TD automatically: a published value when one matches with high
    confidence, otherwise an estimate (labeled as such). Never touches a TD you entered.
    refresh: True = re-fetch published sources now; None = only if older than REFRESH_DAYS."""
    if not _td_lock.acquire(blocking=False):
        return {"written": [], "skipped": "already running"}
    try:
        with open_inventory(cfg) as sm:
            for key, definition in sm.missing_fields().items():
                sm.create_field(key, definition)
            filaments = sm.filaments()
            age = published_age_days(cfg)
            fetch_error = None
            from spoolmix.tddb import normalize_brand

            new_brand = bool({normalize_brand(f.vendor) for f in filaments if f.vendor} - checked_brands(cfg))
            if refresh or (refresh is None and (age is None or age > REFRESH_DAYS or new_brand)):
                try:
                    refresh_published(cfg, [f.vendor for f in filaments if f.vendor], progress)
                except Exception as exc:  # offline etc.: keep using what we have
                    fetch_error = str(exc)
            db = td_db(cfg)
            written, missing = [], []
            for f in filaments:
                if is_multicolor(f):
                    continue
                p = propose_td(db, f)
                if p is not None:
                    sm.set_filament_td(f.id, p.td, p.source)
                    written.append({"id": f.id, "label": f.label, "td": p.td, "kind": p.kind})
                if not f.hex or not (f.material or "").strip():
                    missing.append({"id": f.id, "label": f.label,
                                    "reason": "no color in Spoolman" if not f.hex else "no material in Spoolman"})
                elif p is None and f.td is None:
                    missing.append({"id": f.id, "label": f.label, "reason": "nothing similar measured"})
        return {"written": written, "missing": missing, "fetch_error": fetch_error}
    finally:
        _td_lock.release()


MULTICOLOR_WORDS = re.compile(r"gradient|rainbow|multi[- ]?colou?r|dual[- ]?colou?r|tri[- ]?colou?r|"
                              r"two[- ]tone|co-?extru|color[- ]?chang|colour[- ]?chang", re.I)


def is_multicolor(f: Filament) -> bool:
    """Gradient/rainbow/dual-color spools change color along the spool: unusable for mixing."""
    return bool(f.multi_color_hexes) or bool(MULTICOLOR_WORDS.search(f"{f.name} {f.material or ''}"))


def stock_components(filaments: Sequence[Filament]) -> list[Component]:
    return [
        Component(key=f.key, label=f.label, rgb=f.rgb, td=f.td, owned=True, ref=f, material=f.material)
        for f in filaments
        if f.in_stock and f.rgb is not None and (f.material or "").strip() and not is_multicolor(f)
    ]


def td_owner(f: Filament) -> str:
    """'none', 'lookup' or 'estimate' (written by spoolmix, may be replaced), or 'user' (never touched)."""
    if f.td is None:
        return "none"
    src = f.td_source or ""
    if src.startswith(LOOKUP_PREFIX):
        return "lookup"
    if src.startswith(ESTIMATE_PREFIX):
        return "estimate"
    return "user"


def estimate_source(est) -> str:
    where = "this brand's" if est.scope == "brand" else "other brands'"
    return f"{ESTIMATE_PREFIX}from {where} similar measured filaments: {est.basis} | {dt.date.today().isoformat()}"


@dataclass
class TDProposal:
    td: float
    kind: str  # high | medium | estimate
    source: str  # td_source to write
    match: TDMatch | None = None
    estimate: object = None


def propose_td(db: TDDatabase, f: Filament) -> TDProposal | None:
    """What spoolmix would write for this filament, or None. Never proposes over a user TD,
    and never proposes an estimate over a lookup value."""
    from spoolmix.tddb import estimate_td

    owner = td_owner(f)
    if owner == "user":
        return None
    m = db.best(f.vendor, f.material, f.name, f.hex)
    # Published values win only at high confidence; otherwise an estimate from similar
    # filaments is usually better than a medium (different product) match.
    if m and m.level == "high":
        if owner == "lookup" and m.entry.td == f.td:
            return None
        return TDProposal(m.entry.td, m.level, lookup_source(m), match=m)
    if owner == "lookup":
        return None
    est = estimate_td(db, f.vendor, f.material, f.name, f.hex)
    if est is not None:
        if owner == "estimate" and est.td == f.td:
            return None
        return TDProposal(est.td, "estimate", estimate_source(est), estimate=est)
    if m and m.level == "medium" and owner == "none":
        return TDProposal(m.entry.td, m.level, lookup_source(m), match=m)
    return None


def lookup_source(m: TDMatch) -> str:
    e = m.entry
    return f"{LOOKUP_PREFIX}{e.source} {e.label} | {e.source_url or e.source} | conf {m.confidence:.2f} | {dt.date.today().isoformat()}"


def recipe_view(r: Recipe, slot_numbers: Sequence[int] | None = None) -> dict:
    nums = list(slot_numbers) if slot_numbers else list(range(1, r.size + 1))
    parts = [
        {"slot": n, "label": p.label, "hex": to_hex(p.rgb), "td": p.td, "owned": p.owned, "percent": w,
         "url": getattr(p.ref, "url", None), "bundle": getattr(p.ref, "bundle", None), "price": getattr(p.ref, "price", None)}
        for n, p, w in zip(nums, r.parts, r.weights)
    ]
    cycle = report._cycle_hint(r.weights)
    return {
        "predicted": to_hex(r.rgb),
        "delta_e": r.delta_e,
        "quality": _quality(r.delta_e),
        "size": r.size,
        "parts": parts,
        "gradient_weights": r.gradient_weights,
        "ratio_text": " · ".join(f"F{p['slot']} {p['percent']}%" for p in parts),
        "cycle": cycle,
        "warnings": report.recipe_warnings(r),
        "banding": r.stripe_level if r.size > 1 else None,
        "banding_score": round(r.stripe, 1),
    }


def _quality(de: float) -> tuple[str, str]:
    if de < 1.0:
        return ("indistinguishable", "good")
    if de < 2.0:
        return ("very close", "good")
    if de < 5.0:
        return ("close", "ok")
    if de < 10.0:
        return ("noticeable difference", "warn")
    return ("poor", "bad")


def build_solver(cfg: Config, opts: MatchOptions) -> tuple[Solver, dict]:
    from spoolmix import printers

    tpl = printers.resolve(cfg, opts.template) if opts.model == "calibrated" else None
    cal = tpl.calibration() if tpl else None
    if opts.model == "calibrated":
        model = get_model("calibrated", alpha=(cal or {}).get("alpha"), calibrated=cal is not None)
        model.strengths = dict((cal or {}).get("strengths") or {})
        model.td_scale = float((cal or {}).get("td_scale", 1.0))
    else:
        model = get_model("orca")
    info = {
        "model": opts.model,
        "calibrated": cal is not None,
        "alpha": getattr(model, "alpha", None),
        "cal_colors": (cal or {}).get("colors") or {},
    }
    return Solver(model=model, min_pct=opts.min_pct, max_pct=opts.max_pct, allow_triples=not opts.pairs_only,
                  priority=opts.priority), info


def apply_calibrated_colors(comps: list[Component], cal_colors: dict[str, str]) -> list[Component]:
    from spoolmix.color import parse_hex

    return [replace(c, rgb=parse_hex(cal_colors[c.key])) if c.key in cal_colors else c for c in comps]


def run_match(cfg: Config, targets: list[tuple[RGB, str]], opts: MatchOptions) -> dict:
    try:
        auto_td(cfg)  # make sure every filament has a TD before warnings are computed
    except Exception:
        pass
    solver, info = build_solver(cfg, opts)
    with open_inventory(cfg) as sm:
        stock = stock_components(sm.filaments())
    if info["cal_colors"]:
        stock = apply_calibrated_colors(stock, info["cal_colors"])
    if opts.material != "any":
        from spoolmix.design import filter_materials

        stock, _ = filter_materials(stock, {opts.material})
    from spoolmix import printers

    tpl = printers.get(cfg, opts.template) if opts.template else None
    out: dict = {"info": info, "stock_count": len(stock), "targets": [], "palette": None, "error": None,
                 "subdivide_template": tpl.name if tpl and tpl.info.get("subdivide") else None,
                 "material": opts.material}
    if len(stock) < 2:
        out["error"] = "Need at least two filaments you have, each with a color and a material."
        return out

    catalog = []
    if opts.shop:
        from spoolmix import shopdb
        from spoolmix.materials import category

        cats = {opts.material} if opts.material != "any" else {category(c.material) for c in stock} - {None}
        catalog = shopdb.candidates(cfg, [t for t, _ in targets], cats, stock)

    if len(targets) > 1:
        res = choose_palette(solver, [t for t, _ in targets], stock, slots=opts.slots)
        index = {c.key: i + 1 for i, c in enumerate(res.slots)}
        out["palette"] = {
            "slots": [{"slot": i + 1, "label": c.label, "hex": to_hex(c.rgb), "td": c.td} for i, c in enumerate(res.slots)],
            "worst": res.worst,
            "mean": res.total / len(targets),
        }
    for i, (t, label) in enumerate(targets):
        entry: dict = {"target": to_hex(t), "label": label, "dark": lightness(t) < report.DARK_L}
        if len(targets) > 1:
            r = res.recipes[i]
            entry["recipes"] = [recipe_view(r, [index[p.key] for p in r.parts])]
        else:
            entry["recipes"] = [recipe_view(r) for r in solver.solve(t, stock, top=opts.top)]
            single = solver.singles(t, stock)[0]
            entry["single"] = {"label": single.parts[0].label, "delta_e": single.delta_e, "hex": to_hex(single.rgb)}
        if opts.shop:
            base, options = shop(solver, t, stock, catalog, max_buy=opts.max_buy, top=opts.top)
            entry["shop"] = {
                "base_de": base.delta_e if base else None,
                "options": [
                    {"buy": [{"label": c.label, "hex": to_hex(c.rgb), "td": c.td, "url": getattr(c.ref, "url", None),
                              "bundle": getattr(c.ref, "bundle", None), "price": getattr(c.ref, "price", None)} for c in o.buy],
                     "improvement": o.improvement,
                     "pairs_with": [p.label for p in o.recipe.parts if p.owned],
                     "recipe": recipe_view(o.recipe)}
                    for o in options
                ],
            }
        out["targets"].append(entry)
    return out


def stock_categories(cfg: Config) -> list[str]:
    """Material categories present in stock, most common first."""
    from collections import Counter

    from spoolmix.materials import category

    with open_inventory(cfg) as sm:
        cats = Counter(category(f.material) for f in sm.filaments() if f.in_stock)
    return [c for c, _ in cats.most_common() if c]


def default_priority(cfg: Config) -> str:
    """Striping vs color trade-off: last used (or set in Settings), else balanced."""
    from spoolmix import shopdb
    from spoolmix.solver import DEFAULT_PRIORITY, PRIORITIES

    v = shopdb.load_settings(cfg).get("priority")
    return v if v in PRIORITIES else DEFAULT_PRIORITY


def default_slots(cfg: Config) -> int:
    """Filament slots to plan for when there's no template choice: last used, else the
    default template's filament count, else 4."""
    from spoolmix import printers, shopdb

    v = shopdb.load_settings(cfg).get("slots")
    if isinstance(v, int) and 1 <= v <= MAX_SLOTS:
        return v
    t = printers.default(cfg)
    return t.slots if t and 1 <= t.slots <= MAX_SLOTS else 4


MAX_SLOTS = 16
