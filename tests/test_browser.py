"""Click through the web UI in a real (headless) Chrome and fail on any JavaScript error.

Runs the app in-process on a free port with a throwaway data folder and a small offline
filament database, so it needs no network and no Spoolman. Skipped when Chrome isn't
available locally; in CI (SPOOLMIX_E2E=1) a missing browser is a failure instead.
Install the browser with: uv run playwright install chromium
"""

import importlib.util
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

REQUIRED = os.environ.get("SPOOLMIX_E2E") == "1"
HERE = Path(__file__).parent

# A few real-looking SpoolmanDB entries for the add-filament picker.
DB_ITEMS = [
    {"id": "acme_pla_cyan", "brand": "Acme", "material": "PLA", "name": "Cyan", "hex": "#00B5E2"},
    {"id": "acme_pla_magenta", "brand": "Acme", "material": "PLA", "name": "Magenta", "hex": "#E0218A"},
    {"id": "acme_pla_yellow", "brand": "Acme", "material": "PLA", "name": "Yellow", "hex": "#FFE600"},
    {"id": "acme_pla_white", "brand": "Acme", "material": "PLA", "name": "White", "hex": "#F4F4F2"},
    {"id": "acme_pla_black", "brand": "Acme", "material": "PLA", "name": "Black", "hex": "#151515"},
    {"id": "acme_pla_brown_galaxy", "brand": "Acme", "material": "PLA", "name": "Brown Galaxy", "hex": "#6B4A3E"},
    {"id": "acme_petg_red", "brand": "Acme", "material": "PETG", "name": "Red", "hex": "#C8102E"},
    {"id": "other_pla_green", "brand": "Other", "material": "PLA", "name": "Green", "hex": "#00A651"},
]


def _helpers():
    """The synthetic design and template builders from test_project3mf."""
    spec = importlib.util.spec_from_file_location("_p3mf", HERE / "test_project3mf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    data = tmp_path_factory.mktemp("data")
    (data / "spoolmandb.json").write_text(json.dumps({"fetched": "2026-10-18", "items": DB_ITEMS}))
    old = {k: os.environ.get(k) for k in ("SPOOLMIX_DATA_DIR", "SPOOLMAN_URL", "SPOOLMIX_AUTO_TD")}
    os.environ.update(SPOOLMIX_DATA_DIR=str(data), SPOOLMAN_URL="", SPOOLMIX_AUTO_TD="0")

    from spoolmix.web.app import app

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", data
    srv.should_exit = True
    thread.join(5)
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def browser():
    try:
        with playwright.sync_playwright() as p:
            b = p.chromium.launch()
            yield b
            b.close()
    except Exception as exc:  # no browser installed / missing system libraries
        if REQUIRED:
            raise
        pytest.skip(f"headless Chrome not available: {exc}")


@pytest.fixture
def page(browser, server):
    """A page that records every JavaScript error and failed request; the test fails if there were any."""
    errors: list[str] = []
    pg = browser.new_page(viewport={"width": 1200, "height": 900})
    pg.on("pageerror", lambda e: errors.append(f"page error: {e}"))
    pg.on("console", lambda m: m.type == "error" and errors.append(f"console error: {m.text}"))
    pg.set_default_timeout(20000)
    pg.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status}: {r.url}"))
    yield pg
    pg.close()
    assert errors == [], "JavaScript errors:\n" + "\n".join(errors)


def test_first_launch_setup_and_printer_template(page, server, tmp_path):
    base, _ = server
    page.goto(base + "/")
    assert page.url.endswith("/setup")
    page.check("input[value=spoolman]")
    assert page.is_visible("#spoolman_url")
    page.click("#testbtn")  # nothing at that address: a readable error, not a crash
    page.wait_for_function("document.querySelector('#testres').textContent.length > 10")
    page.check("input[value=local]")
    assert page.is_hidden("#spoolman_url")
    page.click("#sourceform button.primary")
    page.wait_for_url("**/setup/printer**")
    assert "Step 2 of 2" in page.content()

    template = _helpers()._template(tmp_path / "template.3mf")
    page.set_input_files("input[name=file]", str(template))
    page.click("form[action='/template'] button")
    page.wait_for_selector("text=Template saved")
    page.click("text=Continue")
    page.wait_for_url("**/filaments")


