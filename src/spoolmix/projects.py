"""Uploaded design projects: storage, background jobs, plan/convert pipeline for the web UI."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from spoolmix import design, service
from spoolmix.color import to_hex
from spoolmix.config import Config
from spoolmix.project3mf import Assignment, DesignFilament, analyze, convert
from spoolmix.inventory import open_inventory

_ID = re.compile(r"[0-9a-f]{12}")
_executor = ThreadPoolExecutor(max_workers=1)  # one heavy job at a time
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


# --- storage ---------------------------------------------------------------------


def root(cfg: Config) -> Path:
    return cfg.data_dir / "projects"


def pdir(cfg: Config, pid: str) -> Path:
    if not _ID.fullmatch(pid):
        raise ValueError("bad project id")
    return root(cfg) / pid


def load_meta(cfg: Config, pid: str) -> dict:
    return json.loads((pdir(cfg, pid) / "meta.json").read_text())


def save_meta(cfg: Config, pid: str, meta: dict) -> None:
    # Atomic: pages read meta.json while background jobs rewrite it; a half-written file
    # would read as "no such design".
    path = pdir(cfg, pid) / "meta.json"
    tmp = path.with_name(f"meta.json.{threading.get_ident()}.part")
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(path)


def list_projects(cfg: Config) -> list[dict]:
    out = []
    if root(cfg).is_dir():
        for d in root(cfg).iterdir():
            try:
                out.append(load_meta(cfg, d.name) | {"id": d.name})
            except (ValueError, FileNotFoundError, json.JSONDecodeError):
                continue
    return sorted(out, key=lambda m: m.get("uploaded", ""), reverse=True)


def create(cfg: Config, filename: str, fileobj) -> str:
    pid = uuid.uuid4().hex[:12]
    d = pdir(cfg, pid)
    d.mkdir(parents=True)
    with open(d / "source.3mf", "wb") as fh:
        shutil.copyfileobj(fileobj, fh, 1 << 20)
    name = re.sub(r"\.3mf$", "", Path(filename or "design.3mf").name, flags=re.I)
    save_meta(cfg, pid, {"name": name, "uploaded": dt.datetime.now().isoformat(timespec="seconds"),
                         "important": [], "analysis": None, "plans": None})
    return pid


def delete(cfg: Config, pid: str) -> None:
    shutil.rmtree(pdir(cfg, pid), ignore_errors=True)


# --- jobs --------------------------------------------------------------------------


def job(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        return dict(j) if j else None


def submit(kind: str, pid: str, fn, *args) -> str:
    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {"id": jid, "kind": kind, "project": pid, "status": "queued", "message": "waiting…", "error": None}

    def progress(msg: str) -> None:
        with _lock:
            _jobs[jid]["message"] = msg

    def run():
        with _lock:
            _jobs[jid]["status"] = "running"
        try:
            fn(*args, progress)
            with _lock:
                _jobs[jid].update(status="done", message="done")
        except Exception as exc:  # report to the page
            traceback.print_exc()
            with _lock:
                _jobs[jid].update(status="error", error=f"{type(exc).__name__}: {exc}")

    _executor.submit(run)
    return jid


def active_job(pid: str) -> dict | None:
    with _lock:
        for j in reversed(list(_jobs.values())):
            if j["project"] == pid and j["status"] in ("queued", "running"):
                return dict(j)
    return None


# --- pipeline ----------------------------------------------------------------------


def run_analyze(cfg: Config, pid: str, progress) -> None:
    a = analyze(pdir(cfg, pid) / "source.3mf", progress)
    meta = load_meta(cfg, pid)
    meta["analysis"] = a.to_json()
    meta["plans"] = None
    save_meta(cfg, pid, meta)


def _design_filaments(meta: dict) -> list[DesignFilament]:
    return [DesignFilament(f["id"], f["hex"], f["type"], f["settings_id"], f["area"]) for f in meta["analysis"]["filaments"]]


def design_material_categories(meta: dict) -> set[str]:
    """Material families the design uses (e.g. {'PLA'}); empty before analysis."""
    if not meta.get("analysis"):
        return set()
    return design.design_categories(_design_filaments(meta))


def _plan_view(p: design.DesignPlan, all_design: list[DesignFilament], meta: dict, slots: int) -> dict:
    asg = p.assignment(all_design, slots)
    shares = {f["id"]: f["share"] for f in meta["analysis"]["filaments"]}
    important = set(meta.get("important") or [])
    slot_of = {c.key: i + 1 for i, c in enumerate(p.result.slots)}
    colors = []
    for t, r in zip(p.targets, p.result.recipes):
        view = service.recipe_view(r, [slot_of[c.key] for c in r.parts])
        colors.append({"id": t.id, "hex": t.hex, "share": shares.get(t.id, 0), "important": t.id in important,
                       "new_id": asg.targets[t.id], "recipe": view})
    return {
        "buy": [{"label": c.label, "hex": to_hex(c.rgb), "td": c.td, "url": getattr(c.ref, "url", None),
                 "bundle": getattr(c.ref, "bundle", None), "price": getattr(c.ref, "price", None)} for c in p.buy],
        "slots": [{"slot": i + 1, "key": c.key, "label": c.label, "hex": to_hex(c.rgb), "td": c.td, "owned": c.owned,
                   "material": c.material, "url": getattr(c.ref, "url", None)}
                  for i, c in enumerate(p.result.slots)],
        "weighted": p.result.weighted, "worst": p.result.worst, "simplest": p.simplest,
        "colors": colors,
        "mixes": len(asg.mixed_rows),
        "assignment": {"physical": asg.physical, "targets": {str(k): v for k, v in asg.targets.items()},
                       "mixed_rows": asg.mixed_rows, "materials": asg.materials},
    }


def run_plan(cfg: Config, pid: str, opts: dict, progress) -> None:
    meta = load_meta(cfg, pid)
    all_design = _design_filaments(meta)
    solver, info = service.build_solver(cfg, service.MatchOptions(model=opts.get("model", "orca"),
                                                                    max_pct=int(opts.get("max_pct", 70)),
                                                                    priority=opts.get("priority", "balanced"),
                                                                    template=opts.get("template")))
    progress("updating filament TDs")
    try:
        service.auto_td(cfg)
    except Exception:
        pass
    progress("reading your filaments")
    with open_inventory(cfg) as sm:
        stock = service.stock_components(sm.filaments())
    if info["cal_colors"]:
        stock = service.apply_calibrated_colors(stock, info["cal_colors"])
    designed = design.design_categories(all_design)
    # "Print in": the designer's material(s), or another one you chose (e.g. PLA for a PETG
    # design when that's what your printer template and spools are set up for).
    allowed = {opts["material"]} if opts.get("material") else designed
    catalog = []
    max_buy = int(opts.get("max_buy", 0)) if opts.get("shop") else 0
    if max_buy:
        from spoolmix import shopdb

        progress("finding filaments you could buy")
        used = [f for f in all_design if f.area > 0] or all_design
        catalog = shopdb.candidates(cfg, [f.rgb for f in used], allowed, stock)
    _, excluded = design.filter_materials(stock, allowed)
    slots = int(opts.get("slots", 4))
    base, options = design.plan(solver, all_design, stock, catalog, set(meta.get("important") or []),
                                max_buy=max_buy, slots=slots, progress=progress, materials=allowed)
    for old in pdir(cfg, pid).glob("out-*.3mf"):
        old.unlink()  # outputs of a previous plan are stale now
    meta = load_meta(cfg, pid)
    meta["plans"] = {
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "opts": opts, "model": info,
        "materials": sorted(allowed),
        "designed_materials": sorted(designed),
        "excluded": sorted({f"{c.material}" for c in excluded}),
        "excluded_count": len(excluded),
        "base": _plan_view(base, all_design, meta, slots),
        "options": [_plan_view(o, all_design, meta, slots) for o in options],
    }
    save_meta(cfg, pid, meta)


def plan_by_name(plans: dict, which: str) -> dict:
    """'base', 'optN' or 'custom' (your swapped version)."""
    if which == "base":
        return plans["base"]
    if which == "custom":
        if not plans.get("custom"):
            raise ValueError("no custom plan")
        return plans["custom"]
    return plans["options"][int(which[3:])]


def run_custom(cfg: Config, pid: str, which: str, keys: list[str], progress) -> None:
    """Re-plan `which` with the filaments you picked (keys, in slot order; may include that
    plan's purchases). Saved as plans['custom'], replacing an earlier one."""
    meta = load_meta(cfg, pid)
    plans = meta["plans"]
    src = plan_by_name(plans, which)
    opts = plans["opts"]
    solver, _ = service.build_solver(cfg, service.MatchOptions(model=opts.get("model", "orca"),
                                                                 max_pct=int(opts.get("max_pct", 70)),
                                                                 priority=opts.get("priority", "balanced"),
                                                                 template=opts.get("template")))
    progress("reading your filaments")
    with open_inventory(cfg) as sm:
        stock = {c.key: c for c in service.stock_components(sm.filaments())}
    # Spools to buy from the plan you started from: rebuilt from what the plan recorded.
    from spoolmix.color import parse_hex
    from spoolmix.solver import Component

    bought = {s["key"]: Component(s["key"], s["label"], parse_hex(s["hex"]), s.get("td"), owned=False,
                                  material=s.get("material"))
              for s in src["slots"] if s.get("key") and not s.get("owned")}
    comps = []
    for k in keys:
        c = stock.get(k) or bought.get(k)
        if c is None:
            raise ValueError("One of the filaments you picked isn't in your filaments anymore. Reload the page and try again.")
        comps.append(c)
    progress("trying your filaments")
    all_design = _design_filaments(meta)
    try:
        p = design.plan_fixed(solver, all_design, comps, set(meta.get("important") or []),
                              buy=[c for c in comps if not c.owned])
    except ValueError:
        raise ValueError("Those filaments can't all be loaded for one print: their materials don't bond.")
    slots = int(opts.get("slots", 4))
    view = _plan_view(p, all_design, meta, slots)
    view["from"] = which
    out = output_path(cfg, pid, "custom")
    if out.exists():
        out.unlink()  # built from an earlier custom plan
    meta = load_meta(cfg, pid)
    meta["plans"]["custom"] = view
    save_meta(cfg, pid, meta)


def stock_overview(cfg: Config) -> list[dict]:
    """Your usable filaments for the swap menus: key, label, hex, material family."""
    from spoolmix.materials import category

    with open_inventory(cfg) as sm:
        stock = service.stock_components(sm.filaments())
    return sorted(({"key": c.key, "label": c.label, "hex": to_hex(c.rgb), "category": category(c.material)} for c in stock),
                  key=lambda d: d["label"].lower())


def output_path(cfg: Config, pid: str, which: str) -> Path:
    if not re.fullmatch(r"base|opt\d|custom", which):
        raise ValueError("bad plan id")
    return pdir(cfg, pid) / f"out-{which}.3mf"


def run_convert(cfg: Config, pid: str, which: str, progress) -> None:
    meta = load_meta(cfg, pid)
    plans = meta["plans"]
    plan = plan_by_name(plans, which)
    a = plan["assignment"]
    # Each slot's material, so the build can give it the template's matching preset. Plans from
    # before this was stored fall back to the materials recorded on their slots.
    materials = a.get("materials") or [s.get("material") for s in plan["slots"]]
    materials = (list(materials) + [None] * len(a["physical"]))[: len(a["physical"])]
    asg = Assignment([tuple(x) for x in a["physical"]], {int(k): v for k, v in a["targets"].items()}, a["mixed_rows"],
                     materials)
    from spoolmix import printers

    tpl = printers.get(cfg, plans["opts"].get("template"))
    if tpl is None:
        raise ValueError("This plan's printer template is gone (or it was planned without one). "
                         "Choose a template under \"Print with\" and re-plan.")
    template = tpl.path
    out = output_path(cfg, pid, which)
    tmp = out.with_suffix(".part")
    convert(pdir(cfg, pid) / "source.3mf", template, asg, tmp, progress)
    tmp.replace(out)
