import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from spoolmix import calibrate as cal
from spoolmix.color import parse_hex, to_hex
from spoolmix.model import CalibratedModel, TDModel
from spoolmix.solver import Component, Solver

FILS = [
    cal.CalFilament(1, "spoolman:1", "Cyan", "39D0F1", None),
    cal.CalFilament(2, "spoolman:2", "Magenta", "FF00FF", None),
    cal.CalFilament(3, "spoolman:3", "Yellow", "FFFF00", None),
    cal.CalFilament(4, "spoolman:4", "White", "C7CDD7", 7.1),
]


def test_grid_layout():
    sw = cal.design_grid(4)
    assert len(sw) == 4 + 6 * 5 + 4 * 4
    assert sw[0].id == "A1" and sw[8].id == "B1"
    assert all(sum(s.weights) == 100 for s in sw)
    assert all(s.slots == sorted(s.slots) for s in sw)
    assert len(cal.design_grid(2, triples=True)) == 2 + 5


def test_mixed_rows_match_orca_format():
    pair = cal.Swatch("A5", [1, 2], [70, 30])
    assert cal.mixed_row(pair, 1) == "1,2,1,1,30,0,g,w,m2,z0,xa0,xb0,d0,o0,u1,cm0"
    tri = cal.Swatch("F1", [1, 3, 4], [50, 25, 25])
    assert cal.mixed_row(tri, 7) == "1,3,1,1,50,0,g134,w50/25/25,m0,z0,xa0,xb0,d0,o0,u7,cm0"


def _template(tmp_path: Path) -> Path:
    p = tmp_path / "t.3mf"
    settings = {"filament_colour": ["#000000"] * 4, "filament_multi_colors": ["#000000"] * 4,
                "printer_settings_id": "Snapmaker U1 (0.4 nozzle)", "print_settings_id": "0.10mm Color Mixing",
                "mixed_filament_definitions": "1,2,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u1,cm0"}
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        z.writestr("Metadata/slice_info.config", "<config/>")
    return p


def test_write_3mf(tmp_path):
    run = cal.new_run(FILS, tmp_path)
    data = cal.write_3mf(run, _template(tmp_path))
    z = zipfile.ZipFile(__import__("io").BytesIO(data))
    s = json.loads(z.read("Metadata/project_settings.config"))
    assert s["filament_colour"] == ["#39D0F1", "#FF00FF", "#FFFF00", "#C7CDD7"]
    rows = s["mixed_filament_definitions"].split(";")
    assert len(rows) == 46
    cfg = z.read("Metadata/model_settings.config").decode()
    extruders = [int(x) for x in __import__("re").findall(r'key="extruder" value="(\d+)"', cfg)]
    # 4 pure tiles on filaments 1-4, 46 mixes on virtual filaments 5..50, plus the marker on filament 1
    assert sorted(extruders) == sorted([1, 2, 3, 4] + list(range(5, 51)) + [1])
    model = z.read("3D/3dmodel.model").decode()
    assert model.count("<item ") == 51
    assert "3D/Objects/swatches.model" in z.namelist()


def test_run_roundtrip(tmp_path):
    run = cal.new_run(FILS, tmp_path, notes="x")
    run.swatches[0].measured = "#112233"
    cal.save_run(run, tmp_path)
    again = cal.load_run(run.id, tmp_path)
    assert again.swatches[0].measured == "#112233" and again.notes == "x"
    assert [r.id for r in cal.list_runs(tmp_path)] == [run.id]
    with pytest.raises(ValueError):
        cal.load_run("../etc", tmp_path)


def _synthetic(tmp_path, noise=0.0):
    run = cal.new_run(FILS, tmp_path)
    tds = {1: 1.5, 2: 3.0, 3: 8.0, 4: 7.1}
    eff = {f.slot: tuple(int(c * 0.9) for c in parse_hex(f.hex)) for f in FILS}
    m = TDModel(alpha=0.8)
    rng = np.random.default_rng(1)
    for s in run.swatches:
        rgb = np.array(m.mix([eff[x] for x in s.slots], [tds[x] for x in s.slots], s.weights), dtype=float)
        rgb = np.clip(rgb + rng.normal(0, noise, 3), 0, 255)
        s.measured = to_hex(tuple(int(v) for v in rgb))
    return run, tds


def test_fit_recovers_strengths(tmp_path):
    run, tds = _synthetic(tmp_path)
    res = cal.fit([run])
    true = {f"spoolman:{k}": (v ** -0.8) / (tds[1] ** -0.8) for k, v in tds.items()}
    for k, v in true.items():
        assert res["strengths"][k] == pytest.approx(v, rel=0.08)
    assert res["mean_de_fitted"] < 0.5 < res["mean_de_orca"]
    assert res["colors"]["spoolman:1"] == to_hex(tuple(int(c * 0.9) for c in parse_hex("39D0F1")))


def test_fit_with_noise_improves(tmp_path):
    run, _ = _synthetic(tmp_path, noise=3.0)
    res = cal.fit([run])
    assert res["mean_de_fitted"] < res["mean_de_colors_only"] < res["mean_de_orca"]


def test_fit_needs_mixed_measurements(tmp_path):
    run = cal.new_run(FILS, tmp_path)
    with pytest.raises(ValueError):
        cal.fit([run])


def test_calibrated_model_in_solver():
    a = Component("spoolman:1", "A", (0, 195, 255), None)
    b = Component("spoolman:2", "B", (250, 231, 39), None)
    m = CalibratedModel(strengths={"spoolman:1": 1.0, "spoolman:2": 0.25})
    r = Solver(model=m).solve((60, 210, 150), [a, b], top=1)[0]
    w = np.array(r.weights, dtype=float) * np.array([1.0, 0.25])
    from spoolmix.model import mix_sequential_batch
    assert tuple(mix_sequential_batch([p.rgb for p in r.parts], w[None, :] if r.parts[0] is a else w[::-1][None, :])[0]) == r.rgb


def test_new_grids_are_labelled_wall_blocks(tmp_path):
    import re

    run = cal.new_run(FILS[:2], tmp_path)
    assert run.walls and run.tile == 12.0 and run.height == 12.0
    assert cal.load_run(run.id, tmp_path).walls  # survives a save/load
    z = zipfile.ZipFile(__import__("io").BytesIO(cal.write_3mf(run, _template(tmp_path))))
    cfg = z.read("Metadata/model_settings.config").decode()
    labels = re.findall(r'value="label ([A-Z]\d)"', cfg)
    assert labels == [s.id for s in run.swatches]  # every block has its id raised on top
    zs = [float(v) for v in re.findall(r'z="([\d.-]+)"', z.read("3D/Objects/swatches.model").decode())]
    assert max(zs) == pytest.approx(6.0 + cal.LABEL_HEIGHT)  # block centred at 0 (12 mm) + label on top


def test_old_flat_grids_load_as_tiles(tmp_path):
    run = cal.new_run(FILS[:2], tmp_path)
    d = run.to_json()
    d.pop("style")
    assert not cal.CalRun.from_json(d).walls