def test_add_filaments_from_the_database_and_by_hand(page, server):
    base, _ = server
    page.goto(base + "/filaments")

    def pick(name):
        page.fill("#f-brand", "Acme")
        page.dispatch_event("#f-brand", "change")
        page.fill("#f-material", "PLA")
        page.dispatch_event("#f-material", "change")
        page.wait_for_selector(".dbcolor")
        page.fill("#f-name", name)
        page.locator(".dbcolor", has_text=name).first.click()
        assert page.input_value("#f-dbid").startswith("acme_pla_")
        page.click("#addform button.primary")
        page.wait_for_selector(".flash")

    for name in ("Cyan", "Magenta", "Yellow", "White", "Black"):
        pick(name)
    # speckled filament: flagged when picked
    page.fill("#f-brand", "Acme")
    page.dispatch_event("#f-brand", "change")
    page.fill("#f-material", "PLA")
    page.dispatch_event("#f-material", "change")
    page.wait_for_selector(".dbcolor")
    page.locator(".dbcolor", has_text="Brown Galaxy").click()
    assert page.is_visible("#texnote")
    page.click("#addform button.primary")
    page.wait_for_selector(".flash")
    assert "speckled" in page.text_content(".flash")

    # by hand, with the color picker in sync
    page.fill("#f-brand", "Garage Spools")
    page.fill("#f-material", "PLA")
    page.fill("#f-name", "Rust")
    page.fill("#f-hex", "#B7410E")
    page.dispatch_event("#f-hex", "input")
    assert page.input_value("#f-color") == "#b7410e"
    page.click("#addform button.primary")
    page.wait_for_selector(".flash")

    rows = page.locator("#ftable tbody tr.frow")
    assert rows.count() == 7
    page.click("#ftable th[data-sort=name]")
    page.fill("#tfilter", "garage")
    assert sum(1 for i in range(rows.count()) if rows.nth(i).is_visible()) == 1
    page.fill("#tfilter", "")
    page.locator("#ftable button[data-edit]").first.click()
    assert page.locator("tr.editrow:not([hidden])").count() == 1


def test_plan_a_design_and_build_it(page, server, tmp_path):
    base, _ = server
    design = _helpers()._project(tmp_path / "design.3mf")
    page.goto(base + "/")
    page.set_input_files("#file3mf", str(design))
    page.wait_for_url("**/projects/*")
    page.wait_for_selector("form[action$='/plan'] button.primary:not([disabled])", timeout=60000)
    page.select_option("select[name=priority]", "smooth")
    page.click("form[action$='/plan'] button.primary")
    page.wait_for_selector("#plan-base", timeout=120000)
    assert "Load these filaments" in page.content()
    page.click("#plan-base form[action*='/convert/'] button")
    page.wait_for_selector("#plan-base a[href*='/download/'], #jobpanel .flash.err", timeout=120000)
    failed = page.locator("#jobpanel .flash.err")
    assert failed.count() == 0, failed.first.text_content()


def test_match_a_color(page, server):
    base, _ = server
    page.goto(base + "/match")
    page.fill("#hexin", "#2E7D5B")
    page.click("#addhex")
    page.click("#go")
    page.wait_for_selector(".result", timeout=60000)
    assert "Mix ratio" in page.content()


def test_settings_and_other_pages(page, server):
    base, _ = server
    page.goto(base + "/settings")
    page.check("input[name=priority][value=color]")
    page.click("form[action='/settings/general'] button.primary")
    page.wait_for_selector(".flash")
    for path in ("/shopping", "/calibrate", "/filaments"):
        page.goto(base + path)
        assert page.locator("h1").count() == 1


