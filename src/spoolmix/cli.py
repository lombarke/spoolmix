"""spoolmix command-line interface."""

from __future__ import annotations

import datetime as dt

import typer
from rich.console import Console
from rich.table import Table

import json
from pathlib import Path

from spoolmix import report
from spoolmix.color import RGB, parse_hex, swatch, to_hex
from spoolmix.config import load_config
from spoolmix.model import get_model
from spoolmix.solver import ORCA_AUTO_MAX_PERCENT, Component, Solver, choose_palette, shop
from spoolmix.spoolman import Filament
from spoolmix.tddb import TDDatabase, TDMatch

app = typer.Typer(help="Snapmaker Full Spectrum mix recommender backed by Spoolman.", no_args_is_help=True)
fields_app = typer.Typer(help="Manage the Spoolman extra fields spoolmix uses.", no_args_is_help=True)
app.add_typer(fields_app, name="fields")
td_app = typer.Typer(help="Look up, set and record filament TD (transmission distance).", no_args_is_help=True)
app.add_typer(td_app, name="td")
catalog_app = typer.Typer(help="Inspect and extend the purchase catalog.", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")

console = Console(highlight=False)


def _client():
    """Your filaments: Spoolman or spoolmix's own list, whichever the web UI's Settings chose."""
    from spoolmix.inventory import NotConfigured, open_inventory

    try:
        return open_inventory(load_config())
    except NotConfigured:
        console.print("[red]No filament source yet: set SPOOLMAN_URL, or choose one in the web UI's Settings.[/]")
        raise typer.Exit(2)


@app.callback()
def main() -> None:
    """Snapmaker Full Spectrum mix recommender backed by Spoolman."""


@app.command()
def version() -> None:
    """Print the spoolmix version."""
    from spoolmix import __version__

    typer.echo(__version__)


@app.command()
def inventory(all_filaments: bool = typer.Option(False, "--all", help="Include filaments with no usable spool.")) -> None:
    """List filaments in stock with color, TD and remaining weight."""
    with _client() as sm:
        filaments = sm.filaments()
        missing = sm.missing_fields()
    table = Table(title="Spoolman inventory")
    for col in ("ID", "", "Filament", "Hex", "TD", "TD source", "Remaining", "Spools"):
        table.add_column(col)
    for f in filaments:
        if not (all_filaments or f.in_stock):
            continue
        table.add_row(
            str(f.id),
            swatch(f.rgb) if f.rgb else "?",
            f.label,
            f"#{f.hex.upper()}" if f.hex else "—",
            f"{f.td:g}" if f.td is not None else "[yellow]unknown[/]",
            f.td_source or "",
            f"{f.remaining_weight:.0f} g",
            str(sum(1 for s in f.spools if s.usable)),
        )
    console.print(table)
    if missing:
        console.print(
            f"[yellow]Spoolman is missing extra field(s) {', '.join(missing)}; "
            "run `spoolmix fields ensure` to create them.[/]"
        )


@fields_app.command("ensure")
def fields_ensure(yes: bool = typer.Option(False, "--yes", "-y", help="Create without prompting.")) -> None:
    """Create the `td` and `td_source` filament extra fields in Spoolman (asks first)."""
    with _client() as sm:
        missing = sm.missing_fields()
        if not missing:
            console.print("All spoolmix extra fields already exist in Spoolman.")
            return
        console.print(f"Spoolman at {sm.base_url} is missing these filament extra fields:")
        for key, definition in missing.items():
            unit = f", unit {definition['unit']}" if definition.get("unit") else ""
            console.print(f"  • [bold]{key}[/] ({definition['field_type']}{unit}): {definition['name']}")
        if not yes and not typer.confirm("Create them now?", default=False):
            raise typer.Exit(1)
        for key, definition in missing.items():
            sm.create_field(key, definition)
            console.print(f"Created [bold]{key}[/].")



# --- match --------------------------------------------------------------------


def _stock_components(sm) -> list[Component]:
    from spoolmix.service import is_multicolor

    comps = []
    for f in sm.in_stock_filaments():
        if f.rgb is None or is_multicolor(f):
            continue
        comps.append(Component(key=f.key, label=f.label, rgb=f.rgb, td=f.td, owned=True, ref=f,
                               material=f.material))
    return comps


def _collect_targets(colors: list[str], image: Path | None, pixels: list[str], k: int | None) -> list[tuple[RGB, str]]:
    targets: list[tuple[RGB, str]] = []
    for c in colors:
        try:
            targets.append((parse_hex(c), ""))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if image is not None:
        from spoolmix.imagepick import dominant_colors, pick_pixel

        for p in pixels:
            x, y = (int(v) for v in p.split(","))
            targets.append((pick_pixel(image, x, y), f"pixel {x},{y}"))
        if k or not pixels:
            for rgb, share in dominant_colors(image, k or 5):
                targets.append((rgb, f"{share:.0%} of image"))
    if not targets:
        raise typer.BadParameter("give a hex color (e.g. '#1F4D2B') or --image")
    return targets


@app.command()
def match(
    colors: list[str] = typer.Argument(None, help="Target hex color(s), e.g. '#1F4D2B'."),
    shop_mode: bool = typer.Option(False, "--shop", help="Also suggest 1–2 spools to buy from the catalog."),
    max_buy: int = typer.Option(2, "--max-buy", min=1, max=2, help="Max spools to suggest buying together."),
    image: Path = typer.Option(None, "--image", exists=True, dir_okay=False, help="Pick targets from an image."),
    pixel: list[str] = typer.Option([], "--pixel", help="With --image: x,y pixel to sample (repeatable)."),
    k: int = typer.Option(None, "--colors", "-k", min=1, max=12, help="With --image: number of dominant colors."),
    model_name: str = typer.Option("orca", "--model", help="'orca' (matches the slicer preview), 'calibrated' (fitted to your printed swatches) or 'td' (TD-weighted, uncalibrated)."),
    alpha: float = typer.Option(None, "--alpha", help="TD model exponent (default: calibrated value or 0.5)."),
    min_pct: int = typer.Option(0, "--min", min=0, max=50, help="Minimum percent per component."),
    max_pct: int = typer.Option(ORCA_AUTO_MAX_PERCENT, "--max", min=50, max=100,
                                help="Maximum percent per component (Orca auto mode uses 70)."),
    no_triples: bool = typer.Option(False, "--pairs-only", help="Only 2-filament mixes."),
    top: int = typer.Option(3, "--top", min=1, max=20, help="Recipes to show per target."),
    slots: int = typer.Option(4, "--slots", min=2, max=4, help="Toolheads available (multi-target mode)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    material: str = typer.Option("any", "--material", help="Only this material category (e.g. PLA, PETG). "
                                 "Mixes are always material-compatible."),
) -> None:
    """Find the best Full Spectrum mix for target color(s) from your Spoolman stock."""
    targets = _collect_targets(colors or [], image, pixel, k)
    cfg = load_config()
    cal_colors: dict = {}
    calibrated = False
    if model_name == "calibrated":
        from spoolmix import service

        solver, info = service.build_solver(cfg, service.MatchOptions(
            model="calibrated", min_pct=min_pct, max_pct=max_pct, pairs_only=no_triples))
        model, calibrated, cal_colors = solver.model, info["calibrated"], info["cal_colors"]
    else:
        model = get_model(model_name, alpha=alpha)
        solver = Solver(model=model, min_pct=min_pct, max_pct=max_pct, allow_triples=not no_triples)

    with _client() as sm:
        stock = _stock_components(sm)
    if cal_colors:
        from spoolmix.service import apply_calibrated_colors

        stock = apply_calibrated_colors(stock, cal_colors)
    if material != "any":
        from spoolmix.design import filter_materials

        stock, _ = filter_materials(stock, {material.upper()})
    if len(stock) < 2:
        console.print("[red]Need at least two in-stock filaments with a color in Spoolman.[/]")
        raise typer.Exit(1)

    catalog_comps: list[Component] = []
    if shop_mode:
        from spoolmix.catalog import load_catalog

        from spoolmix import shopdb
        from spoolmix.materials import category

        cats = {material.upper()} if material != "any" else {category(c.material) for c in stock} - {None}
        catalog_comps = shopdb.candidates(cfg, [t for t, _ in targets], cats, stock)

    if not as_json:
        note = ""
        if model.name == "td":
            note = f" (alpha {model.alpha:g}, uncalibrated)"
        elif model.name == "calibrated":
            note = "" if calibrated else " (no calibration yet: using defaults)"
        console.print(f"[dim]{len(stock)} filaments in stock · model {model.name}{note} · max {max_pct}% per component[/]")

    out: dict = {"model": model.name, "targets": []}
    if len(targets) > 1:
        result = choose_palette(solver, [t for t, _ in targets], stock, slots=slots)
        if as_json:
            index = {c.key: i + 1 for i, c in enumerate(result.slots)}
            out["palette"] = [{"slot": i + 1, "key": c.key, "label": c.label} for i, c in enumerate(result.slots)]
            for (t, label), r in zip(targets, result.recipes):
                out["targets"].append({"target": to_hex(t), "label": label,
                                       "recipe": report.recipe_json(r, [index[p.key] for p in r.parts])})
        else:
            report.print_palette(console, [t for t, _ in targets], result)
    for t, label in targets if len(targets) == 1 or shop_mode else []:
        entry: dict = {"target": to_hex(t), "label": label}
        recipes = solver.solve(t, stock, top=top) if len(targets) == 1 else []
        single = solver.singles(t, stock)[0]
        if not as_json:
            report.print_target_header(console, t, label)
            if recipes:
                report.print_recipes(console, t, recipes, single, "Best with your stock")
        entry["recipes"] = [report.recipe_json(r) for r in recipes]
        entry["closest_single"] = report.recipe_json(single)
        if shop_mode:
            base, options = shop(solver, t, stock, catalog_comps, max_buy=max_buy, top=top)
            if not as_json:
                report.print_shop(console, base, options)
            entry["shop"] = [
                {"buy": [c.label for c in o.buy], "improvement": round(o.improvement, 3),
                 "recipe": report.recipe_json(o.recipe)} for o in options
            ]
        if len(targets) == 1:
            out["targets"].append(entry)
        else:
            out.setdefault("shop", []).append(entry)
    if as_json:
        typer.echo(json.dumps(out, indent=2))


# --- catalog ------------------------------------------------------------------


@catalog_app.command("list")
def catalog_list() -> None:
    """Show purchase catalog items with their TD (and where it came from)."""
    from spoolmix.catalog import load_catalog

    cfg = load_config()
    items = load_catalog(cfg.catalog, _td_db(), cfg.data_dir)
    table = Table(title=f"Purchase catalog ({len(items)} items)")
    for col in ("ID", "", "Filament", "Hex", "TD", "TD from", "Bundle / notes"):
        table.add_column(col)
    for i in items:
        table.add_row(i.id, swatch(i.rgb), i.label, i.hex.upper(),
                      f"{i.td:g}" if i.td is not None else "[yellow]unknown[/]", i.td_source or "",
                      i.bundle or i.notes or "")
    console.print(table)


@catalog_app.command("import-spoolmandb")
def catalog_import_spoolmandb(
    manufacturer: list[str] = typer.Option([], "--manufacturer", "-m", help="Filter (repeatable), e.g. Sunlu."),
    material: list[str] = typer.Option(["PLA", "PLA+"], "--material", help="Filter (repeatable)."),
    translucent_only: bool = typer.Option(False, "--translucent-only", help="Only filaments SpoolmanDB marks translucent."),
    name: str = typer.Option("spoolmandb", "--name", help="Writes data/catalog.<name>.local.yaml."),
) -> None:
    """Pull candidate filaments from SpoolmanDB into a local (gitignored) catalog file."""
    from spoolmix.catalog import import_spoolmandb

    cfg = load_config()
    out = cfg.data_dir / f"catalog.{name}.local.yaml"
    n = import_spoolmandb(out, manufacturer, material, translucent_only)
    console.print(f"Wrote {n} items to {out}. They're merged into --shop automatically; delete the file to remove them.")
    if n > 300:
        console.print("[yellow]That's a lot of candidates; --shop gets slower. Narrow with --manufacturer.[/]")


# --- td -----------------------------------------------------------------------

LOOKUP_PREFIX = "lookup:"

MEASURE_HELP = """\
[bold]How to measure TD yourself[/]
  • [bold]TD-1 / TD1S meter[/] (AJAX-3D): feed the filament through the sensor and read TD directly.
    Most precise option (about ±7.5% per the vendor).
  • [bold]HueForge Seashell test[/] (free; needs HueForge): open [italic]Seashell_Test.hfp[/] from HueForge's
    Tools folder, export the STL, slice at 0.08 mm layers with black first and your filament as the
    single swap. Print it, then in HueForge adjust that filament's TD until the on-screen preview
    matches the print. If you're unsure, err toward the lower TD.
  Record the result:
    spoolmix td set <filament-id> <TD> --method td1|seashell
    spoolmix td add --filament <filament-id> --td <TD> --method td1|seashell   # reuse in future lookups
"""


def parse_hex_safe(value: str | None):
    try:
        return parse_hex(value) if value else None
    except ValueError:
        return None


def _td_db() -> TDDatabase:
    cfg = load_config()
    return TDDatabase.load(cfg.td_database, cfg.td_user)


def db_path_label() -> str:
    return str(load_config().td_user)


def _td_owner(f: Filament) -> str:
    from spoolmix.service import td_owner

    return td_owner(f)


def _parse_selection(answer: str, rows: list[tuple[int, Filament, TDMatch]]) -> list[tuple[int, Filament, TDMatch]]:
    answer = answer.strip().lower()
    if answer in ("", "none", "n", "no"):
        return []
    if answer == "all":
        return rows
    if answer == "high":
        return [r for r in rows if getattr(r[2], "kind", getattr(r[2], "level", None)) == "high"]
    picked = {int(x) for x in answer.replace(" ", "").split(",") if x}
    return [r for r in rows if r[0] in picked]


@td_app.command("lookup")
def td_lookup(
    all_filaments: bool = typer.Option(False, "--all", help="Include filaments with no usable spool."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-propose values previously written by td lookup."),
    apply: str = typer.Option(None, "--apply", help="Skip the prompt: 'high', 'all', or row numbers like '1,3'."),
) -> None:
    """Propose TD values for Spoolman filaments from td_database.yaml, then ask before writing."""
    db = _td_db()
    with _client() as sm:
        if sm.missing_fields():
            console.print("[red]Spoolman lacks the td/td_source fields; run `spoolmix fields ensure` first.[/]")
            raise typer.Exit(1)
        filaments = [f for f in sm.filaments() if all_filaments or f.in_stock]

        from spoolmix.service import propose_td

        table = Table(title="Proposed TD values")
        for col in ("#", "", "Filament", "Current", "Proposed", "Based on", "Kind"):
            table.add_column(col)
        proposals = []
        unmatched: list[Filament] = []
        for f in filaments:
            owner = _td_owner(f)
            sw = swatch(f.rgb) if f.rgb else "?"
            cur = "—" if f.td is None else f"{f.td:g}"
            if owner == "user":
                table.add_row("", sw, f.label, cur, "—", "[dim]kept: entered by you[/]", "")
                continue
            if owner == "lookup" and not refresh:
                table.add_row("", sw, f.label, cur, "—", "[dim]already set by lookup (--refresh)[/]", "")
                continue
            p = propose_td(db, f)
            if p is None:
                if f.td is None:
                    unmatched.append(f)
                table.add_row("", sw, f.label, cur, "[dim]—[/]", "[dim]nothing new[/]" if f.td is not None else "[yellow]nothing to go on[/]", "")
                continue
            idx = len(proposals) + 1
            proposals.append((idx, f, p))
            if p.match:
                e = p.match.entry
                basis = f"{swatch(parse_hex_safe(e.hex), 2) if e.hex else ''} {e.label} ({e.source})"
                kind = f"[{'green' if p.kind == 'high' else 'yellow'}]{p.match.confidence:.2f} {p.kind}[/]"
            else:
                basis = "similar: " + p.estimate.basis
                kind = "[cyan]estimate[/]"
            table.add_row(str(idx), sw, f.label, cur, f"[bold]{p.td:g}[/]", basis, kind)
        console.print(table)

        if unmatched:
            console.print("\n[yellow]Nothing to go on for:[/] " + ", ".join(f"{f.id} {f.label}" for f in unmatched))
            console.print(MEASURE_HELP)

        if not proposals:
            return
        answer = apply
        if answer is None:
            answer = typer.prompt(
                "Write which proposals to Spoolman? [high / all / row numbers e.g. 1,3 / none]", default="none"
            )
        chosen = _parse_selection(answer, proposals)
        for _, f, p in chosen:
            sm.set_filament_td(f.id, p.td, p.source)
            console.print(f"  set {f.label}: TD {p.td:g}  ({p.kind})")
        if not chosen:
            console.print("Nothing written.")


@td_app.command("sync")
def td_sync(refresh: bool = typer.Option(False, "--refresh", help="Re-fetch published sources now.")) -> None:
    """Fill in every filament's TD automatically: published where available, else an estimate."""
    from spoolmix.service import auto_td

    res = auto_td(load_config(), refresh=True if refresh else None, progress=lambda m: console.print(f"[dim]{m}[/]"))
    for w in res["written"]:
        console.print(f"  {w['label']}: TD {w['td']:g} ({'estimate' if w['kind'] == 'estimate' else 'published'})")
    for m in res.get("missing", []):
        console.print(f"  [yellow]{m['label']}: no TD ({m['reason']})[/]")
    if res.get("fetch_error"):
        console.print(f"[yellow]Couldn't fetch all sources: {res['fetch_error']}[/]")
    if not res["written"]:
        console.print("All TDs up to date.")


@td_app.command("set")
def td_set(
    filament_id: int = typer.Argument(..., help="Spoolman filament ID (see `spoolmix inventory`)."),
    td: float = typer.Argument(..., min=0.0, help="Measured TD."),
    method: str = typer.Option("other", "--method", "-m", help="td1, seashell or other."),
    add_to_db: bool = typer.Option(False, "--add-to-db", help="Also record it in your TD measurements file (data/td_user.yaml)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask before overwriting an existing TD."),
) -> None:
    """Write a TD you measured to a Spoolman filament (td_source = measured:<method>:<date>)."""
    with _client() as sm:
        f = next((x for x in sm.filaments() if x.id == filament_id), None)
        if f is None:
            console.print(f"[red]No filament with ID {filament_id}.[/]")
            raise typer.Exit(1)
        if f.td is not None and not yes:
            if not typer.confirm(f"{f.label} already has TD {f.td:g} ({f.td_source or 'no source'}). Overwrite?"):
                raise typer.Exit(1)
        source = f"measured:{method}:{dt.date.today().isoformat()}"
        sm.set_filament_td(f.id, td, source)
        console.print(f"Set {f.label}: TD {td:g} ({source})")
    if add_to_db:
        _add_entry_from_filament(f, td, method, None)


def _add_entry_from_filament(f: Filament, td: float, method: str, notes: str | None) -> None:
    entry = _td_db().add_user_entry(
        brand=f.vendor or "Unknown", material=f.material or "Unknown", color_name=f.name,
        td=td, hex_color=f.hex, method=method, notes=notes,
    )
    console.print(f"Added to {db_path_label()}: {entry.label} TD {entry.td:g} ({entry.method})")


@td_app.command("add")
def td_add(
    td: float = typer.Option(..., "--td", min=0.0, help="Measured TD."),
    filament: int = typer.Option(None, "--filament", "-f", help="Take brand/material/color/hex from this Spoolman filament."),
    brand: str = typer.Option(None, "--brand"),
    material: str = typer.Option(None, "--material"),
    color: str = typer.Option(None, "--color", help="Color name as the manufacturer calls it."),
    hex_color: str = typer.Option(None, "--hex"),
    method: str = typer.Option("other", "--method", "-m", help="td1, seashell or other."),
    notes: str = typer.Option(None, "--notes"),
) -> None:
    """Record your own TD measurement (data/td_user.yaml) so lookups reuse it."""
    if filament is not None:
        with _client() as sm:
            f = next((x for x in sm.filaments() if x.id == filament), None)
        if f is None:
            console.print(f"[red]No filament with ID {filament}.[/]")
            raise typer.Exit(1)
        f = Filament(id=f.id, name=color or f.name, vendor=brand or f.vendor, material=material or f.material,
                     hex=hex_color or f.hex)
        _add_entry_from_filament(f, td, method, notes)
        return
    if not (brand and material and color):
        console.print("[red]Give --filament ID, or all of --brand, --material and --color.[/]")
        raise typer.Exit(2)
    entry = _td_db().add_user_entry(brand, material, color, td, hex_color, method, notes)
    console.print(f"Added to {db_path_label()}: {entry.label} TD {entry.td:g} ({entry.method})")


if __name__ == "__main__":
    app()
