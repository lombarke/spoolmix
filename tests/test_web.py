import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from spoolmix import inventory, service, shopdb
from spoolmix.config import load_config
from spoolmix.spoolman import Filament, Spool
from spoolmix.web import app as webapp


def _fil(i, name, hexc, td=None, src=None):
    f = Filament(id=i, name=name, vendor="Sunlu", material="PLA", hex=hexc, td=td, td_source=src)
    f.spools.append(Spool(i, i, 900.0, False, None))
    return f


class FakeSpoolman:
    writes: list = []
    created: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def close(self):
        pass

    def info(self):
        return {"version": "0.27.0"}

    def filaments(self):
        return [_fil(1, "Cyan", "00C3FF"), _fil(2, "Magenta", "F54399"), _fil(3, "Yellow", "FAE727"),
                _fil(4, "White", "C7CDD7", 4.0, "measured:td1:2026-10-16"), _fil(6, "Red", "AC3638")]

    def missing_fields(self):
        return {}

    def set_filament_td(self, fid, td, source):
        FakeSpoolman.writes.append((fid, td, source))

    def create_field(self, key, d):
        FakeSpoolman.created.append(key)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOOLMIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPOOLMAN_URL", "http://spoolman.test:7912")
    monkeypatch.setattr(inventory, "SpoolmanClient", FakeSpoolman)
    FakeSpoolman.writes = []
    return TestClient(webapp.app)


@pytest.mark.parametrize("path", ["/", "/filaments", "/catalog", "/calibrate", "/healthz"])
def test_pages(client, path):
    assert client.get(path).status_code == 200


def test_match_fragment(client):
    r = client.post("/match", data={"targets": "#5AB49B\n#E07AB0 pink", "model": "orca", "top": "2"})
    assert r.status_code == 200
    assert "Load these 4 filaments" in r.text or "Load these" in r.text and "Mix ratio:" in r.text


def test_match_bad_hex(client):
    r = client.post("/match", data={"targets": "#XYZ"})
    assert "Not a hex color" in r.text


def test_match_shop(client):
    r = client.post("/match", data={"targets": "#264E9E", "shop": "true", "max_buy": "1", "top": "1"})
    assert r.status_code == 200 and "Shopping" in r.text


def test_auto_td_fills_published_and_estimates_never_user(client, monkeypatch):
    monkeypatch.setattr(service, "refresh_published", lambda *a, **k: 0)  # no network in tests
    assert client.get("/filaments").status_code == 200
    by_id = {w[0]: w for w in FakeSpoolman.writes}
    assert 4 not in by_id  # White has a user-measured TD: never touched
    assert by_id[6][1] == 5.7 and by_id[6][2].startswith("lookup:")  # Red: published value
    assert by_id[1][2].startswith("estimate:")  # Cyan: no published value -> labeled estimate