def test_second_template_and_print_with(page, server, tmp_path):
    base, _ = server
    petg = tmp_path / "petg.3mf"
    import zipfile

    with zipfile.ZipFile(petg, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps({
            "printer_settings_id": "Test Printer", "print_settings_id": "0.20mm PETG", "filament_colour": ["#000000"] * 4,
            "filament_type": ["PETG"] * 4, "mixed_filament_definitions": ""}))
    page.goto(base + "/settings#templates")
    page.click("#templates details summary")
    page.set_input_files("#templates details input[name=file]", str(petg))
    page.fill("#templates details input[name=name]", "PETG test")
    page.click("#templates details form[action='/template'] button")
    page.wait_for_selector("text=Template saved as")
    assert page.locator(".tplcard").count() == 2

    # a PLA design: the PETG template is flagged when picked, and the warning goes away again
    page.goto(base + "/")
    page.locator("a[href^='/projects/']").first.click()
    page.wait_for_selector("select.print-with")
    assert page.is_hidden("#tplmismatch")
    page.locator("select.print-with").select_option(label="PETG test · 4 slots")
    assert page.is_visible("#tplmismatch") and "PETG" in page.text_content("#tplmismatch")
    page.locator("select.print-with").select_option(index=0)
    assert page.is_hidden("#tplmismatch")


def test_calibration_grid_reads_a_row_photo(page, server, tmp_path):
    """Create a 2-filament block grid, upload a synthetic photo of row A (7 blocks on grey
    paper), click paper / first / last block, and check every block's colour is read and
    white-balanced on the paper."""
    from PIL import Image, ImageDraw

    base, _ = server
    page.goto(base + "/calibrate")
    page.select_option("select[name=slot1]", index=1)
    page.select_option("select[name=slot2]", index=2)
    page.click("form[action='/calibrate/runs'] button.primary")
    page.wait_for_url("**/calibrate/runs/*")

    paper = (200, 200, 190)  # slightly warm grey: white balance must neutralise it
    colors = [(30, 140, 180), (180, 170, 20), (60, 150, 140), (90, 160, 110), (120, 165, 80), (140, 165, 60), (160, 168, 40)]
    img = Image.new("RGB", (1400, 500), paper)
    d = ImageDraw.Draw(img)
    centers = []
    for i, c in enumerate(colors):
        x = 120 + i * 190
        d.rectangle([x - 60, 180, x + 60, 320], fill=c)
        centers.append((x, 250))
    photo = tmp_path / "row.png"
    img.save(photo)

    page.select_option("#photorow", "0")
    page.set_input_files("#photofile", str(photo))
    page.wait_for_function("document.querySelector('#photocanvas').width === 1400")
    box = page.locator("#photocanvas").bounding_box()
    scale = box["width"] / 1400
    for x, y in [(700, 60), centers[0], centers[-1]]:
        page.locator("#photocanvas").click(position={"x": x * scale, "y": y * scale})
    gain = [240 / p for p in paper]
    for i, c in enumerate(colors):
        got = page.input_value(f"input[name=m_A{i + 1}]")
        rgb = [int(got[k:k + 2], 16) for k in (1, 3, 5)]
        want = [min(255, v * g) for v, g in zip(c, gain)]
        assert all(abs(a - b) <= 3 for a, b in zip(rgb, want)), (i, got, want)
    page.click("#measureform button.primary")
    page.wait_for_selector("text=Saved 7 measured")


def test_swap_a_filament_and_compare(page, server):
    base, _ = server
    page.goto(base + "/")
    page.locator("a[href^='/projects/']").first.click()
    page.wait_for_selector("#plan-base")
    page.click("#swapbox-base > summary")
    form = page.locator("#swap-base")
    assert form.is_visible()
    first = form.locator("select[name=f1]")
    current = first.input_value()
    options = [o for o in first.locator("option").evaluate_all("os => os.map(o => o.value)") if o and o != current]
    first.select_option(options[0])
    form.locator("button.primary").click()
    page.wait_for_selector("#plan-custom", timeout=120000)
    assert "Compared with the plan with your filaments" in page.content()
    page.click("text=Discard")
    page.wait_for_selector("#plan-base")
    assert page.locator("#plan-custom").count() == 0
