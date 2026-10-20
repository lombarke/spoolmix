import json
import zipfile
from pathlib import Path

import pytest

from spoolmix import paint
from spoolmix.project3mf import Assignment, analyze, convert, surface_by_filament

# --- paint codec ---------------------------------------------------------------


@pytest.mark.parametrize("s, state", [("4", 1), ("8", 2), ("0C", 3), ("1C", 4), ("2C", 5), ("EC", 17), ("0FC", 18)])
def test_leaf_states(s, state):
    assert paint.state_areas(s) == {state: 1.0}
    assert paint.encode(paint.decode(s)) == s


def test_split_tree_areas_and_roundtrip():
    root = paint._Node(3, children=[paint._Node(0, state=1), paint._Node(0, state=2),
                                    paint._Node(0, state=5), paint._Node(0, state=1)])
    s = paint.encode(root)
    assert paint.encode(paint.decode(s)) == s
    assert paint.state_areas(s) == {1: 0.5, 2: 0.25, 5: 0.25}


def test_remap_changes_only_mapped_states():
    root = paint._Node(1, children=[paint._Node(0, state=0), paint._Node(0, state=7)])
    s = paint.encode(root)
    out = paint.remap(s, {7: 2, 3: 9})
    assert paint.state_areas(out) == {0: 0.5, 2: 0.5}
    assert paint.remap(paint.remap(s, {7: 40}), {40: 7}) == s  # multi-nibble states round-trip


# --- synthetic Bambu-style project -------------------------------------------------

_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" '
         'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
         'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">\n')


def _project(path: Path) -> Path:
    """Two placed objects: A (painted square, object filament 1) and B (plain square on filament 3, scaled 2x)."""
    sq = ('<vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="10" y="10" z="0"/>'
          '<vertex x="0" y="10" z="0"/></vertices>')
    painted = paint.encode(paint._Node(1, children=[paint._Node(0, state=2), paint._Node(0, state=4)]))
    objects = (_HEAD + "<resources>\n"
               f'<object id="1" type="model"><mesh>{sq}<triangles>'
               f'<triangle v1="0" v2="1" v3="2" paint_color="{painted}"/>'
               '<triangle v1="0" v2="2" v3="3"/></triangles></mesh></object>\n'
               f'<object id="3" type="model"><mesh>{sq}<triangles>'
               '<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="2" v3="3" paint_color="8"/></triangles></mesh></object>\n'
               "</resources><build/></model>\n")
    main = (_HEAD + "<resources>\n"
            '<object id="2" type="model"><components><component p:path="/3D/Objects/o.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>\n'
            '<object id="4" type="model"><components><component p:path="/3D/Objects/o.model" objectid="3" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>\n'
            '</resources><build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 50 50 0"/>'
            '<item objectid="4" transform="2 0 0 0 2 0 0 0 1 100 50 0"/></build></model>\n')
    ms = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
          '<object id="2"><metadata key="name" value="A"/><metadata key="extruder" value="1"/>'
          '<part id="1" subtype="normal_part"><metadata key="name" value="A"/></part></object>\n'
          '<object id="4"><metadata key="name" value="B"/><metadata key="extruder" value="1"/>'
          '<part id="3" subtype="normal_part"><metadata key="extruder" value="3"/></part></object>\n'
          '<plate><metadata key="filament_maps" value="1 1 1 1 1"/></plate>\n</config>\n')
    settings = {"filament_colour": ["#FF0000FF", "#00FF00FF", "#0000FFFF", "#FFFF00FF", "#FFFFFFFF"],
                "filament_type": ["PLA"] * 5, "filament_settings_id": ["Generic PLA"] * 5,
                "printer_settings_id": "Bambu Lab X1C", "print_settings_id": "0.20mm Standard",
                "enable_support": "1", "support_type": "tree(auto)", "layer_height": "0.2",
                "different_settings_to_system": ["enable_support;support_type;layer_height", "", ""]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/Objects/o.model", objects)
        z.writestr("Metadata/model_settings.config", ms)
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        z.writestr("Metadata/filament_sequence.json", "{}")
    return path


def _template(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(
            {"filament_colour": ["#000000"] * 4, "print_settings_id": "0.10mm Color Mixing @Snapmaker U1 (0.4 nozzle)",
             "printer_settings_id": "Snapmaker U1 (0.4 nozzle)"}))
        z.writestr("Metadata/slice_info.config", "<config/>")
    return path


def test_analyze_areas(tmp_path):
    a = analyze(_project(tmp_path / "p.3mf"))
    area = {f.id: round(f.area, 3) for f in a.filaments}
    # A: painted triangle (50 mm²) split half filament 2, half filament 4; plain triangle -> filament 1.
    # B: scaled 2x2 => triangles of 200 mm²: plain -> part filament 3; painted "8" -> filament 2.
    assert area == {1: 50.0, 2: 225.0, 3: 200.0, 4: 25.0, 5: 0.0}
    assert a.painted_triangles == 2 and a.total_triangles == 4
    assert [f.hex for f in a.filaments][:2] == ["#FF0000", "#00FF00"]


def test_convert_remaps_everything(tmp_path):
    src = _project(tmp_path / "p.3mf")
    out = tmp_path / "out.3mf"
    targets = {1: 2, 2: 5, 3: 1, 4: 6, 5: 1}
    asg = Assignment([("#111111", "a"), ("#222222", "b"), ("#333333", "c"), ("#444444", "d")], targets,
                     ["1,2,1,1,40,0,g,w,m2,z0,xa0,xb0,d0,o0,u1,cm0", "2,3,1,1,30,0,g,w,m2,z0,xa0,xb0,d0,o0,u2,cm0"])
    convert(src, _template(tmp_path / "t.3mf"), asg, out)
    got = {k: round(v, 3) for k, v in surface_by_filament(out).items()}
    assert got == {2: 50.0, 5: 225.0, 1: 200.0, 6: 25.0}
    z = zipfile.ZipFile(out)
    s = json.loads(z.read("Metadata/project_settings.config"))
    assert s["filament_colour"] == ["#111111", "#222222", "#333333", "#444444"]
    assert s["print_settings_id"].startswith("0.10mm Color Mixing")
    # designer's supports carried over; layer height stays the Color Mixing template's
    assert s["enable_support"] == "1" and s["support_type"] == "tree(auto)"
    assert "layer_height" not in s or s["layer_height"] != "0.2"
    assert "enable_support" in s["different_settings_to_system"][0]
    assert s["mixed_filament_definitions"].count(";") == 1
    ms = z.read("Metadata/model_settings.config").decode()
    assert 'key="filament_maps" value="1 1 1 1"' in ms
    assert "Metadata/filament_sequence.json" not in z.namelist()
    assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist() if i.filename.endswith(".model"))


def test_convert_refuses_slot_mismatch_with_clear_message(tmp_path):
    src = _project(tmp_path / "p.3mf")
    asg = Assignment([("#111111", "a")] * 6, {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}, [])
    with pytest.raises(ValueError, match="template project has 4 filament slots, but this plan uses 6"):
        convert(src, _template(tmp_path / "t.3mf"), asg, tmp_path / "o.3mf")
