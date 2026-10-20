"""Formatting of recipes for the terminal and JSON, plus TD / black warnings."""

from __future__ import annotations

import math
import re
from typing import Sequence

from rich.console import Console
from rich.markup import escape

from spoolmix.color import RGB, lightness, swatch, to_hex
from spoolmix.solver import ORCA_AUTO_MAX_PERCENT, PaletteResult, Recipe, ShopOption

OPAQUE_TD = 2.0
DARK_L = 20.0
BANDING_LABEL = {"low": "[green]blends well[/]", "moderate": "[yellow]may band slightly[/]",
                 "high": "[red]likely visible stripes[/]"}
DARK_WARNING = "Target is very dark. Layered mixing can't make true black; print near-black areas in black directly."


def quality(de: float) -> str:
    if de < 1.0:
        return "[green]indistinguishable[/]"
    if de < 2.0:
        return "[green]very close[/]"
    if de < 5.0:
        return "[yellow]close[/]"
    if de < 10.0:
        return "[dark_orange]noticeable difference[/]"
    return "[red]poor[/]"


def banding_warning(recipe: Recipe) -> str | None:
    """Plain-language banding warning for a mix, or None if it should blend."""
    if recipe.size < 2 or recipe.stripe_level == "low":
        return None
    contrast = recipe.stripe_detail[1]
    worst = recipe.parts[max(range(recipe.size), key=lambda i: contrast[i])]
    td = f"TD {worst.td:g}" if worst.td is not None else "TD unknown"
    if recipe.stripe_level == "high":
        return (f"Won't blend well: {worst.label} ({td}) is very different from the mixed color and too opaque "
                "to blend, so its layers will likely show as visible stripes. Closer or more translucent "
                "filaments would blend better.")
    return f"May show faint banding up close: {worst.label} ({td}) stands out from the mixed color."


# Filaments that aren't one flat color. spoolmix plans with their listed (average) color.
_SPECKLED = re.compile(r"marble|galaxy|sparkle|glitter|starlight|granite|confetti|speckle|terrazzo|nebula|\bflakes?\b", re.I)
_WOOD = re.compile(r"\bwood|bamboo|cork", re.I)


def texture(label: str | None) -> str | None:
    """'speckled' or 'wood' for filaments that don't print as one flat color, else None."""
    if not label:
        return None
    if _SPECKLED.search(label):
        return "speckled"
    if _WOOD.search(label):
        return "wood"
    return None


TEXTURE_HELP = {
    "speckled": "has speckles or a marbled/glitter look, so it doesn't print as one flat color. spoolmix plans with its average color",
    "wood": "is a wood-fill filament: its shade changes with print temperature, so its color is only approximate",
}


def texture_warnings(recipe: Recipe) -> list[str]:
    out = []
    for p in recipe.parts:
        kind = texture(p.label)
        if kind is None:
            continue
        if recipe.size == 1:
            out.append(f"{p.label} {TEXTURE_HELP[kind]}; this area will look {kind} rather than solid.")
        else:
            out.append(f"{p.label} {TEXTURE_HELP[kind]}. Mixed in, its {'speckles' if kind == 'speckled' else 'grain'} "
                       "show through and the blend won't look uniform. A solid filament would mix more predictably.")
    return out


def recipe_warnings(recipe: Recipe, target: RGB | None = None, max_pct: int = ORCA_AUTO_MAX_PERCENT) -> list[str]:
    out = texture_warnings(recipe)
    if recipe.size > 1:
        band = banding_warning(recipe)
        if band:
            out.append(band)
        else:
            opaque = [f"{p.label} (TD {p.td:g})" for p in recipe.parts if p.td is not None and p.td < OPAQUE_TD]
            if opaque:
                out.append(f"Nearly opaque: {', '.join(opaque)}. It will dominate more than the slicer preview shows.")
        unknown = [p.label for p in recipe.parts if p.td is None]
        if unknown:
            out.append(f"TD unknown for {', '.join(unknown)}; the real print may differ (set it under Filaments & TD, or `spoolmix td set`).")
        if max(recipe.weights) > ORCA_AUTO_MAX_PERCENT:
            out.append(f"A component exceeds {ORCA_AUTO_MAX_PERCENT}%; slicers may flag this ratio as too high.")
    if target is not None and lightness(target) < DARK_L:
        out.append(DARK_WARNING)
    return out


