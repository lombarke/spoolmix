"""spoolmix web UI (FastAPI + server-rendered Jinja templates, no build step)."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from spoolmix import __version__, calibrate, inventory, printers, projects, service
from spoolmix.catalog import load_catalog
from spoolmix.color import parse_hex, to_hex
from spoolmix.config import load_config
from spoolmix.inventory import open_inventory

HERE = Path(__file__).parent
app = FastAPI(title="spoolmix", version=__version__)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["version"] = __version__


def _asset_url(name: str) -> str:
    """/static/<name>?v=<content hash>: a new URL whenever the file changes, so no browser
    keeps running an old cached copy after an update."""
    import hashlib

    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


templates.env.globals["asset"] = _asset_url
templates.env.filters["de"] = lambda v: f"{v:.2f}" if v is not None else "—"
templates.env.globals["texture"] = __import__("spoolmix.report", fromlist=["texture"]).texture


def _cfg():
    return load_config()


def _redirect(path: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    if msg:
        path, hash_, frag = path.partition("#")
        path += ("&" if "?" in path else "?") + ("err=" if error else "msg=") + quote(msg) + hash_ + frag
    return RedirectResponse(path, status_code=303)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("source", inventory.source(_cfg()))
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def _td_loop() -> None:
    """Keep TDs filled in: at startup, then every 6 hours (published sources re-fetched weekly)."""
    import time

    while True:
        try:
            service.auto_td(_cfg())
        except inventory.NotConfigured:
            pass  # not set up yet
        except Exception as exc:  # Spoolman unreachable etc.; try again later
            print(f"auto TD sync failed: {exc}", flush=True)
        try:
            from spoolmix import shopdb

            shopdb.ensure_fresh(_cfg())
        except Exception as exc:
            print(f"SpoolmanDB refresh failed: {exc}", flush=True)
        time.sleep(6 * 3600)


@app.on_event("startup")
def _start_td_loop() -> None:
    import os
    import threading

    if os.environ.get("SPOOLMIX_AUTO_TD", "1") != "0":
        threading.Thread(target=_td_loop, name="auto-td", daemon=True).start()


# Pages that work before setup; everything else sends you to /setup first.
SETUP_EXEMPT = ("/setup", "/static/", "/healthz", "/api/spoolman/test", "/api/db/")


@app.middleware("http")
async def _require_setup(request: Request, call_next):
    path = request.url.path
    if not path.startswith(SETUP_EXEMPT) and inventory.source(_cfg()) is None:
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


def _template(cfg) -> printers.Template | None:
    """The default printer template, or None if there are none."""
    return printers.default(cfg)


def _safe_next(url: str | None, default: str) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else default


def _sync_td_soon() -> None:
    import os
    import threading

    if os.environ.get("SPOOLMIX_AUTO_TD", "1") == "0":
        return

    def run():
        try:
            service.auto_td(_cfg())
        except Exception as exc:
            print(f"auto TD sync failed: {exc}", flush=True)

    threading.Thread(target=run, name="auto-td-once", daemon=True).start()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": __version__}


# --- designs (projects) ----------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def projects_page(request: Request):
    cfg = _cfg()
    return _page(request, "projects.html", active="projects", projects=projects.list_projects(cfg),
                 has_template=_template(cfg) is not None)


@app.post("/projects")
async def project_upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".3mf"):
        return _redirect("/", "Please upload a .3mf project file.", error=True)
    cfg = _cfg()
    try:
        with zipfile.ZipFile(file.file) as z:
            z.getinfo("Metadata/project_settings.config")
    except (zipfile.BadZipFile, KeyError):
        return _redirect("/", "That .3mf has no colors or project settings. Save or export it as a 3MF project from your slicer and try again.", error=True)
    file.file.seek(0)
    pid = projects.create(cfg, file.filename, file.file)
    projects.submit("analyze", pid, projects.run_analyze, cfg, pid)
    return _redirect(f"/projects/{pid}")


def _project_or_404(cfg, pid: str) -> dict:
    try:
        return projects.load_meta(cfg, pid)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404)


@app.get("/projects/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str):
    cfg = _cfg()
    # Check for a running job before reading results: a job that finishes in between has
    # already saved them, so the page shows either the job or its results, never neither.
    job = projects.active_job(pid)
    meta = _project_or_404(cfg, pid)
    outputs = {p.stem[4:] for p in projects.pdir(cfg, pid).glob("out-*.3mf")}
    templates = printers.all_templates(cfg)
    plan_opts = (meta.get("plans") or {}).get("opts") or {}
    cats = projects.design_material_categories(meta)
    print_material = plan_opts.get("material") or ""
    chosen = printers.get(cfg, plan_opts.get("template")) or printers.best_for(cfg, {print_material} if print_material else cats)
    plan_template = printers.get(cfg, plan_opts.get("template")) if meta.get("plans") else None
    try:
        swap_stock = projects.stock_overview(cfg)
    except Exception:  # filament source unreachable: no material choice or swapping
        swap_stock = []
    from collections import Counter

    stock_materials = dict(Counter(f["category"] for f in swap_stock if f["category"]).most_common())
    # Suggest printing in the template's material when the design's is a poor fit: the
    # template is set up for another material and you have more filament in that one.
    suggest = None
    if chosen and cats and chosen.categories and not (chosen.categories & cats) and not print_material:
        alt = max(chosen.categories, key=lambda c: stock_materials.get(c, 0))
        if stock_materials.get(alt, 0) > sum(stock_materials.get(c, 0) for c in cats):
            suggest = alt
    return _page(request, "project.html", active="projects", pid=pid, meta=meta, job=job,
                 outputs=outputs, has_template=bool(templates), templates=templates, chosen=chosen,
                 plan_template=plan_template, design_categories=sorted(cats), stock_materials=stock_materials,
                 print_material=print_material, suggest_material=suggest,
                 designed_count=sum(stock_materials.get(c, 0) for c in cats), swap_stock=swap_stock,
                 calibrated={t.id: t.calibration() is not None for t in templates},
                 default_slots=service.default_slots(cfg), default_priority=service.default_priority(cfg))


@app.post("/projects/{pid}/plan")
async def project_plan(request: Request, pid: str):
    cfg = _cfg()
    meta = _project_or_404(cfg, pid)
    form = await request.form()
    meta["important"] = sorted(int(v) for v in form.getlist("important"))
    projects.save_meta(cfg, pid, meta)
    from spoolmix import shopdb
    from spoolmix.solver import PRIORITIES

    material = str(form.get("material") or "")
    material = material if re.fullmatch(r"[A-Z][A-Z0-9+-]{1,15}", material) else None  # None: as designed
    # With printer templates, the slot count is the chosen template's; without, the number typed.
    tpl = printers.resolve(cfg, str(form.get("template") or ""))
    slots = tpl.slots if tpl else max(1, min(service.MAX_SLOTS, int(form.get("slots") or 4)))
    priority = form.get("priority") if form.get("priority") in PRIORITIES else service.default_priority(cfg)
    shopdb.save_settings(cfg, priority=priority, **({} if tpl else {"slots": slots}))  # remembered as the defaults
    opts = {"shop": bool(form.get("shop")), "max_buy": int(form.get("max_buy") or 2), "slots": slots, "priority": priority,
            "template": tpl.id if tpl else None,
            "material": material,
            "model": form.get("model") if form.get("model") in ("orca", "calibrated") else "orca",
            "max_pct": max(50, min(100, int(form.get("max_pct") or 70)))}
    projects.submit("plan", pid, projects.run_plan, cfg, pid, opts)
    return _redirect(f"/projects/{pid}")


@app.post("/projects/{pid}/convert/{which}")
def project_convert(pid: str, which: str):
    cfg = _cfg()
    _project_or_404(cfg, pid)
    try:
        projects.output_path(cfg, pid, which)
    except ValueError:
        raise HTTPException(400)
    projects.submit("convert", pid, projects.run_convert, cfg, pid, which)
    return _redirect(f"/projects/{pid}#plan-{which}")


@app.get("/projects/{pid}/download/{which}")
def project_download(pid: str, which: str):
    cfg = _cfg()
    meta = _project_or_404(cfg, pid)
    try:
        path = projects.output_path(cfg, pid, which)
    except ValueError:
        raise HTTPException(404)
    if not path.is_file():
        raise HTTPException(404)
    names = {"base": "", "custom": "-custom"}
    suffix = names[which] if which in names else f"-option{int(which[3:]) + 1}"
    return FileResponse(path, media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
                        filename=f"{meta['name']}-spoolmix{suffix}.3mf")


@app.post("/projects/{pid}/custom")
async def project_custom(request: Request, pid: str):
    """Try a plan with filaments you picked: which = the plan you started from, f1..fN = the
    filament keys per slot ('' = leave the slot empty)."""
    cfg = _cfg()
    meta = _project_or_404(cfg, pid)
    form = await request.form()
    which = str(form.get("which") or "base")
    try:
        projects.plan_by_name(meta.get("plans") or {}, which)
    except (KeyError, ValueError, IndexError, TypeError):
        raise HTTPException(400)
    keys: list[str] = []
    for i in range(1, service.MAX_SLOTS + 1):
        k = str(form.get(f"f{i}") or "")
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return _redirect(f"/projects/{pid}#plan-{which}", "Pick at least one filament.", error=True)
    projects.submit("custom", pid, projects.run_custom, cfg, pid, which, keys)
    return _redirect(f"/projects/{pid}#plan-custom")


@app.post("/projects/{pid}/custom/discard")
def project_custom_discard(pid: str):
    cfg = _cfg()
    meta = _project_or_404(cfg, pid)
    if (meta.get("plans") or {}).pop("custom", None) is not None:
        projects.save_meta(cfg, pid, meta)
    projects.output_path(cfg, pid, "custom").unlink(missing_ok=True)
    return _redirect(f"/projects/{pid}")


@app.post("/projects/{pid}/delete")
def project_delete(pid: str):
    cfg = _cfg()
    _project_or_404(cfg, pid)
    projects.delete(cfg, pid)
    return _redirect("/", "Design deleted.")


@app.get("/jobs/{jid}")
def job_status(jid: str) -> JSONResponse:
    j = projects.job(jid)
    if j is None:
        raise HTTPException(404)
    return JSONResponse(j)


# --- match ----------------------------------------------------------------------


@app.get("/match", response_class=HTMLResponse)
def match_page(request: Request):
    cfg = _cfg()
    templates = printers.all_templates(cfg)
    try:
        cats = service.stock_categories(cfg)
    except Exception:
        cats = []
    return _page(request, "match.html", active="match", templates=templates,
                 calibrated={t.id: t.calibration() is not None for t in templates}, categories=cats,
                 default_slots=service.default_slots(cfg), priority=service.default_priority(cfg))


@app.post("/match", response_class=HTMLResponse)
def match_run(
    request: Request,
    targets: str = Form(""),
    shop: bool = Form(False),
    max_buy: int = Form(2),
    model: str = Form("orca"),
    max_pct: int = Form(70),
    pairs_only: bool = Form(False),
    top: int = Form(3),
    material: str = Form("any"),
    slots: int = Form(4),
    priority: str = Form("balanced"),
    template: str = Form(""),
):
    parsed: list[tuple] = []
    bad: list[str] = []
    for line in re.split(r"[\n,;]+", targets):
        line = line.strip()
        if not line:
            continue
        hexpart, _, label = line.partition(" ")
        try:
            parsed.append((parse_hex(hexpart), label.strip()))
        except ValueError:
            bad.append(hexpart)
    if bad or not parsed:
        return templates.TemplateResponse(
            request, "_results.html",
            {"result": {"error": f"Not a hex color: {', '.join(bad)}" if bad else "Add at least one target color."}},
        )
    opts = service.MatchOptions(
        shop=shop, max_buy=max(1, min(2, max_buy)), model=model if model in ("orca", "calibrated") else "orca",
        max_pct=max(50, min(100, max_pct)), pairs_only=pairs_only, top=max(1, min(10, top)),
        material=material if re.fullmatch(r"[A-Za-z]+", material or "") else "any",
        slots=max(1, min(service.MAX_SLOTS, slots)), priority=priority,
    )
    tpl = printers.resolve(_cfg(), template)
    if tpl:
        opts.slots, opts.template = tpl.slots, tpl.id
    from spoolmix import shopdb
    from spoolmix.solver import PRIORITIES

    if priority not in PRIORITIES:
        opts.priority = service.default_priority(_cfg())
    shopdb.save_settings(_cfg(), priority=opts.priority, **({} if tpl else {"slots": opts.slots}))
    try:
        result = service.run_match(_cfg(), parsed[:12], opts)
    except Exception as exc:  # surface Spoolman/connectivity problems in the page
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return templates.TemplateResponse(request, "_results.html", {"result": result, "opts": opts})


@app.post("/api/image-colors")
async def image_colors(file: UploadFile = File(...), k: int = Form(5)) -> JSONResponse:
    from spoolmix.imagepick import dominant_colors

    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "image too large")
    tmp = io.BytesIO(data)
    cols = dominant_colors(tmp, max(1, min(12, k)))  # PIL accepts file-like objects
    return JSONResponse([{"hex": to_hex(c), "share": round(s, 3)} for c, s in cols])


# --- filaments & TD --------------------------------------------------------------


@app.get("/filaments", response_class=HTMLResponse)
def filaments_page(request: Request, show_all: bool = False, edit: int | None = None):
    cfg = _cfg()
    src = inventory.source(cfg)
    local = src.kind == "local"
    try:
        sync = service.auto_td(cfg)  # fills any new filament right away
        with open_inventory(cfg, src) as sm:
            fils = sm.filaments()
    except Exception as exc:
        return _page(request, "filaments.html", active="filaments", rows=[], show_all=show_all, sync=None, local=local,
                     published_age=None, err=f"Can't reach {src.description}: {exc}")
    missing = {m["id"]: m["reason"] for m in (sync or {}).get("missing", [])}
    rows = []
    for f in fils:
        if not (show_all or local or f.in_stock):
            continue
        rows.append({"f": f, "owner": service.td_owner(f), "multicolor": service.is_multicolor(f),
                     "missing": missing.get(f.id), "source": _source_view(f.td_source)})
    if local:
        rows.sort(key=lambda r: (not r["f"].in_stock, (r["f"].vendor or "").lower(), (r["f"].material or "").lower(),
                                 r["f"].name.lower()))
    return _page(request, "filaments.html", active="filaments", rows=rows, show_all=show_all, sync=sync,
                 published_age=service.published_age_days(cfg), user_db=str(cfg.td_user), local=local, edit=edit,
                 materials=_material_choices(), db_ready=_db_ready(cfg))


def _source_view(src: str | None) -> dict:
    """Split a td_source string into something readable."""
    if not src:
        return {}
    if src.startswith(service.LOOKUP_PREFIX):
        parts = [p.strip() for p in src[len(service.LOOKUP_PREFIX):].split("|")]
        url = parts[1] if len(parts) > 1 and parts[1].startswith("http") else None
        return {"kind": "published", "text": parts[0], "url": url}
    if src.startswith(service.ESTIMATE_PREFIX):
        return {"kind": "estimate", "text": src[len(service.ESTIMATE_PREFIX):].split("|")[0].strip()}
    if src.startswith("measured:"):
        return {"kind": "yours", "text": "measured (" + src.split(":")[1] + ")"}
    return {"kind": "yours", "text": src}


@app.post("/td/refresh")
def td_refresh():
    sync = service.auto_td(_cfg(), refresh=True)
    if sync.get("skipped"):
        return _redirect("/filaments", "A TD update is already running; reload in a moment.")
    if sync.get("fetch_error"):
        return _redirect("/filaments", f"Couldn't fetch all sources ({sync['fetch_error']}); used cached values.", error=True)
    n = len(sync["written"])
    return _redirect("/filaments", f"Checked published sources. Updated {n} filament{'s' if n != 1 else ''}.")


@app.post("/td/set")
def td_set(filament_id: int = Form(...), td: float = Form(...), method: str = Form("other"), add_to_db: bool = Form(False)):
    import datetime as dt

    if not 0 < td <= 100:
        return _redirect("/filaments", "TD must be between 0 and 100.", error=True)
    method = method if method in ("td1", "seashell", "other") else "other"
    cfg = _cfg()
    with open_inventory(cfg) as sm:
        f = next((x for x in sm.filaments() if x.id == filament_id), None)
        if f is None:
            return _redirect("/filaments", f"No filament {filament_id}.", error=True)
        sm.set_filament_td(f.id, td, f"measured:{method}:{dt.date.today().isoformat()}")
    msg = f"Set {f.label} TD {td:g}."
    if add_to_db:
        service.td_db(cfg).add_user_entry(f.vendor or "Unknown", f.material or "Unknown", f.name, td, f.hex, method)
        msg += " Saved to your measurements file too."
    return _redirect("/filaments", msg)


LOCAL_MSG = "That only works with spoolmix's own filament list (Settings → Where your filaments live)."


def _local_or_error():
    cfg = _cfg()
    src = inventory.source(cfg)
    if src is None or src.kind != "local":
        return None
    return inventory.LocalInventory(cfg.data_dir / inventory.LOCAL_FILE)


def _material_choices() -> list[str]:
    return ["PLA", "PLA+", "PLA Matte", "PLA Silk", "PETG", "PCTG", "ABS", "ASA", "TPU", "PC", "PA", "HIPS", "PVA"]


def _db_ready(cfg) -> bool:
    from spoolmix import shopdb

    return shopdb.age_days(cfg) is not None


def _filament_form(form) -> dict:
    values = {k: str(form.get(k) or "").strip() for k in ("brand", "material", "name", "hex")}
    if not values["name"]:
        raise ValueError("Give the filament a color name.")
    if not values["material"]:
        raise ValueError("Pick a material (e.g. PLA): spoolmix never mixes incompatible materials.")
    if inventory.clean_hex(values["hex"]) is None:
        raise ValueError(f"'{values['hex']}' is not a hex color like #3A7BD5.")
    if form.get("db_id"):
        values["db_id"] = str(form.get("db_id"))[:120]
    return values


@app.post("/filaments/add")
async def filament_add(request: Request):
    import datetime as dt

    local = _local_or_error()
    if local is None:
        return _redirect("/filaments", LOCAL_MSG, error=True)
    form = await request.form()
    try:
        values = _filament_form(form)
        td = str(form.get("td") or "").strip()
        if td:
            tdv = float(td)
            if not 0 < tdv <= 100:
                raise ValueError("TD must be between 0 and 100.")
            values |= {"td": tdv, "td_source": f"measured:other:{dt.date.today().isoformat()}"}
    except ValueError as exc:
        return _redirect("/filaments#add", str(exc), error=True)
    local.add(**values)
    label = " ".join(x for x in (values["brand"], values["material"], values["name"]) if x)
    from spoolmix.report import texture

    note = (" It's speckled, so it won't print as one flat color: spoolmix uses its average color and warns you where it's used."
            if texture(label) == "speckled" else "")
    return _redirect("/filaments", f"Added {label}.{note}")


@app.post("/filaments/{fid}/edit")
async def filament_edit(request: Request, fid: int):
    local = _local_or_error()
    if local is None:
        return _redirect("/filaments", LOCAL_MSG, error=True)
    row = local.get(fid)
    if row is None:
        raise HTTPException(404)
    form = await request.form()
    try:
        values = _filament_form(form)
    except ValueError as exc:
        return _redirect(f"/filaments?edit={fid}#f{fid}", str(exc), error=True)
    changed = any((row.get(k) or "") != (inventory.clean_hex(v) if k == "hex" else v) for k, v in values.items() if k != "db_id")
    f = local._filament(row)
    if changed and service.td_owner(f) in ("lookup", "estimate"):
        values |= {"td": None, "td_source": None}  # a different filament now: look its TD up again
    local.update(fid, **values)
    return _redirect(f"/filaments#f{fid}", "Saved.")


@app.post("/filaments/{fid}/have")
def filament_have(fid: int, have: bool = Form(False)):
    local = _local_or_error()
    if local is None:
        return _redirect("/filaments", LOCAL_MSG, error=True)
    try:
        local.update(fid, have=have)
    except KeyError:
        raise HTTPException(404)
    return _redirect(f"/filaments#f{fid}")


@app.post("/filaments/{fid}/delete")
def filament_delete(fid: int):
    local = _local_or_error()
    if local is None:
        return _redirect("/filaments", LOCAL_MSG, error=True)
    row = local.get(fid)
    if row is None:
        raise HTTPException(404)
    local.delete(fid)
    return _redirect("/filaments", f"Deleted {row.get('brand') or ''} {row.get('name') or ''}.".replace("  ", " "))


# --- SpoolmanDB browsing (the "add filament" form) ---------------------------------


@app.get("/api/db/brands")
def db_brands() -> JSONResponse:
    from spoolmix import shopdb

    cfg = _cfg()
    shopdb.ensure_fresh(cfg)
    return JSONResponse(shopdb.brands(cfg))


@app.get("/api/db/materials")
def db_materials(brand: str) -> JSONResponse:
    from spoolmix import shopdb

    return JSONResponse(shopdb.materials(_cfg(), brand))


@app.get("/api/db/colors")
def db_colors(brand: str, material: str) -> JSONResponse:
    from spoolmix import shopdb

    return JSONResponse(shopdb.colors(_cfg(), brand, material))


# --- setup & settings --------------------------------------------------------------


def _source_ctx(cfg) -> dict:
    from spoolmix.settings import load_settings

    src = inventory.source(cfg)
    s = load_settings(cfg)
    return {"src": src, "spoolman_url": (src.url if src and src.kind == "spoolman" else s.get("spoolman_url"))
            or cfg.spoolman_url or "", "td_in_spoolman": src.td_in_spoolman if src and src.kind == "spoolman"
            else s.get("td_in_spoolman", True)}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    cfg = _cfg()
    return _page(request, "setup.html", active=None, **_source_ctx(cfg))


@app.post("/api/spoolman/test")
def spoolman_test(url: str = Form("")) -> JSONResponse:
    return JSONResponse(inventory.test_spoolman(url))


async def _save_source(request: Request, back: str):
    form = await request.form()
    kind = str(form.get("kind") or "")
    cfg = _cfg()
    if kind == "spoolman":
        res = inventory.test_spoolman(str(form.get("spoolman_url") or ""))
        if not res["ok"]:
            return None, _redirect(back, res["error"], error=True)
        inventory.save_source(cfg, "spoolman", res["url"], td_in_spoolman=bool(form.get("td_in_spoolman")))
        _sync_td_soon()
        return "spoolman", None
    if kind == "local":
        inventory.save_source(cfg, "local")
        return "local", None
    return None, _redirect(back, "Choose Spoolman or spoolmix's own list.", error=True)


@app.post("/setup")
async def setup_save(request: Request):
    kind, err = await _save_source(request, "/setup")
    if err:
        return err
    then = "/filaments" if kind == "local" else "/"
    if _template(_cfg()) is None:
        return _redirect(f"/setup/printer?next={quote(then)}")
    if kind == "local":
        return _redirect("/filaments", "All set. Add the filaments you have, and spoolmix fills in the rest.")
    return _redirect("/", "Connected to Spoolman. spoolmix is filling in TD values in the background.")


@app.get("/setup/printer", response_class=HTMLResponse)
def setup_printer(request: Request, next: str = "/"):
    return _page(request, "setup_printer.html", active=None, template=_template(_cfg()), next=_safe_next(next, "/"))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from spoolmix import shopdb

    cfg = _cfg()
    local_count = len(inventory.LocalInventory(cfg.data_dir / inventory.LOCAL_FILE).filaments())
    return _page(request, "settings.html", active="settings", **_source_ctx(cfg), local_count=local_count,
                 templates=printers.all_templates(cfg),
                 scopes=shopdb.SCOPES, scope=shopdb.shop_scope(cfg), slots=service.default_slots(cfg),
                 priority=service.default_priority(cfg),
                 max_slots=service.MAX_SLOTS)


@app.post("/settings/source")
async def settings_source(request: Request):
    kind, err = await _save_source(request, "/settings")
    if err:
        return err
    return _redirect("/settings", "Now using " + inventory.source(_cfg()).description + ".")


@app.post("/settings/general")
def settings_general(slots: int = Form(4), scope: str = Form("all"), priority: str = Form("balanced")):
    from spoolmix import shopdb
    from spoolmix.solver import PRIORITIES

    if scope not in shopdb.SCOPES or priority not in PRIORITIES:
        raise HTTPException(400)
    shopdb.save_settings(_cfg(), slots=max(1, min(service.MAX_SLOTS, slots)), shop_scope=scope, priority=priority)
    return _redirect("/settings", "Saved.")


@app.post("/settings/import-spoolman")
def settings_import(url: str = Form("")):
    res = inventory.test_spoolman(url)
    if not res["ok"]:
        return _redirect("/settings", res["error"], error=True)
    n = inventory.import_from_spoolman(_cfg(), res["url"])
    return _redirect("/settings", f"Copied {n} filament{'s' if n != 1 else ''} from Spoolman into your list.")


# --- catalog ---------------------------------------------------------------------


@app.get("/catalog")
def catalog_redirect():
    return RedirectResponse("/shopping", status_code=301)


@app.get("/shopping", response_class=HTMLResponse)
def shopping_page(request: Request):
    from spoolmix import shopdb

    cfg = _cfg()
    items = load_catalog(cfg.catalog, service.td_db(cfg), cfg.data_dir)
    return _page(request, "shopping.html", active="shopping", items=items, scopes=shopdb.SCOPES,
                 scope=shopdb.shop_scope(cfg), db_count=shopdb.db_count(cfg), db_age=shopdb.age_days(cfg))


@app.post("/shopping/settings")
def shopping_settings(scope: str = Form("all")):
    from spoolmix import shopdb

    if scope not in shopdb.SCOPES:
        raise HTTPException(400)
    shopdb.save_settings(_cfg(), shop_scope=scope)
    return _redirect("/settings", "Saved.")


@app.post("/shopping/refresh")
def shopping_refresh():
    from spoolmix import shopdb

    try:
        n = shopdb.refresh(_cfg())
    except Exception as exc:
        return _redirect("/shopping", f"Couldn't download SpoolmanDB: {exc}", error=True)
    return _redirect("/shopping", f"Updated: {n:,} filaments available to suggest.")


# --- calibration -----------------------------------------------------------------


@app.get("/calibrate", response_class=HTMLResponse)
def calibrate_page(request: Request, t: str = ""):
    cfg = _cfg()
    tpl = printers.resolve(cfg, t)
    try:
        with open_inventory(cfg) as sm:
            stock = [f for f in sm.filaments() if f.in_stock and f.rgb]
    except Exception:
        stock = []
    runs = [r for r in calibrate.list_runs(cfg.data_dir) if tpl and r.template == tpl.id]
    return _page(request, "calibrate.html", active="calibrate", template=tpl, templates=printers.all_templates(cfg),
                 stock=stock, runs=runs, cal=tpl.calibration() if tpl else None)


@app.post("/template")
@app.post("/calibrate/template")
async def template_upload(file: UploadFile = File(...), next: str = Form("/settings#templates"), name: str = Form("")):
    """Add a printer template (the first one becomes the default)."""
    back = _safe_next(next, "/settings#templates")
    try:
        t = printers.add(_cfg(), await file.read(), name or None)
    except ValueError as exc:
        return _redirect(back, str(exc), error=True)
    msg = f"Template saved as \"{t.name}\": {t.slots} filament slots."
    if not t.info.get("mixing"):
        msg += " Note: it has no color-mixing settings, so print-ready projects won't open with mixes (see below)."
    return _redirect(back, msg)


@app.post("/templates/{tid}/replace")
async def template_replace(tid: str, file: UploadFile = File(...)):
    try:
        t = printers.replace(_cfg(), tid, await file.read())
    except KeyError:
        raise HTTPException(404)
    except ValueError as exc:
        return _redirect(f"/settings#t-{tid}", str(exc), error=True)
    return _redirect(f"/settings#t-{tid}", f"Replaced \"{t.name}\". Re-plan designs that use it to pick up the change.")


@app.post("/templates/{tid}/rename")
def template_rename(tid: str, name: str = Form("")):
    try:
        printers.rename(_cfg(), tid, name)
    except KeyError:
        raise HTTPException(404)
    except ValueError as exc:
        return _redirect(f"/settings#t-{tid}", str(exc), error=True)
    return _redirect(f"/settings#t-{tid}", "Renamed.")


@app.post("/templates/{tid}/default")
def template_default(tid: str):
    try:
        printers.set_default(_cfg(), tid)
    except KeyError:
        raise HTTPException(404)
    return _redirect("/settings#templates", "Default template changed.")


@app.post("/templates/{tid}/delete")
def template_delete(tid: str):
    cfg = _cfg()
    t = printers.get(cfg, tid)
    if t is None:
        raise HTTPException(404)
    printers.delete(cfg, tid)
    return _redirect("/settings#templates", f"Deleted \"{t.name}\" and its calibration.")


@app.post("/calibrate/runs")
async def calibrate_new(request: Request):
    form = await request.form()
    ids = [int(v) for v in (form.get(f"slot{i}") for i in range(1, 5)) if v]
    if len(ids) != len(set(ids)) or not 2 <= len(ids) <= 4:
        return _redirect("/calibrate", "Pick 2–4 different filaments.", error=True)
    cfg = _cfg()
    with open_inventory(cfg) as sm:
        by_id = {f.id: f for f in sm.filaments()}
    fils = [calibrate.CalFilament(i + 1, by_id[fid].key, by_id[fid].label, by_id[fid].hex, by_id[fid].td, by_id[fid].material)
            for i, fid in enumerate(ids)]
    tpl = printers.resolve(cfg, str(form.get("template") or ""))
    if tpl is None:
        return _redirect("/calibrate", "Set up a printer template first.", error=True)
    run = calibrate.new_run(fils, cfg.data_dir, triples=bool(form.get("triples")), notes=str(form.get("notes") or ""),
                            template=tpl.id)
    return _redirect(f"/calibrate/runs/{run.id}", "Calibration grid created.")


@app.get("/calibrate/runs/{run_id}", response_class=HTMLResponse)
def calibrate_run(request: Request, run_id: str):
    cfg = _cfg()
    try:
        run = calibrate.load_run(run_id, cfg.data_dir)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404)
    rows = max(s.row for s in run.swatches) + 1
    grid = [[None] * calibrate.COLS for _ in range(rows)]
    for s in run.swatches:
        grid[s.row][s.col] = s
    return _page(request, "run.html", active="calibrate", run=run, grid=grid,
                 template=printers.get(cfg, run.template), has_template=printers.get(cfg, run.template) is not None)


@app.get("/calibrate/runs/{run_id}/3mf")
def calibrate_run_3mf(run_id: str):
    cfg = _cfg()
    run = calibrate.load_run(run_id, cfg.data_dir)
    tpl = printers.get(cfg, run.template)
    if tpl is None:
        raise HTTPException(400, "this grid's printer template was deleted")
    data = calibrate.write_3mf(run, tpl.path)
    return Response(data, media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
                    headers={"Content-Disposition": f'attachment; filename="spoolmix-calibration-{run.id}.3mf"'})


@app.post("/calibrate/runs/{run_id}/measure")
async def calibrate_measure(request: Request, run_id: str):
    cfg = _cfg()
    run = calibrate.load_run(run_id, cfg.data_dir)
    form = await request.form()
    for s in run.swatches:
        v = str(form.get(f"m_{s.id}") or "").strip()
        if not v:
            s.measured = None
            continue
        try:
            s.measured = to_hex(parse_hex(v))
        except ValueError:
            return _redirect(f"/calibrate/runs/{run_id}", f"{s.id}: '{v}' is not a hex color.", error=True)
    save_run_notes = str(form.get("notes") or run.notes)
    run.notes = save_run_notes
    calibrate.save_run(run, cfg.data_dir)
    return _redirect(f"/calibrate/runs/{run_id}", f"Saved {run.measured_count} measured swatches.")


@app.post("/calibrate/fit")
def calibrate_fit(template: str = Form("")):
    """Fit the calibration of one printer template from the grids printed with it."""
    cfg = _cfg()
    tpl = printers.get(cfg, template)
    if tpl is None:
        raise HTTPException(404)
    back = f"/calibrate?t={tpl.id}"
    runs = [r for r in calibrate.list_runs(cfg.data_dir) if r.measured_count and r.template == tpl.id and r.walls]
    if not runs:
        return _redirect(back, "Measure a block grid first. Older flat grids measured the top surface, which doesn't show how "
                               "layers blend, so they aren't used.", error=True)
    try:
        cal = calibrate.fit(runs)
    except ValueError as exc:
        return _redirect(back, str(exc), error=True)
    calibrate.save_calibration(tpl.dir, cal)
    return _redirect(back, f"Fitted for \"{tpl.name}\": mean ΔE {cal['mean_de_orca']:.2f} → {cal['mean_de_fitted']:.2f}.")


@app.post("/calibrate/reset")
def calibrate_reset(template: str = Form("")):
    tpl = printers.get(_cfg(), template)
    if tpl is None:
        raise HTTPException(404)
    (tpl.dir / calibrate.CALIBRATION_FILE).unlink(missing_ok=True)
    return _redirect(f"/calibrate?t={tpl.id}", "Calibration removed; the calibrated model now uses defaults for this template.")
