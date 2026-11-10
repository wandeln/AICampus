/* Client-seitiges Bild-Zuschneiden (Canvas) + Modal.
   Unabhängig von State: ImageCrop.open({ url, mime, onCropped(blob), onError(msg) }).
   Wird von Medienübersicht (Bearbeiten → Zuschneiden) und Media-Studio verwendet,
   damit beide exakt dasselbe Crop-Verhalten haben (DRY). */
(function () {
  const MAX_W = 860, MAX_H = 560, MIN = 8;
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

  let modal = null, stage = null, info = null;
  let cropImg = null;      // <img> in Originalauflösung
  let cropScale = 1;       // Anzeige-px → Original-px
  let cropSel = { x: 0, y: 0, w: 0, h: 0 };  // in Anzeige-px
  let cropDrag = null;
  let onCropped = null, onError = null, cropMime = "image/png";

  function fail(msg) { if (onError) onError(msg); else alert(msg); }

  function ensureDom() {
    if (modal) return;
    modal = document.createElement("div");
    modal.className = "hidden fixed inset-0 bg-black/60 flex items-center justify-center z-[60]";
    modal.innerHTML =
      '<div class="bg-white rounded-2xl shadow-2xl w-full max-w-4xl p-6 mx-4 max-h-[92vh] flex flex-col">' +
      '  <h2 class="text-xl font-bold text-gray-800 mb-4">Bild zuschneiden</h2>' +
      '  <div class="flex-1 min-h-0 flex items-center justify-center bg-gray-100 rounded-lg overflow-hidden">' +
      '    <div data-stage class="relative select-none overflow-hidden" style="touch-action: none;"></div>' +
      '  </div>' +
      '  <div class="flex items-center justify-between gap-2 mt-2">' +
      '    <p data-hint class="text-xs text-gray-400">Rechteck ziehen bzw. Griffe/Innenbereich verschieben</p>' +
      '    <p data-info class="text-xs text-gray-500"></p>' +
      '  </div>' +
      '  <div class="flex items-center justify-end gap-3 mt-4">' +
      '    <button data-cancel type="button" class="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 transition">Abbrechen</button>' +
      '    <button data-confirm type="button" class="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-blue-700 transition font-semibold">✂️ Zuschneiden</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(modal);
    stage = modal.querySelector("[data-stage]");
    info = modal.querySelector("[data-info]");
    modal.querySelector("[data-cancel]").addEventListener("click", close);
    modal.querySelector("[data-confirm]").addEventListener("click", doCrop);
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    stage.addEventListener("pointerdown", onDown);
    stage.addEventListener("pointermove", onMove);
    stage.addEventListener("pointerup", endCropDrag);
    stage.addEventListener("pointercancel", endCropDrag);
  }

  function open(opts) {
    ensureDom();
    onCropped = opts.onCropped || null;
    onError = opts.onError || null;
    cropMime = opts.mime || "image/png";
    cropImg = new Image();
    cropImg.onload = () => {
      // Erst anzeigen, dann im nächsten Frame die tatsächlich verfügbare Größe
      // messen (display:none → clientWidth wäre sonst 0).
      modal.classList.remove("hidden");
      requestAnimationFrame(buildCropStage);
    };
    cropImg.onerror = () => fail("Bild konnte nicht geladen werden.");
    cropImg.src = opts.url + (opts.url.includes("?") ? "&" : "?") + "t=" + Date.now();
  }

  function close() {
    cropDrag = null;
    if (modal) modal.classList.add("hidden");
    if (stage) stage.innerHTML = "";
    cropImg = null;
  }

  function buildCropStage() {
    // Always „contain“: das ganze Bild muss sichtbar sein (sonst greift auf
    // schmalen Viewports der Container-Overflow ins Bild).
    const box = stage.parentElement;
    const availW = Math.min(MAX_W, box && box.clientWidth > 0 ? box.clientWidth : window.innerWidth - 80);
    const availH = Math.min(MAX_H, Math.max(160, window.innerHeight * 0.45));
    cropScale = Math.min(availW / cropImg.naturalWidth, availH / cropImg.naturalHeight, 1);
    const w = Math.round(cropImg.naturalWidth * cropScale);
    const h = Math.round(cropImg.naturalHeight * cropScale);
    stage.innerHTML = "";
    stage.style.width = w + "px";
    stage.style.height = h + "px";
    cropImg.className = "block max-w-none";
    cropImg.style.width = w + "px";
    cropImg.style.height = h + "px";
    stage.appendChild(cropImg);
    cropSel = { x: w * 0.1, y: h * 0.1, w: w * 0.8, h: h * 0.8 };
    renderCropSel();
    updateCropInfo();
  }

  function renderCropSel() {
    stage.querySelectorAll("[data-crop-ui]").forEach((el) => el.remove());
    const sel = document.createElement("div");
    sel.dataset.cropUi = "1";
    Object.assign(sel.style, {
      position: "absolute",
      left: cropSel.x + "px", top: cropSel.y + "px",
      width: cropSel.w + "px", height: cropSel.h + "px",
      border: "1px solid rgba(255,255,255,0.9)",
      boxShadow: "0 0 0 9999px rgba(0,0,0,0.5)",
      cursor: "move",
    });
    const H = 12;
    const handles = {
      nw: [0, 0, "nwse-resize"], n: [0.5, 0, "ns-resize"], ne: [1, 0, "nesw-resize"],
      e: [1, 0.5, "ew-resize"], se: [1, 1, "nwse-resize"], s: [0.5, 1, "ns-resize"],
      sw: [0, 1, "nesw-resize"], w: [0, 0.5, "ew-resize"],
    };
    for (const [name, [fx, fy, cursor]] of Object.entries(handles)) {
      const hd = document.createElement("div");
      hd.dataset.cropUi = "1";
      hd.dataset.handle = name;
      Object.assign(hd.style, {
        position: "absolute", width: H + "px", height: H + "px",
        left: `calc(${fx * 100}% - ${H / 2}px)`, top: `calc(${fy * 100}% - ${H / 2}px)`,
        backgroundColor: "#fff", border: "1px solid #2563eb",
        borderRadius: "2px", cursor,
      });
      sel.appendChild(hd);
    }
    stage.appendChild(sel);
  }

  function updateCropInfo() {
    if (!cropImg) return;
    const w = Math.max(1, Math.round(cropSel.w / cropScale));
    const h = Math.max(1, Math.round(cropSel.h / cropScale));
    info.textContent = `Ausschnitt: ${w} × ${h} px (Original: ${cropImg.naturalWidth} × ${cropImg.naturalHeight} px)`;
  }

  function onDown(e) {
    if (!cropImg) return;
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const startSel = { ...cropSel };
    const handle = e.target.dataset && e.target.dataset.handle;
    if (handle) {
      cropDrag = { mode: "resize", handle, px, py, startSel };
    } else if (px >= cropSel.x && px <= cropSel.x + cropSel.w &&
               py >= cropSel.y && py <= cropSel.y + cropSel.h) {
      cropDrag = { mode: "move", px, py, startSel };
    } else {
      cropDrag = { mode: "new", px, py, startSel };
      cropSel = { x: px, y: py, w: 0, h: 0 };
      renderCropSel();
    }
    stage.setPointerCapture(e.pointerId);
  }

  function onMove(e) {
    if (!cropDrag) return;
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    const W = stage.clientWidth, H = stage.clientHeight;
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const s = cropDrag.startSel;
    if (cropDrag.mode === "move") {
      cropSel = {
        x: clamp(s.x + px - cropDrag.px, 0, W - s.w),
        y: clamp(s.y + py - cropDrag.py, 0, H - s.h),
        w: s.w, h: s.h,
      };
    } else if (cropDrag.mode === "resize") {
      let { x, y } = s;
      let x2 = s.x + s.w, y2 = s.y + s.h;
      const h = cropDrag.handle;
      if (h.includes("e")) x2 = clamp(px, s.x + MIN, W);
      if (h.includes("s")) y2 = clamp(py, s.y + MIN, H);
      if (h.includes("w")) x = clamp(px, 0, x2 - MIN);
      if (h.includes("n")) y = clamp(py, 0, y2 - MIN);
      cropSel = { x, y, w: x2 - x, h: y2 - y };
    } else { // new
      cropSel = {
        x: clamp(Math.min(cropDrag.px, px), 0, W),
        y: clamp(Math.min(cropDrag.py, py), 0, H),
        w: clamp(Math.abs(px - cropDrag.px), 0, W),
        h: clamp(Math.abs(py - cropDrag.py), 0, H),
      };
    }
    renderCropSel();
    updateCropInfo();
  }

  function endCropDrag() {
    // Kleinklick statt Ziehen (neues Rechteck zu klein) → Auswahl wiederherstellen
    if (cropDrag && cropDrag.mode === "new" &&
        (cropSel.w < MIN || cropSel.h < MIN) && cropDrag.startSel) {
      cropSel = { ...cropDrag.startSel };
      renderCropSel();
      updateCropInfo();
    }
    cropDrag = null;
  }

  // Sicherheitsmargin unter dem 5-MB-Upload-Limit. Ein Zuschnitt eines großen
  // PNGs kann sonst als PNG > 5 MB werden und beim Speichern abgelehnt werden.
  const MAX_CROP_BYTES = 4.5 * 1024 * 1024;

  const canvasBlob = (canvas, mime, q) => new Promise((res) => canvas.toBlob(res, mime, q));
  const makeScaled = (src, w, h) => {
    const s = document.createElement("canvas");
    s.width = w; s.height = h;
    s.getContext("2d").drawImage(src, 0, 0, w, h);
    return s;
  };
  // 128×128-Abtastung: hat das Bild (relevant) Transparenz?
  function hasAlpha(canvas) {
    const w = Math.min(128, canvas.width), h = Math.min(128, canvas.height);
    const s = document.createElement("canvas");
    s.width = w; s.height = h;
    const c = s.getContext("2d");
    c.drawImage(canvas, 0, 0, w, h);
    const d = c.getImageData(0, 0, w, h).data;
    for (let i = 3; i < d.length; i += 4) if (d[i] < 255) return true;
    return false;
  }
  // Zuschnitt exportieren, so dass er unter MAX_CROP_BYTES bleibt:
  // 1) Originalformat (Diagramme/Screenshots bleiben z. B. PNG) →
  // 2) bei Transparenz-freien Fotos als JPEG (deutlich kleiner) →
  // 3) notfalls runterskalieren.
  async function exportCrop(canvas) {
    let blob = await canvasBlob(canvas, cropMime, 0.92);
    if (blob && blob.size <= MAX_CROP_BYTES) return { blob, mime: cropMime };
    const alpha = hasAlpha(canvas);
    if (!alpha) {
      const j = await canvasBlob(canvas, "image/jpeg", 0.9);
      if (j && j.size <= MAX_CROP_BYTES) return { blob: j, mime: "image/jpeg" };
    }
    const mime = !alpha ? "image/jpeg" : (cropMime === "image/webp" ? "image/webp" : "image/png");
    let cur = canvas, w = canvas.width, h = canvas.height;
    for (let i = 0; i < 8; i++) {
      w = Math.max(160, Math.round(w * 0.85));
      h = Math.max(160, Math.round(h * 0.85));
      cur = makeScaled(cur, w, h);
      const b = await canvasBlob(cur, mime, 0.9);
      if (b && b.size <= MAX_CROP_BYTES) return { blob: b, mime };
      if (w <= 160 && h <= 160) break;
    }
    return { blob, mime };
  }

  function doCrop() {
    if (!cropImg) return;
    const sx = Math.max(0, Math.round(cropSel.x / cropScale));
    const sy = Math.max(0, Math.round(cropSel.y / cropScale));
    const sw = Math.min(cropImg.naturalWidth - sx, Math.round(cropSel.w / cropScale));
    const sh = Math.min(cropImg.naturalHeight - sy, Math.round(cropSel.h / cropScale));
    if (sw < 2 || sh < 2) { fail("Ausschnitt zu klein."); return; }

    const canvas = document.createElement("canvas");
    canvas.width = sw; canvas.height = sh;
    canvas.getContext("2d").drawImage(cropImg, sx, sy, sw, sh, 0, 0, sw, sh);

    exportCrop(canvas)
      .then(({ blob, mime }) => {
        if (!blob) { fail("Zuschnitt konnte nicht erzeugt werden."); return; }
        const cb = onCropped;
        close();
        if (cb) cb(blob, mime);
      })
      .catch(() => fail("Zuschnitt konnte nicht erzeugt werden."));
  }

  window.ImageCrop = { open, close };
})();