def _cycle_hint(weights: Sequence[int]) -> str | None:
    if len(weights) != 2:
        return None
    g = math.gcd(*weights)
    a, b = (w // g for w in weights)
    return f"≈ {a}:{b} layer cycle" if a + b <= 10 else None


def recipe_lines(recipe: Recipe, slot_numbers: Sequence[int] | None = None) -> list[str]:
    """Human instructions for entering the recipe in the slicer."""
    nums = list(slot_numbers) if slot_numbers else list(range(1, recipe.size + 1))
    if recipe.size == 1:
        return [f"Use {escape(recipe.parts[0].label)} on its own (no mix)."]
    order = "  →  ".join(f"F{n} {swatch(p.rgb, 2)} {escape(p.label)}{' [magenta](buy)[/]' if not p.owned else ''}"
                         for n, p in zip(nums, recipe.parts))
    ratio = " · ".join(f"F{n} {w}%" for n, w in zip(nums, recipe.weights))
    lines = [
        f"Order matters (lowest filament number first): {order}",
        f"Mix ratio:  {ratio}    [dim](gradient_component_weights {recipe.gradient_weights})[/]",
    ]
    hint = _cycle_hint(recipe.weights)
    if hint:
        lines[-1] += f"  [dim]{hint}[/]"
    return lines


def print_target_header(console: Console, target: RGB, label: str | None = None) -> None:
    extra = f"  {label}" if label else ""
    console.print(f"\n[bold]Target[/] {swatch(target, 6)} {to_hex(target)}{extra}")


def print_recipes(
    console: Console, target: RGB, recipes: Sequence[Recipe], closest_single: Recipe | None, heading: str
) -> None:
    if not recipes:
        console.print("  No mix possible (need at least 2 filaments).")
        return
    best = recipes[0]
    console.print(f"[bold]{heading}: ΔE {best.delta_e:.2f}[/] ({quality(best.delta_e)})")
    for i, r in enumerate(recipes, 1):
        console.print(f"  [bold]{i}.[/] {swatch(r.rgb, 6)} predicted {to_hex(r.rgb)}  ΔE {r.delta_e:.2f}"
                      + (f"  {BANDING_LABEL[r.stripe_level]}" if r.size > 1 else ""))
        for line in recipe_lines(r):
            console.print(f"     {line}")
        for w in recipe_warnings(r):
            console.print(f"     [yellow]⚠ {escape(w)}[/]")
    if closest_single is not None:
        console.print(
            f"  [dim]Closest single filament: {escape(closest_single.parts[0].label)} ΔE {closest_single.delta_e:.2f}[/]"
        )
    if lightness(target) < DARK_L:
        console.print(f"  [yellow]⚠ {DARK_WARNING}[/]")


def print_shop(console: Console, base: Recipe | None, options: Sequence[ShopOption]) -> None:
    base_de = base.delta_e if base else float("nan")
    if not options:
        console.print(f"[bold]Shopping:[/] nothing in the catalog beats your stock (ΔE {base_de:.2f}).")
        return
    first = options[0]
    buy_text = " + ".join(escape(c.label) for c in first.buy)
    console.print(
        f"\n[bold]Best with your stock: ΔE {base_de:.2f}. Add {buy_text} → ΔE {first.recipe.delta_e:.2f}[/]"
    )
    for i, o in enumerate(options, 1):
        items = " + ".join(f"{swatch(c.rgb, 2)} {escape(c.label)}" for c in o.buy)
        pairs_with = [escape(p.label) for p in o.recipe.parts if p.owned]
        console.print(
            f"  [bold]{i}.[/] Buy {items}: ΔE {o.recipe.delta_e:.2f} "
            f"([green]−{o.improvement:.2f}[/])" + (f", pairs with {', '.join(pairs_with)}" if pairs_with else "")
        )
        for c in o.buy:
            ref = c.ref
            meta = [x for x in (getattr(ref, "bundle", None), getattr(ref, "price", None), getattr(ref, "url", None)) if x]
            td = f"TD {c.td:g}" if c.td is not None else "TD unknown"
            console.print(f"     [dim]{escape(c.label)}: {td}{' · ' + ' · '.join(meta) if meta else ''}[/]")
        console.print(f"     {swatch(o.recipe.rgb, 6)} predicted {to_hex(o.recipe.rgb)}")
        for line in recipe_lines(o.recipe):
            console.print(f"     {line}")
        for w in recipe_warnings(o.recipe):
            console.print(f"     [yellow]⚠ {escape(w)}[/]")


def print_palette(console: Console, targets: Sequence[RGB], result: PaletteResult) -> None:
    slots = "   ".join(f"T{i} {swatch(c.rgb, 2)} {escape(c.label)}" for i, c in enumerate(result.slots, 1))
    console.print(f"\n[bold]4-slot palette[/] (load in this order as filaments F1–F{len(result.slots)}):")
    console.print(f"  {slots}")
    console.print(f"  worst ΔE {result.worst:.2f}, mean ΔE {result.total / len(targets):.2f}")
    index = {c.key: i + 1 for i, c in enumerate(result.slots)}
    for t, r in zip(targets, result.recipes):
        print_target_header(console, t)
        console.print(f"  {swatch(r.rgb, 6)} predicted {to_hex(r.rgb)}  ΔE {r.delta_e:.2f} ({quality(r.delta_e)})")
        for line in recipe_lines(r, [index[p.key] for p in r.parts]):
            console.print(f"     {line}")
        for w in recipe_warnings(r, t):
            console.print(f"     [yellow]⚠ {escape(w)}[/]")


def recipe_json(r: Recipe, slot_numbers: Sequence[int] | None = None) -> dict:
    nums = list(slot_numbers) if slot_numbers else list(range(1, r.size + 1))
    return {
        "predicted": to_hex(r.rgb),
        "delta_e": round(r.delta_e, 3),
        "components": [
            {"slot": n, "key": p.key, "label": p.label, "hex": to_hex(p.rgb), "td": p.td, "owned": p.owned, "percent": w}
            for n, p, w in zip(nums, r.parts, r.weights)
        ],
        "gradient_component_weights": r.gradient_weights,
        "warnings": recipe_warnings(r),
    }
