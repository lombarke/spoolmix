# How spoolmix works

This page explains what spoolmix computes and why, for anyone who wants to understand (or
check) its suggestions. You don't need any of it to use spoolmix.

- [Color mixing by layers](#color-mixing-by-layers)
- [Predicting a mix](#predicting-a-mix)
- [Stripes (banding)](#stripes-banding)
- [TD: transmission distance](#td-transmission-distance)
- [Planning a design](#planning-a-design)
- [Shopping suggestions](#shopping-suggestions)
- [Material compatibility](#material-compatibility)
- [The print-ready project](#the-print-ready-project)
- [Subdivide mix layers](#subdivide-mix-layers)
- [Calibration](#calibration)
- [Customizing the data](#customizing-the-data)

## Color mixing by layers

A color-mixing printer doesn't blend filament in the nozzle. It alternates very thin layers
(or line segments) of different filaments, and your eye averages them. A 40/60 mix of cyan
and yellow is 40% cyan layers and 60% yellow layers, which reads as green.

That works well when the filaments are **translucent**: light passes through a few layers
and the colors combine. It works badly with **opaque** filaments: each layer shows only its
own color, and you see stripes instead of a blend.

## Predicting a mix

spoolmix predicts the color of a mix exactly the way the slicer's color-mixing preview
does, so what spoolmix shows is what your slicer shows. This was researched from
Snapmaker Orca's source (`filament_mixer_model.h`, `MixedFilament.cpp`,
`MixedColorMatchHelpers.cpp`) and reimplemented independently:

- **Two filaments:** a degree-4 polynomial `lerp(c1, c2, t)` on sRGB 0–255 values, where
  `t` is the second filament's share. The polynomial ([FilamentMixer](https://github.com/justinh-rahb/filament-mixer),
  MIT) was trained to approximate pigment mixing (Mixbox), so blue + yellow gives green,
  not grey. Each output channel is truncated to an integer, as in the slicer.
- **Three filaments:** mixed pairwise **in ascending filament number**: the first two by
  their relative share, then the third by its share of the total. The order changes the
  result, so spoolmix tries every order and tells you which filament goes in which slot.
- **Color difference:** sRGB → CIELAB (D65 white point), then **CIEDE2000 (ΔE)**, the
  standard perceptual color difference. Roughly: below 1 is invisible, 1–2 is very close,
  2–5 is close, above 10 is clearly a different color.
- **Search:** every 1% ratio for pairs; triples on a coarse grid refined to 1% around the
  best. No filament exceeds 70% in a mix by default (the slicer's automatic mode uses the
  same limit).

The slicer's prediction ignores translucency, so spoolmix uses it as-is for the color and
handles translucency separately, as stripes.

## Stripes (banding)

A layer shows its own color to the extent its filament is opaque. spoolmix scores each mix
as the weighted RMS of each filament's color difference from the mixed result, times its
opacity `exp(-TD / 3)`. A filament with unknown TD counts as fairly opaque (TD 2).

| Score | Label |
|---|---|
| below 6 | blends well |
| 6–15 | may band slightly |
| 15 or more | likely visible stripes |

This is why black PLA (TD ≈ 0.3) mixed into a light color is flagged: it's very different
from the mix *and* nearly opaque.

**Lopsided pairs stripe more.** In a 2-filament mix, the minority filament prints as isolated
layers between runs of the majority (80/20 is one layer in five), and those read as lines.
Side-wall test prints (cyan/yellow PLA, 0.1 mm layers) showed it clearly: 80/20 striped far
more than anything from 65/35 to 35/65, and more than 20/80. So when the smaller share drops
below 35%, the score rises with how sparse it is, weighted by how opaque the majority filament
is compared with the minority one (an opaque majority keeps the thin lines crisp). Mixes of
three or more filaments, which the slicer prints as rotating pairs, aren't adjusted: that
hasn't been measured.

## TD: transmission distance

TD (HueForge's convention) is roughly how many millimeters of a filament light can pass
through: low is opaque, high is translucent. Regular black PLA is about 0.1–0.3; filaments
made for color mixing are 5–10.

spoolmix gives every filament a TD automatically, in this order:

1. **Published:** a measured value for that exact filament, from
   - [Snapmaker's](https://github.com/Snapmaker/OrcaSlicer/blob/main/resources/profiles/Snapmaker/filament/filaments_colours.json)
     official Full Spectrum values,
   - [Prusa's](https://help.prusa3d.com/article/hueforge-filament-transparency-values-and-hexcodes_762314)
     published table for Prusament,
   - [filamentcolors.xyz](https://filamentcolors.xyz/) community measurements, for the
     brands you own.

   These are re-checked weekly. A match must be confident (same brand, material family
   and color name, and a close hex) to count.
2. **Estimate:** otherwise, from the most similar measured filaments: same material and
   kind (glow, translucent and regular filaments are never mixed up), nearest color, and
   the same brand when one is close enough. Estimates are labeled and replaced as soon as a
   published value appears.
3. **Yours:** a TD you entered (Filaments & TD, or in Spoolman) is never changed.

If you have a TD meter or run HueForge's test print, enter your value and tick *remember*
to reuse it for other spools of that filament.

The bundled database (`td_database.yaml`) records a source URL and date for every entry.

## Planning a design

When you plan a 3MF:

1. **Analysis.** spoolmix reads every object and part's filament and the per-triangle
   painting, and measures the visible surface area of each designer color (scaling and
   copies included).
2. **Weights.** Each color counts by its share of the surface, with a floor so tiny details
   still matter. Colors you tick as important count five times as much.
3. **Your filaments.** Only filaments of the design's material are used. For each design
   color, the filaments closest to it plus good mixing partners are shortlisted.
4. **Search.** For your slot count, spoolmix searches which filaments to load and in which
   order (every order up to 4 slots; with more, sensible orders plus refinement), and for
   each design color picks a filament as-is when one is within ΔE 3, otherwise the best
   2–3 filament mix.
5. **Visible error.** Plans are ranked by what you'll actually see: color error, plus a
   penalty when a mix is likely to stripe, plus a small cost for mixing at all. A plain
   filament wins unless a mix is clearly closer. A color left far off (beyond ΔE 12) also
   counts extra, whatever its area: small details like eyes are seen, so a plan doesn't give
   up the black for the eyes to gain a little elsewhere.
6. **Your trade-off.** The *Favor* setting (per design, per color match, and a default
   under Settings) changes those penalties:

   | Favor | Mixing cost | Stripe penalty (ΔE per point over 6) | Effect |
   |---|---|---|---|
   | Exact colors | 0.25 | 0.05 | closest colors, even with mixes that may stripe |
   | Balanced | 1.0 | 0.3 | the default |
   | Smooth blends | 2.0 | 0.6 | plain filaments and blending mixes, even if further off |

**Speckled filaments.** Marble, galaxy, sparkle, glitter and similar filaments, plus
wood-fill, don't print as one flat color. You can still use them: spoolmix plans with their
listed (average) color, tags them, and warns wherever one is printed or mixed.

## Shopping suggestions

Candidates come from every single-color 1.75 mm filament in
[SpoolmanDB](https://github.com/Donkie/SpoolmanDB) (about 4,600 after removing duplicate
spool sizes), downloaded weekly, plus your own shopping list. Textured or multicolor spools
(wood, marble, silk, glitter, gradient, glow), incompatible materials and filaments you
already have are skipped. Under **Settings** you can limit suggestions to brands you
already own, or to your own list.

For each design, the nearest candidates to each color are shortlisted and each one (and
each pair) is planned for real. A purchase is suggested only when:

- it lowers the visible error clearly (on average, or for the worst color), and
- it doesn't make a small detail (under 5% of the surface, like eyes) or a color you
  ticked as important noticeably worse. If the best plan with a purchase gives up the
  filament a detail needs, spoolmix tries again keeping it.

Suggestions are limited to one per color family (no list of five near-identical
yellows), and each one says why it helps, e.g. *"fewer stripes: 56% → 7% of the
surface"*. When printing everything with plain filaments is nearly as good, the first
suggestion is the **Simplest** plan, e.g. *"buy a brown and skip color mixing"*, listing
any colors that end up not exact.

## Material compatibility

spoolmix never mixes materials that don't bond, using the slicer's own compatibility table
(`material_compatibility.yaml`):

| Material | Can be mixed with |
|---|---|
| PLA | PLA, PC |
| PETG | PETG, PET, ABS, ASA, PC |
| TPU | TPU, PET (not PETG: no flexible layers in rigid mixes) |
| ABS / ASA | each other, PETG, PET, PC, PA |
| PVA / BVOH (support) | nothing |

Material names map to families: PLA+, Silk PLA, PLA Meta, High Speed PLA and PLA-CF count
as PLA; PCTG counts as PETG. Unrecognized names only mix with the exact same name. All the
filaments loaded for one print are mutually compatible; if that rules out a full set,
spoolmix loads fewer.

## The print-ready project

**Build print-ready project** writes a new `.3mf`:

- the original geometry, objects and plates;
- printer, process and filament presets from your template project, with the process
  settings the designer changed (supports, brim, infill, walls) carried over, while layer
  height, line widths and the prime tower stay as your color-mixing process sets them;
- your filaments in slots 1…N and each mix defined as filament N+1, N+2, …;
- every object, part and painted triangle re-pointed to its new filament.

Painting data is decoded and re-encoded exactly (the 3MF `paint_color` triangle-tree
format), and the painted area per color is preserved; the tests check both. Large projects
work: a 3-million-triangle, 8-color model analyzes in about 8 s and converts in about 20 s
using about 350 MB of memory.

## Subdivide mix layers

The slicer's "Subdivide Mix Layer" option prints each layer of a mixed area as thinner
sub-layers, one per filament, instead of alternating whole layers. Measured with side-wall
test prints (cyan/yellow PLA, 0.1 mm layers, the same blocks printed with it off, on, and on
with "Full domain"):

- **It only applies to painted mixed areas.** Whole objects or parts assigned to a mix are
  only subdivided with "Full domain" on as well.
- **It's very smooth:** no visible layer lines at any ratio.
- **But the ratio barely matters.** Subdivided blocks from 65/35 to 20/80 all came out the
  same green (about two-thirds of the way to yellow), and even 80/20 landed near the middle.
  Without subdivision the same ratios gave a proper ramp that tracked the set ratios. The
  slicer's own color preview shows a milder squeeze than the prints did.
- **It costs time:** about two extra tool changes per layer wherever it applies (the test
  went from 2:54 to 5:15, and 6:09 with Full domain).

So spoolmix never suggests turning it on. Its predictions assume the ratio you set is what
prints, which holds with subdivision off. When a printer template has it on, the template
card, any plan that uses mixes in subdivided areas, and color matches with that template all
warn that those mixes will print close to one in-between color. A test at thicker layers
(0.2 mm leaves more room for sub-layers) might behave differently; that hasn't been measured.

## Calibration

The slicer's preview is a model. To fit predictions to your printer, under **Calibrate**:

1. **New grid:** pick 2–4 filaments in the order you'll load them. The grid has each
   filament pure, every pair at 30/40/50/60/70%, and each triple at equal and dominant
   weights: 7 blocks for 2 filaments, 21 for 3, 50 for 4. Each is a 12 mm block with its
   label (A1, A2, …) raised on top.
2. **Download the 3MF**, slice and print it. Every mix is already set up.
3. **Measure each block's side wall.** Layered mixing shows on walls, where every layer is
   visible; a top surface shows mostly the last few layers, so its color depends on which
   filament happened to print last. Line up one row's blocks in label order on plain white
   paper, photograph the front walls straight on in daylight, then on the grid page pick
   the row, upload the photo and click the paper, the first block and the last block.
   spoolmix reads every block in between, normalizing each photo on the white paper so
   rows shot separately stay comparable. Or type hex values from a colorimeter.
4. **Fit.** spoolmix fits each filament's printed color and a *strength* (how much more or
   less than its layer share it shows; opaque filaments typically show more), and reports
   the mean ΔE of the slicer's preview versus the fitted model. Filaments that weren't on
   a grid fall back to a TD-based strength.

Each printer template has its own calibration: layer height and process settings change how
colors blend, so a fit made with one template doesn't carry over to another. Grids remember
the template they were built from; **Fit** uses only that template's block grids. Older flat
grids (measured from the top) are still listed but not used.

Then choose **Calibrated to my prints** when planning. Recipes are entered in the slicer
the same way; only the predicted colors change.

## Customizing the data

Everything spoolmix saves lives in the `data` folder:

| File | What |
|---|---|
| `settings.json` | where your filaments live, slots, shopping scope |
| `filaments.json` | your filament list, if you don't use Spoolman |
| `td_spoolman.json` | TD values for Spoolman filaments, if spoolmix may not write to Spoolman |
| `td_user.yaml` | TD values you measured (reused for matching filaments) |
| `td_published.yaml` | cached published TD values |
| `spoolmandb.json` | cached SpoolmanDB filament list |
| `templates/<id>/` | each printer template: `template.3mf`, its name and summary, and its calibration |
| `projects/` | imported designs, plans and built projects |
| `calibration/` | calibration grids and measurements |

**Your own shopping list.** spoolmix ships with a small list (Snapmaker's Full Spectrum
filaments, made for color mixing, and common PLA colors). Add your favorites, with store
links and prices, in any file named `data/catalog.<anything>.local.yaml`:

```yaml
- id: my-favorite-teal              # unique; reusing a built-in id replaces that item
  brand: Polymaker
  material: PLA
  color_name: Teal
  hex: "#00807F"                    # required
  td:                               # leave blank to look it up
  url: https://example.com/teal
  price: 22 USD
  # enabled: false                  # hide an item
```

Items from these files appear on the Shopping page and are considered first.