def test_td_set_and_remember(client, tmp_path):
    r = client.post("/td/set", data={"filament_id": "1", "td": "3.2", "method": "td1", "add_to_db": "true"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert FakeSpoolman.writes[0][:2] == (1, 3.2) and FakeSpoolman.writes[0][2].startswith("measured:td1:")
    assert "Cyan" in (tmp_path / "td_user.yaml").read_text()


def test_image_colors(client):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (31, 77, 43)).save(buf, "PNG")
    r = client.post("/api/image-colors", files={"file": ("x.png", buf.getvalue(), "image/png")}, data={"k": "2"})
    assert r.json() == [{"hex": "#1F4D2B", "share": 1.0}]


def test_calibration_flow(client, tmp_path):
    tmpl = io.BytesIO()
    with zipfile.ZipFile(tmpl, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps({"filament_colour": ["#000000"] * 4}))
    r = client.post("/calibrate/template", files={"file": ("t.3mf", tmpl.getvalue())}, follow_redirects=False)
    assert "Template" in r.headers["location"]
    r = client.post("/calibrate/runs", data={"slot1": "1", "slot2": "2", "slot3": "3", "triples": "true"},
                    follow_redirects=False)
    run_url = r.headers["location"].split("?")[0]
    assert client.get(run_url).status_code == 200
    r = client.get(run_url + "/3mf")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    page = client.get(run_url).text
    import re
    ids = re.findall(r'name="m_([A-Z]\d+)"', page)
    data = {f"m_{i}": "#808080" for i in ids}
    assert client.post(run_url + "/measure", data=data, follow_redirects=False).status_code == 303
    from spoolmix import printers

    tpl = printers.default(load_config())
    r = client.post("/calibrate/fit", data={"template": tpl.id}, follow_redirects=False)
    assert "Fitted" in r.headers["location"]
    assert (tpl.dir / "calibration.local.yaml").is_file()  # calibration lives with its template
    r = client.post("/match", data={"targets": "#808080", "model": "calibrated"})
    assert "calibrated model" in r.text


def test_design_project_flow(client, tmp_path):
    import time

    from tests.test_project3mf import _project, _template

    (tmp_path / "template.3mf").write_bytes(_template(tmp_path / "t.3mf").read_bytes())
    src = _project(tmp_path / "p.3mf").read_bytes()
    r = client.post("/projects", files={"file": ("cool model.3mf", src)}, follow_redirects=False)
    url = r.headers["location"]

    def wait():
        for _ in range(100):
            if 'id="jobpanel"' not in client.get(url).text:
                return
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    wait()
    assert "Designer's colors" in client.get(url).text
    client.post(url + "/plan", data={"important": "4", "slots": "4"}, follow_redirects=False)
    wait()
    page = client.get(url).text
    assert "With the filaments you have" in page and "Build print-ready project" in page
    client.post(url + "/convert/base", follow_redirects=False)
    wait()
    r = client.get(url + "/download/base")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "cool%20model-spoolmix.3mf" in r.headers["content-disposition"]
    assert client.get("/").status_code == 200
    assert client.post(url + "/delete", follow_redirects=False).status_code == 303
    assert client.get(url).status_code == 404


def test_new_brand_triggers_published_refresh(tmp_path, monkeypatch):
    from spoolmix import service as svc
    from spoolmix.config import load_config

    monkeypatch.setenv("SPOOLMIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPOOLMAN_URL", "http://spoolman.test:7912")
    monkeypatch.setattr(inventory, "SpoolmanClient", FakeSpoolman)
    calls = []

    def fake_refresh(cfg, brands, progress=None):
        calls.append(sorted(brands))
        (cfg.data_dir / svc.PUBLISHED_FILE).write_text("schema_version: 1\nentries: []\n")
        (cfg.data_dir / svc.BRANDS_FILE).write_text('["sunlu"]')
        return 0

    monkeypatch.setattr(svc, "refresh_published", fake_refresh)
    cfg = load_config()
    svc.auto_td(cfg)  # no cache yet -> fetch
    svc.auto_td(cfg)  # fresh cache, known brands -> no fetch
    assert len(calls) == 1
    (tmp_path / svc.BRANDS_FILE).write_text('["polymaker"]')  # Sunlu now unseen
    svc.auto_td(cfg)
    assert len(calls) == 2


def test_slots_setting_is_remembered(client, tmp_path):
    r = client.post("/match", data={"targets": "#5AB49B\n#E07AB0", "slots": "3"})
    assert "Load these 3 filaments" in r.text
    assert 'name="slots" value="3"' in client.get("/match").text


# --- setup, settings and spoolmix's own filament list ------------------------------------


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A brand-new install: no settings, no SPOOLMAN_URL."""
    monkeypatch.setenv("SPOOLMIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPOOLMAN_URL", "")  # empty: also stops a developer .env from filling it in
    monkeypatch.setattr(inventory, "SpoolmanClient", FakeSpoolman)
    FakeSpoolman.writes, FakeSpoolman.created = [], []
    return TestClient(webapp.app)


def test_first_launch_goes_to_setup(fresh):
    r = fresh.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert fresh.get("/match", follow_redirects=False).headers["location"] == "/setup"
    assert fresh.get("/setup").status_code == 200
    assert fresh.get("/healthz").status_code == 200


def test_env_url_counts_as_set_up(fresh, monkeypatch):
    monkeypatch.setenv("SPOOLMAN_URL", "http://spoolman.test:7912")
    assert fresh.get("/", follow_redirects=False).status_code == 200
    src = inventory.source(load_config())
    assert src.kind == "spoolman" and src.from_env and src.td_in_spoolman


def test_choose_local(fresh):
    r = fresh.post("/setup", data={"kind": "local"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/printer?next=/filaments"  # step 2: printer template
    assert inventory.source(load_config()).kind == "local"
    assert fresh.get("/", follow_redirects=False).status_code == 200
    assert "Add a filament" in fresh.get("/filaments").text


def test_choose_spoolman_tests_connection(fresh):
    r = fresh.post("/setup", data={"kind": "spoolman", "spoolman_url": "spoolman.test:7912", "td_in_spoolman": "1"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/printer?next=/"
    src = inventory.source(load_config())
    assert (src.kind, src.url, src.td_in_spoolman) == ("spoolman", "http://spoolman.test:7912", True)


def test_bad_spoolman_url_is_explained(fresh, monkeypatch):
    import httpx

    class Down(FakeSpoolman):
        def info(self):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(inventory, "SpoolmanClient", Down)
    j = fresh.post("/api/spoolman/test", data={"url": "http://nope:7912"}).json()
    assert not j["ok"] and "Couldn't connect" in j["error"]
    r = fresh.post("/setup", data={"kind": "spoolman", "spoolman_url": "http://nope:7912"}, follow_redirects=False)
    assert "/setup?err=" in r.headers["location"]
    assert inventory.source(load_config()) is None


def test_test_endpoint_reports_counts(fresh):
    j = fresh.post("/api/spoolman/test", data={"url": "http://spoolman.test:7912/api/v1/"}).json()
    assert j == {"ok": True, "url": "http://spoolman.test:7912", "version": "0.27.0", "filaments": 5, "in_stock": 5,
                 "needs_fields": []}


def test_no_td_writes_to_spoolman_without_consent(fresh):
    cfg = load_config()
    inventory.save_source(cfg, "spoolman", "http://spoolman.test:7912", td_in_spoolman=False)
    res = service.auto_td(cfg)
    assert res["written"] and FakeSpoolman.writes == [] and FakeSpoolman.created == []
    overlay = json.loads((cfg.data_dir / inventory.TD_OVERLAY_FILE).read_text())
    assert set(overlay) == {str(w["id"]) for w in res["written"]}
    with inventory.open_inventory(cfg) as inv:
        cyan = next(f for f in inv.filaments() if f.id == 1)
    assert cyan.td == overlay["1"]["td"]
    assert service.auto_td(cfg)["written"] == []  # stable on the next run


# --- spoolmix's own list -------------------------------------------------------------------


@pytest.fixture
def local(fresh):
    inventory.save_source(load_config(), "local")
    return fresh


def _add(c, **kw):
    data = {"brand": "Sunlu", "material": "PLA", "name": "Cyan", "hex": "#00C3FF"} | kw
    return c.post("/filaments/add", data=data, follow_redirects=False)


def test_local_add_edit_have_delete(local):
    cfg = load_config()
    assert "msg=" in _add(local).headers["location"]
    assert "msg=" in _add(local, name="Magenta", hex="f54399", td="5.5").headers["location"]
    inv = inventory.LocalInventory(cfg.data_dir / inventory.LOCAL_FILE)
    cyan, magenta = inv.filaments()
    assert (cyan.hex, cyan.vendor, cyan.in_stock, cyan.key) == ("00C3FF", "Sunlu", True, "local:1")
    assert magenta.td == 5.5 and service.td_owner(magenta) == "user"

    local.post(f"/filaments/{cyan.id}/have", data={}, follow_redirects=False)  # unticked
    assert not inv.filaments()[0].in_stock

    local.post(f"/filaments/{magenta.id}/edit", data={"brand": "Sunlu", "material": "PLA+", "name": "Hot Pink", "hex": "#FF3399"})
    m = inv.get(magenta.id)
    assert (m["material"], m["name"], m["hex"], m["td"]) == ("PLA+", "Hot Pink", "FF3399", 5.5)  # your TD kept

    local.post(f"/filaments/{cyan.id}/delete")
    assert [f.name for f in inv.filaments()] == ["Hot Pink"]


def test_local_validation(local):
    assert "err=" in _add(local, hex="blue").headers["location"]
    assert "err=" in _add(local, material="").headers["location"]
    assert "err=" in _add(local, name="").headers["location"]
    assert "err=" in _add(local, td="0").headers["location"]
    assert inventory.LocalInventory(load_config().data_dir / inventory.LOCAL_FILE).filaments() == []


def test_local_list_gets_tds_and_plans(local):
    for name, hx in (("Cyan", "00C3FF"), ("Magenta", "F54399"), ("Yellow", "FAE727"), ("White", "F5F4F2")):
        _add(local, name=name, hex=hx)
    page = local.get("/filaments").text
    assert "estimate" in page or "published" in page
    tds = [f.td for f in inventory.LocalInventory(load_config().data_dir / inventory.LOCAL_FILE).filaments()]
    assert all(td is not None for td in tds)
    r = local.post("/match", data={"targets": "#5AB49B", "top": "1"})
    assert "Mix ratio" in r.text or "F1" in r.text


def test_edit_clears_estimated_td(local):
    _add(local)
    local.get("/filaments")  # fills an estimate/lookup
    inv = inventory.LocalInventory(load_config().data_dir / inventory.LOCAL_FILE)
    assert service.td_owner(inv.filaments()[0]) in ("lookup", "estimate")
    local.post("/filaments/1/edit", data={"brand": "Sunlu", "material": "PLA", "name": "Black", "hex": "#111111"},
               follow_redirects=False)
    row = inv.get(1)
    assert (row["hex"], row["td"], row["td_source"]) == ("111111", None, None)  # looked up again next sync
    local.get("/filaments")
    assert inv.get(1)["td"] < 1  # black: nearly opaque


def test_spoolman_only_pages_reject_local_edits(fresh, monkeypatch):
    monkeypatch.setenv("SPOOLMAN_URL", "http://spoolman.test:7912")
    assert "err=" in _add(fresh).headers["location"]


def test_import_from_spoolman(local):
    r = local.post("/settings/import-spoolman", data={"url": "http://spoolman.test:7912"}, follow_redirects=False)
    assert "Copied+5" in r.headers["location"] or "Copied%205" in r.headers["location"]
    again = local.post("/settings/import-spoolman", data={"url": "http://spoolman.test:7912"}, follow_redirects=False)
    assert "Copied%200" in again.headers["location"] or "Copied+0" in again.headers["location"]
    white = next(f for f in inventory.LocalInventory(load_config().data_dir / inventory.LOCAL_FILE).filaments() if f.name == "White")
    assert white.td == 4.0 and white.td_source.startswith("measured:")


def test_settings_page_and_general(local):
    assert local.get("/settings").status_code == 200
    local.post("/settings/general", data={"slots": "6", "scope": "list"})
    cfg = load_config()
    assert service.default_slots(cfg) == 6 and shopdb.shop_scope(cfg) == "list"


def test_switching_keeps_local_list(local):
    _add(local)
    local.post("/settings/source", data={"kind": "spoolman", "spoolman_url": "http://spoolman.test:7912"})
    assert inventory.source(load_config()).kind == "spoolman"
    assert "1 filament" in local.get("/settings").text
    local.post("/settings/source", data={"kind": "local"})
    assert len(inventory.LocalInventory(load_config().data_dir / inventory.LOCAL_FILE).filaments()) == 1


def test_db_browse(local, monkeypatch):
    cfg = load_config()
    items = [{"id": "sunlu_pla_cyan", "brand": "Sunlu", "material": "PLA", "name": "Cyan", "hex": "#00C3FF"},
             {"id": "sunlu_pla_black", "brand": "Sunlu", "material": "PLA", "name": "Black", "hex": "#111111"},
             {"id": "sunlu_petg_red", "brand": "Sunlu", "material": "PETG", "name": "Red", "hex": "#CC2222"},
             {"id": "elegoo_pla_red", "brand": "Elegoo", "material": "PLA", "name": "Red", "hex": "#D02020"}]
    (cfg.data_dir / shopdb.CACHE_FILE).write_text(json.dumps({"fetched": "2026-10-17", "items": items}))
    monkeypatch.setattr(shopdb, "ensure_fresh", lambda cfg: None)
    assert local.get("/api/db/brands").json() == [{"brand": "Elegoo", "count": 1}, {"brand": "Sunlu", "count": 3}]
    assert local.get("/api/db/materials", params={"brand": "sunlu"}).json() == ["PETG", "PLA"]
    names = [c["name"] for c in local.get("/api/db/colors", params={"brand": "Sunlu", "material": "PLA"}).json()]
    assert names == ["Black", "Cyan"]


def _template_bytes(**settings):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps({
            "printer_settings_id": "Test Printer 0.4 nozzle", "print_settings_id": "0.10mm Color Mixing",
            "layer_height": "0.1", "nozzle_diameter": ["0.4"] * 4, "filament_colour": ["#000000"] * 4,
            "filament_settings_id": ["Generic PLA"] * 4, "mixed_filament_definitions": "",
            "dithering_local_z_mode": "0"} | settings))
    return buf.getvalue()


def test_setup_step_two_printer_template(fresh):
    fresh.post("/setup", data={"kind": "local"})
    page = fresh.get("/setup/printer?next=/filaments").text
    assert "Step 2 of 2" in page and "Skip for now" in page and 'href="/filaments"' in page
    r = fresh.post("/template", files={"file": ("t.3mf", _template_bytes(filament_colour=["#000000"] * 5))},
                   data={"next": "/setup/printer?next=/filaments"}, follow_redirects=False)
    assert r.headers["location"].startswith("/setup/printer?next=/filaments&msg=Template")
    page = fresh.get("/setup/printer?next=/filaments").text
    assert "Template saved" in page and "Test Printer" in page and "Continue" in page
    assert service.default_slots(load_config()) == 5  # slot count follows the template
    # once a template exists, setup goes straight on
    r = fresh.post("/setup", data={"kind": "local"}, follow_redirects=False)
    assert r.headers["location"].startswith("/filaments")


def test_template_upload_rejects_non_projects(local):
    r = local.post("/template", files={"file": ("x.3mf", b"not a zip")}, follow_redirects=False)
    assert "err=" in r.headers["location"] and r.headers["location"].endswith("#templates")
    assert not (load_config().data_dir / "template.3mf").exists()


def test_template_card_warns_about_subdivide_and_missing_mixing(local):
    local.post("/template", files={"file": ("t.3mf", _template_bytes(dithering_local_z_mode="1"))})
    page = local.get("/settings").text
    assert "Subdivide mix layers" in page and "the ratio barely mattered" in page
    raw = json.loads(zipfile.ZipFile(io.BytesIO(_template_bytes())).read("Metadata/project_settings.config"))
    raw.pop("mixed_filament_definitions")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(raw))
    r = local.post("/template", files={"file": ("t.3mf", buf.getvalue())}, follow_redirects=False)
    assert "no+color-mixing" in r.headers["location"] or "no%20color-mixing" in r.headers["location"]
    assert "no color-mixing settings" in local.get("/settings").text


def test_home_points_to_template_setup(local):
    page = local.get("/").text
    assert "Set up printer template" in page and "/settings#template" in page


def test_speckled_filaments_are_flagged(local):
    from spoolmix.report import texture

    assert texture("Bambu Lab PLA Brown Galaxy") == "speckled"
    assert texture("Sunlu PLA High Speed Ashen Marble") == "speckled"
    assert texture("Polymaker PolyTerra Stone Grey") is None  # a solid color
    assert texture("Sunlu PLA Cyan") is None
    r = _add(local, name="Brown Galaxy", hex="#6B4A3E")
    assert "speckled" in r.headers["location"]
    page = local.get("/filaments").text
    assert 'class="tag tex"' in page


def test_speckled_filament_in_a_recipe_warns():
    from spoolmix.report import recipe_warnings
    from spoolmix.solver import Component, Recipe

    galaxy = Component("s:1", "Bambu Lab PLA Brown Galaxy", (107, 74, 62), 1.0, material="PLA")
    white = Component("s:2", "Sunlu PLA+ White", (255, 255, 255), 7.0, material="PLA")
    assert any("as one flat color" in w for w in recipe_warnings(Recipe((galaxy,), (100,), galaxy.rgb, 1.0)))
    assert any("won't look uniform" in w for w in recipe_warnings(Recipe((galaxy, white), (40, 60), (200, 180, 170), 1.0)))


def test_priority_changes_the_trade_off():
    from spoolmix.solver import Component, Solver

    # black + yellow gets closer but stripes (black is opaque); cyan + yellow blends but is further off
    stock = [Component("s:k", "Black", (0, 0, 0), 0.1), Component("s:y", "Yellow", (246, 250, 0), 6.2),
             Component("s:c", "Cyan", (57, 208, 241), 3.7)]
    best = {p: Solver(priority=p, allow_triples=False).solve((90, 170, 80), stock, top=1)[0] for p in ("color", "smooth")}
    assert {c.label for c in best["color"].parts} == {"Black", "Yellow"}
    assert {c.label for c in best["smooth"].parts} == {"Cyan", "Yellow"}
    assert best["color"].delta_e < best["smooth"].delta_e and best["smooth"].stripe < best["color"].stripe


def test_priority_is_remembered_and_settable(local):
    local.post("/settings/general", data={"slots": "4", "scope": "all", "priority": "smooth"})
    assert service.default_priority(load_config()) == "smooth"
    assert 'value="smooth" selected' in local.get("/match").text



# --- multiple printer templates ---------------------------------------------------------------


def _tpl_upload(c, name="", **settings):
    return c.post("/template", files={"file": ("t.3mf", _template_bytes(**settings))}, data={"name": name},
                  follow_redirects=False)


def test_legacy_template_and_calibration_move_into_the_store(local):
    from spoolmix import calibrate, printers

    cfg = load_config()
    (cfg.data_dir / "template.3mf").write_bytes(_template_bytes())
    (cfg.data_dir / calibrate.CALIBRATION_FILE).write_text("alpha: 0.5\n")
    run = calibrate.new_run([calibrate.CalFilament(1, "local:1", "A", "FF0000", None),
                             calibrate.CalFilament(2, "local:2", "B", "00FF00", None)], cfg.data_dir)
    ts = printers.all_templates(cfg)
    assert len(ts) == 1 and ts[0].default and ts[0].name == "Test Printer 0.4 nozzle · 0.10mm Color Mixing"
    assert ts[0].calibration() == {"alpha": 0.5}
    assert not (cfg.data_dir / "template.3mf").exists() and not (cfg.data_dir / calibrate.CALIBRATION_FILE).exists()
    assert calibrate.load_run(run.id, cfg.data_dir).template == ts[0].id


def test_several_templates_rename_default_delete(local):
    from spoolmix import printers

    cfg = load_config()
    _tpl_upload(local)
    _tpl_upload(local, name="Fine, subdivided", dithering_local_z_mode="1", filament_colour=["#000000"] * 5)
    ts = printers.all_templates(cfg)
    assert [t.name for t in ts] == ["Test Printer 0.4 nozzle · 0.10mm Color Mixing", "Fine, subdivided"]
    first, second = ts
    assert first.default and second.slots == 5 and second.info["subdivide"]
    page = local.get("/settings").text
    assert "Fine, subdivided" in page and "Make default" in page

    local.post(f"/templates/{second.id}/rename", data={"name": "U1 fine"})
    local.post(f"/templates/{second.id}/default")
    ts = printers.all_templates(cfg)
    assert ts[0].name == "U1 fine" and ts[0].default
    local.post(f"/templates/{second.id}/delete")
    ts = printers.all_templates(cfg)
    assert [t.id for t in ts] == [first.id] and ts[0].default  # default falls back to what's left


def test_plan_uses_the_chosen_template_and_its_slots(local, tmp_path):
    import time

    from spoolmix import printers, projects

    from tests.test_project3mf import _project

    for n, hx in (("Cyan", "00B5E2"), ("Magenta", "E0218A"), ("Yellow", "FFE600"), ("White", "F4F4F2"), ("Black", "151515")):
        _add(local, name=n, hex=hx)
    _tpl_upload(local)
    _tpl_upload(local, name="Five", filament_colour=["#000000"] * 5)
    cfg = load_config()
    five = next(t for t in printers.all_templates(cfg) if t.name == "Five")
    url = local.post("/projects", files={"file": ("d.3mf", _project(tmp_path / "d.3mf").read_bytes())},
                     follow_redirects=False).headers["location"]

    def wait():
        for _ in range(200):
            if 'id="jobpanel"' not in local.get(url).text:
                return
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    wait()
    assert 'class="print-with"' in local.get(url).text
    local.post(url + "/plan", data={"template": five.id, "slots": "2"})  # the typed slot count is ignored
    wait()
    meta = projects.load_meta(cfg, url.rsplit("/", 1)[1])
    assert meta["plans"]["opts"]["template"] == five.id and meta["plans"]["opts"]["slots"] == 5
    assert "Planned for <b>Five</b>" in local.get(url).text
    # deleting the template leaves the plan readable but unbuildable until re-planned
    local.post(f"/templates/{five.id}/delete")
    assert "was deleted" in local.get(url).text


def test_material_mismatch_is_flagged(local, tmp_path):
    from tests.test_project3mf import _project

    _tpl_upload(local, name="PETG one", filament_type=["PETG"] * 4)
    url = local.post("/projects", files={"file": ("d.3mf", _project(tmp_path / "d.3mf").read_bytes())},
                     follow_redirects=False).headers["location"]
    import time
    for _ in range(200):
        if 'id="jobpanel"' not in local.get(url).text:
            break
        time.sleep(0.05)
    page = local.get(url).text
    assert 'data-cats="PETG"' in page and 'id="tplmismatch"' in page


def test_calibration_is_per_template(local):
    from spoolmix import calibrate, printers

    _tpl_upload(local, name="A")
    _tpl_upload(local, name="B")
    cfg = load_config()
    a, b = sorted(printers.all_templates(cfg), key=lambda t: t.name)
    calibrate.save_calibration(a.dir, {"alpha": 0.5, "strengths": {}, "colors": {}})
    assert a.calibration() and b.calibration() is None
    assert "(calibrated)" in local.get(f"/calibrate?t={a.id}").text
    local.post("/calibrate/reset", data={"template": a.id})
    assert a.calibration() is None


def test_subdivided_template_warns_on_plans_and_matches(local, tmp_path):
    import time

    from tests.test_project3mf import _project

    for n, hx in (("Cyan", "00B5E2"), ("Magenta", "E0218A"), ("Yellow", "FFE600"), ("White", "F4F4F2"), ("Black", "151515")):
        _add(local, name=n, hex=hx)
    _tpl_upload(local, name="Subdivided", dithering_local_z_mode="1")
    assert "the ratio barely mattered" in local.get("/settings").text
    url = local.post("/projects", files={"file": ("d.3mf", _project(tmp_path / "d.3mf").read_bytes())},
                     follow_redirects=False).headers["location"]

    def wait():
        for _ in range(200):
            if 'id="jobpanel"' not in local.get(url).text:
                return
            time.sleep(0.05)

    wait()
    local.post(url + "/plan", data={"shop": ""})
    wait()
    page = local.get(url).text
    assert "has Subdivide Mix Layer on, so the mixes" in page  # the design is painted and the plan mixes
    r = local.post("/match", data={"targets": "#2E7D5B", "top": "1"})
    assert "has Subdivide Mix Layer on" in r.text


def _petg_design(path):
    """The synthetic design from test_project3mf, but PETG (like a downloaded PETG model)."""
    import zipfile as zf

    from tests.test_project3mf import _project

    src = _project(path.with_name("pla.3mf"))
    with zf.ZipFile(src) as zin, zf.ZipFile(path, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n == "Metadata/project_settings.config":
                s = json.loads(data)
                s["filament_type"] = ["PETG"] * len(s["filament_type"])
                data = json.dumps(s).encode()
            zout.writestr(n, data)
    return path


def test_print_a_petg_design_in_pla(local, tmp_path):
    import time

    from spoolmix import projects

    for n, hx in (("Cyan", "00B5E2"), ("Magenta", "E0218A"), ("Yellow", "FFE600"), ("White", "F4F4F2"), ("Black", "151515")):
        _add(local, name=n, hex=hx)  # all PLA
    _add(local, material="PETG", name="White", hex="#FFFFFF")
    _tpl_upload(local, name="PLA template", filament_type=["PLA"] * 4)
    url = local.post("/projects", files={"file": ("d.3mf", _petg_design(tmp_path / "d.3mf").read_bytes())},
                     follow_redirects=False).headers["location"]

    def wait():
        for _ in range(200):
            if 'id="jobpanel"' not in local.get(url).text:
                return
            time.sleep(0.05)

    wait()
    page = local.get(url).text
    # PETG design, PLA template, 5 PLA vs 1 PETG filament: offer to print it in PLA
    assert "Print it in PLA" in page and 'id="printmat"' in page
    local.post(url + "/plan", data={"material": "PLA", "shop": ""})
    wait()
    meta = projects.load_meta(load_config(), url.rsplit("/", 1)[1])
    assert meta["plans"]["materials"] == ["PLA"] and meta["plans"]["designed_materials"] == ["PETG"]
    assert all(s["label"].startswith("Acme PLA") or "PLA" in s["label"] for s in meta["plans"]["base"]["slots"])
    page = local.get(url).text
    assert "you chose to print it in" in page and "Print it in PLA" not in page


def test_swap_filaments_build_and_discard(local, tmp_path):
    import time

    from spoolmix import projects
    from tests.test_project3mf import _project

    for n, hx in (("Cyan", "00B5E2"), ("Magenta", "E0218A"), ("Red", "C8102E"), ("Yellow", "FFE600"),
                  ("White", "F4F4F2"), ("Black", "151515")):
        _add(local, name=n, hex=hx)
    _tpl_upload(local)
    cfg = load_config()
    url = local.post("/projects", files={"file": ("d.3mf", _project(tmp_path / "d.3mf").read_bytes())},
                     follow_redirects=False).headers["location"]
    pid = url.rsplit("/", 1)[1]

    def wait():
        for _ in range(300):
            if 'id="jobpanel"' not in local.get(url).text:
                return
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    wait()
    local.post(url + "/plan", data={"shop": ""})
    wait()
    page = local.get(url).text
    assert "Try other filaments" in page and 'id="swap-base"' in page and 'id="swapbox-base"' in page
    base = projects.load_meta(cfg, pid)["plans"]["base"]
    keys = [s["key"] for s in base["slots"]]
    stock = {f["label"]: f["key"] for f in projects.stock_overview(cfg)}
    # swap the first filament for one the plan didn't use
    unused = next(k for k in stock.values() if k not in keys)
    picked = [unused] + keys[1:]
    local.post(url + "/custom", data={"which": "base", **{f"f{i + 1}": k for i, k in enumerate(picked)}})
    wait()
    plans = projects.load_meta(cfg, pid)["plans"]
    custom = plans["custom"]
    assert custom["from"] == "base" and {s["key"] for s in custom["slots"]} == set(picked)
    page = local.get(url).text
    assert "Your version" in page and "Compared with the plan with your filaments" in page
    local.post(url + "/convert/custom")
    wait()
    r = local.get(url + "/download/custom")
    assert r.status_code == 200 and r.content[:2] == b"PK" and "-spoolmix-custom.3mf" in r.headers["content-disposition"]
    local.post(url + "/custom/discard")
    assert "custom" not in projects.load_meta(cfg, pid)["plans"]
    assert local.get(url + "/download/custom").status_code == 404
    # re-planning clears a custom version too
    local.post(url + "/custom", data={"which": "base", "f1": keys[0]})
    wait()
    assert projects.load_meta(cfg, pid)["plans"]["custom"]["slots"][0]["key"] == keys[0]
    local.post(url + "/plan", data={"shop": ""})
    wait()
    assert "custom" not in projects.load_meta(cfg, pid)["plans"]


def test_swap_keeps_a_purchase_from_an_option():
    from spoolmix import design
    from spoolmix.project3mf import DesignFilament
    from spoolmix.solver import Component, Solver

    d = [DesignFilament(1, "#2850E0", "PLA", "", 500.0), DesignFilament(2, "#FFFFFF", "PLA", "", 500.0)]
    white = Component("s:w", "White", (245, 244, 242), 6.5, material="PLA")
    blue = Component("shop:blue", "Blue", (40, 80, 224), 3.0, owned=False, material="PLA")
    p = design.plan_fixed(Solver(), d, [white, blue], buy=[blue])
    assert p.buy == (blue,) and {c.key for c in p.result.slots} == {"s:w", "shop:blue"}
    assert p.result.weighted < 2.0 and all(r.size == 1 for r in p.result.recipes)  # both plain


def test_static_urls_change_when_the_files_do(local):
    """Browsers cache app.js/style.css; the URL carries a content hash so updates can't be
    served from an old cached copy (Brave kept running an old app.js)."""
    import re

    from spoolmix.web import app as webapp

    page = local.get("/settings").text
    js = re.search(r'src="(/static/app\.js\?v=[0-9a-f]{10})"', page)
    css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', page)
    assert js and css
    assert local.get(js.group(1)).status_code == 200
    assert webapp._asset_url("app.js") == js.group(1)
