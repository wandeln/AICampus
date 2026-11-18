// ─────────────────────────────────────────────────────────────────────
// PDF-Export mit Notizen (Stift-Notizen auf den Folien + Whiteboard-Seiten).
//   probe(courseId, deckId)  → { slides, board } | null  (gibt es druckbare Notizen?
//                               Studierende: nur wenn freigegeben, sonst null)
//   apply(opts)              → zeichnet die Notizen auf die Druckseiten (.pdf-page) und
//                               hängt die Whiteboard-Seiten hinten an.
// Nutzt dieselbe Zeichenfunktion wie der Student-Viewer (slide-live-viewer.js).
// Siehe docs/LLM-plans/slide-annotations-live-share.md.
// ─────────────────────────────────────────────────────────────────────
(function () {
  const WB = "_wb";   // Pseudo-Folie des globalen Whiteboards (mode 1, f = Seite)
  const hasInk = (r) => r.events.some((e) => e.type === "draw");

  async function load(courseId, deckId) {
    try {
      const r = await fetch(`/api/courses/${courseId}/slides/${deckId}/annotations`, { credentials: "same-origin" });
      if (!r.ok) return null;
      return (await r.json()).rows;
    } catch (e) {
      return null;
    }
  }

  async function probe(courseId, deckId) {
    const rows = await load(courseId, deckId);
    if (!rows) return null;
    const slides = rows.some((r) => r.mode === 0 && r.uid !== WB && hasInk(r));
    const board = rows.some((r) => r.mode === 1 && r.uid === WB && hasInk(r));
    return slides || board ? { slides, board } : null;
  }

  // Events in ein Canvas (CSS-Größe W×H, Config-Raum 1:1, 2× Auflösung) zeichnen
  function paint(canvas, events, mode, white, W, H) {
    canvas.width = W * 2;
    canvas.height = H * 2;
    canvas.style.cssText = `position:absolute;left:0;top:0;width:${W}px;height:${H}px;pointer-events:none`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(2, 0, 0, 2, 0, 0);
    const G = { sc: 1, xo: 0, yo: 0 };
    for (const e of events) {
      if (e.type === "clear") ctx.clearRect(0, 0, W, H);
      else window.aicampusLiveViewer.drawEvent(ctx, mode, e, G, white);
    }
  }

  function drawGrid(canvas, W, H, g) {
    canvas.width = W * 2;
    canvas.height = H * 2;
    canvas.style.cssText = `position:absolute;left:0;top:0;width:${W}px;height:${H}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(2, 0, 0, 2, 0, 0);
    const d = g.distance;
    ctx.strokeStyle = g.color;
    ctx.lineWidth = g.width;
    ctx.lineCap = "round";
    const fx = W / 2 - d / 2 - Math.floor((W - d) / 2 / d) * d;
    for (let x = fx; x < W; x += d) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    const fy = H / 2 - d / 2 - Math.floor((H - d) / 2 / d) * d;
    for (let y = fy; y < H; y += d) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  }

  /**
   * opts: { courseId, deckId, slidesEl, slideW, slideH, board ("whiteboard"|"chalkboard"), boardBg (URL) }
   * Voraussetzung: Reveal hat das Druck-Layout gebaut (.pdf-page je Folie bzw. Fragment-Schritt).
   */
  async function apply(opts) {
    const rows = await load(opts.courseId, opts.deckId);
    if (!rows) return;
    const white = opts.board === "whiteboard";
    const { slideW: W, slideH: H, slidesEl } = opts;
    const byKey = new Map(rows.map((r) => [r.uid + "|" + r.mode + "|" + r.f, r]));

    // Folien: Reveal legt je Folie eine Seite an, bei Fragmenten je Schritt eine weitere
    // (Reihenfolge: ohne Fragment = f -1, danach f 0, 1, …).
    const seen = new Map();
    for (const page of slidesEl.querySelectorAll(":scope > .pdf-page")) {
      const sec = page.querySelector("section[data-slide-uid]");
      if (!sec) continue;
      const uid = sec.getAttribute("data-slide-uid");
      const n = seen.get(uid) || 0;
      seen.set(uid, n + 1);
      const row = byKey.get(uid + "|0|" + (n === 0 ? -1 : n - 1));
      if (!row || !hasInk(row)) continue;
      const box = document.createElement("div");
      box.style.cssText = `position:absolute;left:${parseFloat(sec.style.left) || 0}px;top:${parseFloat(sec.style.top) || 0}px;width:${W}px;height:${H}px;pointer-events:none`;
      const cv = document.createElement("canvas");
      paint(cv, row.events, 0, white, W, H);
      box.appendChild(cv);
      page.appendChild(box);
    }

    // Whiteboard-Seiten hinten anhängen (je Seite eine PDF-Seite, aufsteigend)
    const pages = rows.filter((r) => r.uid === WB && r.mode === 1 && hasInk(r)).sort((a, b) => a.f - b.f);
    for (const r of pages) {
      const page = document.createElement("div");
      page.className = "pdf-page";
      page.style.height = (opts.pageH || H) + "px";
      const bg = document.createElement("div");
      bg.style.cssText = `position:absolute;left:0;top:0;width:${W}px;height:${H}px;overflow:hidden;`
        + `background:url("${opts.boardBg}") repeat;print-color-adjust:exact;-webkit-print-color-adjust:exact`;
      const grid = document.createElement("canvas");
      drawGrid(grid, W, H, window.aicampusLiveViewer.GRIDS[white ? "whiteboard" : "chalkboard"]);
      const ink = document.createElement("canvas");
      paint(ink, r.events, 1, white, W, H);
      // Seite ohne sichtbare Tinte (alles gelöscht/außerhalb) weglassen
      const px = ink.getContext("2d").getImageData(0, 0, ink.width, ink.height).data;
      if (!px.some((v, i) => i % 4 === 3 && v)) continue;
      const label = document.createElement("div");
      label.textContent = "Whiteboard – Seite " + (r.f + 1);
      label.style.cssText = `position:absolute;right:14px;bottom:10px;font:14px sans-serif;color:${white ? "#888" : "#bbb"}`;
      bg.append(grid, ink, label);
      page.appendChild(bg);
      slidesEl.appendChild(page);
    }
  }

  // ── PDF-Links (Deck-Liste, Editor, Präsentation): gibt es druckbare Notizen
  // (Studierende: freigegeben), erst fragen: mit / ohne Notizen.
  function open(url) {
    if (!window.open(url, "_blank")) location.href = url;   // Popup-Blocker → im selben Tab
  }

  function ask(url, info) {
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;font-family:sans-serif";
    const card = document.createElement("div");
    card.style.cssText = "background:#fff;color:#111;border-radius:12px;padding:20px 24px;max-width:420px;box-shadow:0 10px 40px rgba(0,0,0,.4)";
    card.innerHTML = '<div style="font-weight:600;font-size:17px;margin-bottom:8px">Notizen mitdrucken?</div>'
      + '<div style="font-size:14px;color:#444;margin-bottom:16px">Dieses Deck enthält Notizen'
      + (info.board ? ' (Whiteboard-Seiten werden hinten an das PDF angehängt)' : '') + '.</div>';
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:8px;justify-content:flex-end";
    const btn = (txt, primary, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = txt;
      b.style.cssText = "padding:6px 14px;border-radius:8px;font-size:14px;cursor:pointer;border:1px solid #ccc;background:"
        + (primary ? "#2563eb;color:#fff;border-color:#2563eb" : "#fff;color:#111");
      b.addEventListener("click", () => { box.remove(); fn(); });
      return b;
    };
    row.append(
      btn("Abbrechen", false, () => {}),
      btn("Ohne Notizen", false, () => open(url)),
      btn("Mit Notizen", true, () => open(url + "&withnotes=1")),
    );
    card.appendChild(row);
    box.appendChild(card);
    box.addEventListener("click", (ev) => { if (ev.target === box) box.remove(); });
    document.body.appendChild(box);
  }

  document.addEventListener("click", async (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const a = e.target.closest && e.target.closest("a[href]");
    const m = a && a.getAttribute("href").match(/^\/courses\/(\d+)\/slides\/(\d+)\/present\?print-pdf$/);
    if (!m) return;
    e.preventDefault();
    const info = await probe(m[1], m[2]);
    if (info) ask(a.href, info); else open(a.href);
  });

  window.aicampusNotesPrint = { probe, apply };
})();
