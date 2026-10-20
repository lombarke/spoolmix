/* spoolmix web UI: small vanilla JS, no build step. */
const SpoolMix = (() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const hex2 = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
  const toHex = (r, g, b) => ("#" + hex2(r) + hex2(g) + hex2(b)).toUpperCase();
  const normHex = (v) => {
    const m = String(v).trim().match(/^#?([0-9a-f]{6})([0-9a-f]{2})?$/i);
    return m ? "#" + m[1].toUpperCase() : null;
  };

  function loadImageToCanvas(file, canvas, maxW = 1400) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, maxW / img.naturalWidth);
        canvas.width = Math.round(img.naturalWidth * scale);
        canvas.height = Math.round(img.naturalHeight * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas._img = img;
        resolve(canvas);
      };
      img.onerror = reject;
      img.src = URL.createObjectURL(file);
    });
  }

  function canvasPoint(canvas, ev) {
    const r = canvas.getBoundingClientRect();
    return { x: (ev.clientX - r.left) * (canvas.width / r.width), y: (ev.clientY - r.top) * (canvas.height / r.height) };
  }

  function sampleAvg(ctx, x, y, radius) {
    const r = Math.max(1, Math.round(radius));
    const x0 = Math.max(0, Math.round(x - r)), y0 = Math.max(0, Math.round(y - r));
    const w = Math.min(ctx.canvas.width - x0, 2 * r + 1), h = Math.min(ctx.canvas.height - y0, 2 * r + 1);
    const d = ctx.getImageData(x0, y0, w, h).data;
    let R = 0, G = 0, B = 0, n = 0;
    for (let i = 0; i < d.length; i += 4) { R += d[i]; G += d[i + 1]; B += d[i + 2]; n++; }
    return [R / n, G / n, B / n];
  }

  // ---------------------------------------------------------------- match page
  function initMatch() {
    const targets = [];
    const chips = $("#chips"), picker = $("#picker"), hexin = $("#hexin");

    function render() {
      chips.innerHTML = "";
      targets.forEach((t, i) => {
        const c = document.createElement("span");
        c.className = "chip";
        c.innerHTML = `<span class="sw" style="background:${t.hex}"></span><span class="hex">${t.hex}</span>` +
          (t.label ? `<span class="lbl">${t.label}</span>` : "") + `<button type="button" title="remove">×</button>`;
        c.querySelector("button").onclick = () => { targets.splice(i, 1); render(); };
        chips.appendChild(c);
      });
      $("#targets").value = targets.map((t) => t.hex + (t.label ? " " + t.label : "")).join("\n");
    }
    function add(hex, label = "") {
      const h = normHex(hex);
      if (!h) { hexin.setCustomValidity("Use #RRGGBB"); hexin.reportValidity(); return; }
      hexin.setCustomValidity("");
      if (targets.length >= 12) return;
      if (!targets.some((t) => t.hex === h)) targets.push({ hex: h, label });
      render();
    }
    picker.addEventListener("change", () => add(picker.value));
    picker.addEventListener("input", () => { hexin.value = picker.value.toUpperCase(); });
    $("#addhex").onclick = () => add(hexin.value || picker.value);
    hexin.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(hexin.value); } });
    hexin.addEventListener("input", () => { const h = normHex(hexin.value); if (h) picker.value = h.toLowerCase(); });

    const canvas = $("#imgcanvas"), file = $("#imgfile");
    file.addEventListener("change", async () => {
      if (!file.files[0]) return;
      await loadImageToCanvas(file.files[0], canvas, 1000);
      $("#imgwrap").style.display = "block";
      $("#autocolors").disabled = false;
    });
    canvas.addEventListener("click", (ev) => {
      const p = canvasPoint(canvas, ev);
      const [r, g, b] = sampleAvg(canvas.getContext("2d"), p.x, p.y, 2);
      add(toHex(r, g, b), "from image");
    });
    $("#autocolors").onclick = async () => {
      const fd = new FormData();
      fd.append("file", file.files[0]);
      fd.append("k", $("#kcolors").value || "4");
      $("#autocolors").disabled = true;
      try {
        const res = await fetch("/api/image-colors", { method: "POST", body: fd });
        const cols = await res.json();
        cols.forEach((c) => add(c.hex, Math.round(c.share * 100) + "% of image"));
      } finally { $("#autocolors").disabled = false; }
    };

    $("#matchform").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!targets.length) add(hexin.value || picker.value);
      if (!targets.length) return;
      const fd = new FormData(ev.target);
      $("#go").disabled = true;
      $("#spinner").classList.add("on");
      $("#results").innerHTML = "";
      try {
        const res = await fetch("/match", { method: "POST", body: fd });
        $("#results").innerHTML = await res.text();
        $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e) {
        $("#results").innerHTML = `<div class="flash err">Request failed: ${e}</div>`;
      } finally {
        $("#go").disabled = false;
        $("#spinner").classList.remove("on");
      }
    });
  }

  // ---------------------------------------------------------------- calibration run
  function initRun() {
    // Keep color pickers, hex fields and the measured preview in sync (empty filler cells have none).
    $$(".cell[data-id]").forEach((cell) => {
      const color = $("input[type=color]", cell), text = $("input[type=text]", cell), meas = $(".meas", cell);
      color.addEventListener("input", () => { text.value = color.value.toUpperCase(); meas.style.background = color.value; });
      text.addEventListener("input", () => {
        const h = normHex(text.value);
        if (h) { color.value = h.toLowerCase(); meas.style.background = h; }
        else if (!text.value.trim()) meas.style.background = "transparent";
      });
    });

    const canvas = $("#photocanvas"), file = $("#photofile");
    if (!canvas) return;
    const rowSel = $("#photorow"), labels = $$("#stepper span");
    let pts = [];

    function stepUI() {
      labels.forEach((s, i) => { s.className = i < pts.length ? "done" : i === pts.length ? "cur" : ""; });
    }
    function redraw(extra = []) {
      const ctx = canvas.getContext("2d");
      ctx.drawImage(canvas._img, 0, 0, canvas.width, canvas.height);
      ctx.lineWidth = 2;
      [...pts, ...extra].forEach((p, i) => {
        ctx.strokeStyle = i === 0 && pts.length ? "#ff0" : "#0ff";
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r || 6, 0, 7); ctx.stroke();
      });
    }
    const reset = () => { pts = []; stepUI(); if (canvas._img) redraw(); };
    file.addEventListener("change", async () => {
      if (!file.files[0]) return;
      await loadImageToCanvas(file.files[0], canvas, 1600);
      reset();
    });
    rowSel.addEventListener("change", reset);
    canvas.addEventListener("click", (ev) => {
      if (!canvas._img) return;
      if (pts.length >= 3) pts = [];
      pts.push(canvasPoint(canvas, ev));
      stepUI(); redraw();
      if (pts.length === 3) fillRow();
    });

    // One photo = one row of blocks: blocks are evenly spaced between the first and last clicks.
    // White balance and exposure come from the paper, mapped to a fixed white, so rows shot
    // separately stay comparable.
    function fillRow() {
      const ctx = canvas.getContext("2d");
      ctx.drawImage(canvas._img, 0, 0, canvas.width, canvas.height);
      const [paper, first, last] = pts;
      const ref = sampleAvg(ctx, paper.x, paper.y, 8);
      const gain = ref.map((c) => 240 / Math.max(1, c));
      const row = +rowSel.value;
      const cells = $$(`.cell[data-row="${row}"]`).sort((a, b) => +a.dataset.col - +b.dataset.col);
      const n = cells.length;
      const step = { x: (last.x - first.x) / Math.max(1, n - 1), y: (last.y - first.y) / Math.max(1, n - 1) };
      const radius = Math.max(2, 0.15 * (Math.hypot(step.x, step.y) || 60));
      const marks = [];
      cells.forEach((cell, i) => {
        const x = first.x + i * step.x, y = first.y + i * step.y;
        const rgb = sampleAvg(ctx, x, y, radius).map((v, k) => Math.min(255, v * gain[k]));
        const h = toHex(...rgb);
        $("input[type=text]", cell).value = h;
        $("input[type=color]", cell).value = h.toLowerCase();
        $(".meas", cell).style.background = h;
        marks.push({ x, y, r: radius });
      });
      redraw(marks);
      const next = rowSel.selectedIndex + 1;
      if (next < rowSel.options.length) rowSel.selectedIndex = next;  // ready for the next row's photo
    }
  }

  // ---------------------------------------------------------------- background jobs
  function pollJob() {
    const panel = $("#jobpanel");
    if (!panel) return;
    const tick = async () => {
      try {
        const res = await fetch("/jobs/" + panel.dataset.job);
        if (!res.ok) { location.reload(); return; }
        const j = await res.json();
        $("#jobmsg").textContent = j.message || "";
        if (j.status === "done") { location.reload(); return; }
        if (j.status === "error") {
          panel.innerHTML = `<div class="flash err">Failed: ${j.error}</div>`;
          return;
        }
      } catch (e) { /* server restarting; keep trying */ }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 800);
  }

  // ---------------------------------------------------------------- 3MF drop zone
  function initDropzone() {
    const zone = $("#dropzone"), input = $("#file3mf"), form = $("#uploadform");
    if (!zone) return;
    const say = (big, sub) => { $("#droptext").textContent = big; if (sub !== undefined) $("#dropsub").textContent = sub; };
    const upload = (file) => {
      if (!file) return;
      if (!/\.3mf$/i.test(file.name)) { say("That isn't a .3mf file", "Drop a 3MF project file."); return; }
      zone.classList.add("busy");
      say(`Uploading ${file.name}…`, `${(file.size / 1048576).toFixed(1)} MB`);
      const fd = new FormData();
      fd.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", form.action);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) $("#dropbar").style.width = (100 * e.loaded / e.total) + "%"; };
      xhr.onload = () => { window.location = xhr.responseURL || "/"; };
      xhr.onerror = () => { zone.classList.remove("busy"); say("Upload failed. Try again?", ""); };
      xhr.send(fd);
    };
    ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "dragend"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("drag")));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("drag");
      upload(e.dataTransfer.files[0]);
    });
    input.addEventListener("change", () => upload(input.files[0]));
    // Dropping anywhere on the page also works.
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => { if (!zone.contains(e.target)) { e.preventDefault(); upload(e.dataTransfer.files[0]); } });
  }

  // ---------------------------------------------------------------- setup / settings: filament source
  function initSource() {
    const form = $("#sourceform");
    if (!form) return;
    const show = () => {
      const kind = (form.querySelector("input[name=kind]:checked") || {}).value;
      $$(".choice-detail", form).forEach((d) => { d.hidden = d.dataset.for !== kind; });
      $("#spoolman_url").required = kind === "spoolman";
    };
    $$("input[name=kind]", form).forEach((r) => r.addEventListener("change", show));
    show();
    const res = $("#testres");
    $("#testbtn").addEventListener("click", async () => {
      res.className = "small muted";
      res.textContent = "Connecting…";
      const fd = new FormData();
      fd.append("url", $("#spoolman_url").value);
      try {
        const j = await (await fetch("/api/spoolman/test", { method: "POST", body: fd })).json();
        if (j.ok) {
          $("#spoolman_url").value = j.url;
          res.className = "small q-good";
          res.textContent = `✓ Connected to Spoolman ${j.version || ""}: ${j.filaments} filament${j.filaments === 1 ? "" : "s"}, ${j.in_stock} with a spool in stock.`;
        } else {
          res.className = "small q-bad";
          res.textContent = j.error;
        }
      } catch (e) {
        res.className = "small q-bad";
        res.textContent = "Couldn't test the connection.";
      }
    });
  }

  // ---------------------------------------------------------------- filaments: add form + table
  // Same words as spoolmix.report.texture: filaments that don't print as one flat color.
  const SPECKLED = /marble|galaxy|sparkle|glitter|starlight|granite|confetti|speckle|terrazzo|nebula|\bflakes?\b/i;

  function initFilaments() {
    const brand = $("#f-brand"), material = $("#f-material"), name = $("#f-name");
    const hex = $("#f-hex"), color = $("#f-color"), dbid = $("#f-dbid");
    const box = $("#dbcolors"), grid = $("#dbgrid"), head = $("#dbcolors-head");
    const defaultMaterials = $("#dl-materials").innerHTML;
    let brands = [], colors = [], picked = null;
    const knownBrand = () => brands.find((b) => b.brand.toLowerCase() === brand.value.trim().toLowerCase());
    const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    fetch("/api/db/brands").then((r) => r.json()).then((list) => {
      brands = list;
      $("#dl-brands").innerHTML = list.map((b) => `<option value="${esc(b.brand)}">`).join("");
    }).catch(() => {});

    const setHex = (v, fromPicker) => {
      const h = normHex(v);
      if (!h) return;
      if (!fromPicker) color.value = h.toLowerCase();
      if (fromPicker || hex.value !== h) hex.value = h;
    };
    color.addEventListener("input", () => { setHex(color.value, true); dbid.value = ""; });
    hex.addEventListener("input", () => { setHex(hex.value, false); dbid.value = ""; });

    const loadMaterials = async () => {
      const b = knownBrand();
      if (!b) { $("#dl-materials").innerHTML = defaultMaterials; return; }
      const mats = await (await fetch("/api/db/materials?brand=" + encodeURIComponent(b.brand))).json();
      $("#dl-materials").innerHTML = mats.map((m) => `<option value="${esc(m)}">`).join("");
      if (mats.length === 1 && !material.value) { material.value = mats[0]; loadColors(); }
    };
    // Brand/material edits fire several overlapping loads: only the latest one may update the list.
    let colorsRequest = 0;
    const loadColors = async () => {
      const req = ++colorsRequest;
      const b = knownBrand();
      let found = [];
      if (b && material.value.trim()) {
        found = await (await fetch(`/api/db/colors?brand=${encodeURIComponent(b.brand)}&material=${encodeURIComponent(material.value.trim())}`)).json();
      }
      if (req !== colorsRequest) return;
      colors = found;
      renderColors();
    };
    const renderColors = () => {
      if (!colors.length) {
        box.hidden = !(knownBrand() && material.value.trim());
        head.textContent = box.hidden ? "" : `No ${material.value} colors listed for ${brand.value}. Type the color name and pick its color below.`;
        grid.innerHTML = "";
        return;
      }
      const q = picked ? "" : name.value.trim().toLowerCase();
      const shown = colors.filter((c) => !q || c.name.toLowerCase().includes(q));
      box.hidden = false;
      head.textContent = `${colors.length} ${knownBrand().brand} ${material.value} colors` + (q ? ` · ${shown.length} matching "${name.value}"` : "") + ". Click yours:";
      grid.innerHTML = shown.map((c) =>
        `<button type="button" class="dbcolor${picked && picked.id === c.id ? " on" : ""}" data-id="${esc(c.id)}" title="${esc(c.hex)}">` +
        `<span class="sw" style="background:${esc(c.hex)}"></span><span>${esc(c.name)}${c.translucent ? ' <span class="muted">· translucent</span>' : ""}${c.finish ? ` <span class="muted">· ${esc(c.finish)}</span>` : ""}${SPECKLED.test(c.name) ? ' <span class="tag tex">speckled</span>' : ""}</span></button>`).join("");
    };
    grid.addEventListener("click", (e) => {
      const btn = e.target.closest(".dbcolor");
      if (!btn) return;
      const hit = colors.find((c) => String(c.id) === btn.dataset.id);
      if (!hit) return;
      picked = hit;
      name.value = picked.name;
      setHex(picked.hex, false);
      dbid.value = picked.id;
      $("#texnote").hidden = !SPECKLED.test(picked.name);
      renderColors();
    });
    brand.addEventListener("change", () => { loadMaterials(); loadColors(); });
    brand.addEventListener("input", () => { if (knownBrand()) { loadMaterials(); loadColors(); } });
    material.addEventListener("change", loadColors);
    material.addEventListener("input", () => { if ([...$("#dl-materials").options].some((o) => o.value === material.value)) loadColors(); });
    name.addEventListener("input", () => {
      if (picked && name.value !== picked.name) { picked = null; dbid.value = ""; }
      $("#texnote").hidden = !SPECKLED.test(name.value);
      renderColors();
    });

    // Edit forms: keep color picker and hex box in sync.
    $$("input[type=color][data-hexfor]").forEach((c) => {
      const t = document.getElementById(c.dataset.hexfor);
      c.addEventListener("input", () => { t.value = c.value.toUpperCase(); });
      t.addEventListener("input", () => { const h = normHex(t.value); if (h) c.value = h.toLowerCase(); });
    });

    // Table: edit rows, filter and sort.
    const table = $("#ftable");
    if (!table) return;
    $$("button[data-edit]", table).forEach((b) => b.addEventListener("click", () => {
      const row = document.getElementById(b.dataset.edit);
      row.hidden = !row.hidden;
      b.textContent = row.hidden ? "Edit" : "Close";
    }));
    const rows = $$("tbody tr.frow", table);
    const editRow = (r) => document.getElementById("e" + r.id.slice(1));
    const filt = $("#tfilter"), mat = $("#tmaterial");
    const apply = () => {
      const q = filt.value.trim().toLowerCase();
      rows.forEach((r) => {
        const okQ = !q || r.textContent.toLowerCase().includes(q);
        const okM = !mat.value || r.dataset.material === mat.value;
        r.hidden = !(okQ && okM);
        if (r.hidden) editRow(r).hidden = true;
      });
    };
    filt.addEventListener("input", apply);
    mat.addEventListener("change", apply);
    let sortKey = null, dir = 1;
    $$("th[data-sort]", table).forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.sort;
      dir = sortKey === k ? -dir : 1;
      sortKey = k;
      $$("th[data-sort]", table).forEach((x) => x.classList.toggle("sorted", x === th));
      const num = k === "td" || k === "have";
      const tbody = $("tbody", table);
      rows.slice().sort((a, b) => {
        const x = a.dataset[k], y = b.dataset[k];
        return dir * (num ? parseFloat(x) - parseFloat(y) : x.localeCompare(y));
      }).forEach((r) => { tbody.appendChild(r); tbody.appendChild(editRow(r)); });
    }));
  }

  // ---------------------------------------------------------------- "Print with" template picker
  // Warns when the picked template's filament presets don't match the material you print in
  // (the design's, or the one chosen under "Print in").
  function initPrintWith() {
    const sel = $("select.print-with"), warn = $("#tplmismatch"), mat = $("#printmat");
    if (!sel || !warn) return;
    const show = () => {
      const cats = sel.selectedOptions[0] ? sel.selectedOptions[0].dataset.cats : "";
      const printing = (mat && mat.value) || warn.dataset.designed;
      const ok = !cats || !printing || printing.split("/").some((m) => cats.split("/").includes(m));
      warn.hidden = ok;
      $$(".mm-types", warn).forEach((e) => { e.textContent = cats; });
      $$(".mm-mat", warn).forEach((e) => { e.textContent = printing; });
    };
    sel.addEventListener("change", show);
    if (mat) mat.addEventListener("change", show);
    show();
  }

  // "Print it in PLA" on the design page: pick the material and plan.
  function printIn(material) {
    const mat = $("#printmat");
    if (!mat) return;
    mat.value = material;
    mat.dispatchEvent(new Event("change"));
    mat.form.requestSubmit();
  }

  return { initMatch, initRun, pollJob, initDropzone, initSource, initFilaments, initPrintWith, printIn };
})();
