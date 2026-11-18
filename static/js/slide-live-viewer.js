// ─────────────────────────────────────────────────────────────────────
// Live-Notizen: Viewer (Studierende) + Laser-Overlay + „Presenter folgen“.
//
// - Student: zeichnet freigegebene Notizen (Stift = Mode 0, Whiteboard = Mode 1)
//   live auf eigene Canvases; Toggle (Auge) blendet alles aus/ein.
// - Tutor (opts.tutor): KEINE Canvases (die zeichnet das Chalkboard-Plugin),
//   nur Laser anderer Tutoren + Folgen-Dropdown.
// - Folgen: Dropdown mit einem Eintrag je Tutor, der gerade im Deck ist.
//
// Rendering = Kopie der Plugin-Funktionen (static/vendor/reveal/plugin/chalkboard/
// plugin.js: drawWithBoardmarker/drawWithChalk/eraseWithSponge) — in Sync halten!
// Geometrie wie das Plugin: Config-Raum (SLIDE_W×SLIDE_H) mit min-Skala, zentriert.
// Siehe docs/LLM-plans/slide-annotations-live-share.md.
// ─────────────────────────────────────────────────────────────────────
(function () {
  const L = window.aicampusLive;
  const MARKERS = ["rgba(100,100,100,1)", "rgba(30,144,255,1)", "rgba(220,20,60,1)", "rgba(50,205,50,1)",
    "rgba(255,140,0,1)", "rgba(150,0,150,1)", "rgba(255,220,0,1)"];
  const CHALKS = ["rgba(255,255,255,1)", "rgba(96,154,244,1)", "rgba(237,20,28,1)", "rgba(20,237,28,1)",
    "rgba(220,133,41,1)", "rgba(220,0,220,1)", "rgba(255,220,0,1)"];
  const MARKER_W = 3, CHALK_W = 7, ERASER_R = 20;
  const PEN_REF = 1.5;   // Skala, bei der die Breiten den Pixelwerten entsprechen (= plugin.js penK)
  const GRIDS = { chalkboard: { color: "rgba(50,50,10,0.5)", distance: 80, width: 2 },
    whiteboard: { color: "rgba(127,127,255,0.1)", distance: 40, width: 2 } };
  const LASER_STALE_MS = 2000;

  // ── Rendering (Kopie der Plugin-Funktionen) ──────────────────────
  /** white: Whiteboard (Filzstift) statt Blackboard (Kreide) im Modus 1; G = {sc, xo, yo} (Config-Raum → Pixel) */
  function drawEvent(ctx, mode, e, G, white) {
    const X = (x) => G.xo + x * G.sc, Y = (y) => G.yo + y * G.sc;
    if (e.type === "draw") {
      ctx.lineCap = "round";
      if (mode === 0 || white) {
        ctx.lineWidth = MARKER_W * G.sc / PEN_REF;
        ctx.strokeStyle = MARKERS[e.color] || MARKERS[0];
        ctx.beginPath(); ctx.moveTo(X(e.x1), Y(e.y1)); ctx.lineTo(X(e.x2), Y(e.y2)); ctx.stroke();
      } else {
        chalk(ctx, X(e.x1), Y(e.y1), X(e.x2), Y(e.y2), e.color, G.sc / PEN_REF);
      }
    } else if (e.type === "erase") {
      const x = X(e.x), y = Y(e.y), R = ERASER_R * G.sc / PEN_REF;
      ctx.save();
      ctx.beginPath(); ctx.arc(x, y, R, 0, 2 * Math.PI, false); ctx.clip();
      ctx.clearRect(x - R - 1, y - R - 1, R * 2 + 2, R * 2 + 2);
      ctx.restore();
    }
  }
  function chalk(ctx, fx, fy, tx, ty, idx, k) {
    const W = CHALK_W * k;
    ctx.lineWidth = W;
    ctx.lineCap = "round";
    ctx.strokeStyle = CHALKS[idx] || CHALKS[0];
    ctx.beginPath(); ctx.moveTo(fx, fy); ctx.lineTo(tx, ty); ctx.stroke();
    const len = Math.round(Math.hypot(tx - fx, ty - fy) / (5 / W));
    const xu = (tx - fx) / len, yu = (ty - fy) / len;
    for (let i = 0; i < len; i++) {
      if (1.0 > Math.random() * 0.9) {
        const xr = fx + i * xu + (Math.random() - 0.5) * W * 1.2;
        const yr = fy + i * yu + (Math.random() - 0.5) * W * 1.2;
        ctx.clearRect(xr, yr, Math.random() * 2 + 2, Math.random() + 1);
      }
    }
  }

  // Auge: dieselben Icons wie der Sichtbarkeits-Schalter (Kapitel/Folien/Aufgaben, script.html)
  const EYE = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>';
  const EYE_OFF = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a16.54 16.54 0 0 1 4.1-5.79"></path><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.55 18.55 0 0 1-2.06 3.28"></path><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';
  const BOARD = '<svg viewBox="0 0 16 16" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1.5" y="2.5" width="13" height="9" rx="1"></rect><path d="M1.5 13.5h13"></path></svg>';

  /**
   * opts: { reveal, root (#reveal-root), tutor, deckId, assetBoard (Whiteboard-Textur-URL),
   *         selfUserId }
   * Pro Mount (Reveal-Instanz) aufrufen; baut die DOM-Teile neu (alte werden entfernt).
   */
  function mount(opts) {
    const { reveal, root, tutor } = opts;
    const WHITE = opts.board === "whiteboard";   // Whiteboard: Filzstift statt Kreide
    const GRID = GRIDS[WHITE ? "whiteboard" : "chalkboard"];
    // Remount: alte DOM-Teile + Listener des vorigen Mounts entfernen
    root.querySelectorAll(".slive-root, .slive-ui, .slive-laser-box").forEach((el) => el.remove());
    (window._sliveOffs || []).forEach((f) => f());
    const offs = (window._sliveOffs = []);
    const on = (t, fn) => offs.push(L.on(t, fn));

    const LS_KEY = "aicampus-live-notes-" + opts.deckId;
    let visible = localStorage.getItem(LS_KEY) !== "0";
    const cache = new Map();        // uid → Map("mode|f" → {rev, events[]})
    let board = 0;                  // aktuelles Whiteboard-Board (folgt dem Tutor)
    let wbManual = false;           // Student hat „Whiteboard“ selbst geöffnet
    let followUid = null;           // User-ID des gefolgten Tutors
    let navLock = 0;                // programmatische Navigation läuft (kein Auto-Unfollow)
    let stage = null, notes = null, wb = null, wbNum = null, grid = null, laserBox = null;
    let nctx = null, wctx = null;

    // ── DOM ──────────────────────────────────────────────────────────
    const ui = document.createElement("div");
    ui.className = "slive-ui" + (tutor ? " slive-tutor" : "");
    root.appendChild(ui);

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    const boardBtn = document.createElement("button");
    boardBtn.type = "button";
    boardBtn.className = "slive-btn";
    boardBtn.title = "Whiteboard dieser Folie anzeigen";
    boardBtn.innerHTML = BOARD;
    const select = document.createElement("select");
    select.className = "slive-select";
    select.title = "Einem Dozenten folgen";

    toggleBtn.className = "slive-btn";
    if (tutor) {
      // Tutor: Auge = Notizen für Studierende freigeben / beenden
      toggleBtn.addEventListener("click", () => L.setShared(!L.shared));
      ui.appendChild(toggleBtn);
    } else {
      toggleBtn.addEventListener("click", () => {
        visible = !visible;
        localStorage.setItem(LS_KEY, visible ? "1" : "0");
        refreshUi();
        render();
        if (visible) ensureLoaded();
      });
      boardBtn.addEventListener("click", () => {
        if (wbState().show) { wbManual = false; followUid = null; }  // zurück zu den Slides, Folgen beenden
        else wbManual = true;
        refreshUi(); render();
      });
      ui.append(toggleBtn, boardBtn);
    }
    ui.appendChild(select);
    // Fokus nach Klick abgeben: kein Fokusrahmen, und Leertaste/Enter/Pfeile gehen nicht an den Button
    ui.addEventListener("click", (e) => e.target.closest?.("button")?.blur());

    // Laser-Overlay (Tutor + Student): Punkte + Spur
    laserBox = document.createElement("div");
    laserBox.className = "slive-laser-box";
    root.appendChild(laserBox);

    if (!tutor) {
      stage = document.createElement("div");
      stage.className = "slive-root";
      notes = document.createElement("canvas");
      notes.className = "slive-notes";
      const wbWrap = document.createElement("div");
      wbWrap.className = "slive-wb";
      wbWrap.style.backgroundImage = `url("${opts.assetBoard}")`;
      grid = document.createElement("canvas");
      wb = document.createElement("canvas");
      // Seiten-Leiste: identisches Markup + CSS wie beim Tutor (chalkboard/style.css, .boardhandle)
      const wbNav = document.createElement("div");
      wbNav.className = "boardhandle";
      wbNav.innerHTML = '<ul><li><a href="#" title="Vorherige Seite">&#8593;</a></li><li class="boardnum" title="Whiteboard-Seite">1</li><li><a href="#" title="Nächste Seite">&#8595;</a></li></ul>';
      const [up, down] = wbNav.querySelectorAll("a");
      up.addEventListener("click", (e) => { e.preventDefault(); stepBoard(-1); });
      down.addEventListener("click", (e) => { e.preventDefault(); stepBoard(1); });
      wbNum = wbNav.querySelector(".boardnum");
      wbWrap.append(grid, wb, wbNav);
      stage.append(notes, wbWrap);
      // vor den UI-Elementen einfügen (Overlay unter den Buttons)
      root.insertBefore(stage, ui);
      nctx = notes.getContext("2d");
      wctx = wb.getContext("2d");
      stage._wb = wbWrap;
    }

    // ── Geometrie ────────────────────────────────────────────────────
    const size = () => ({ w: root.clientWidth || window.innerWidth, h: root.clientHeight || window.innerHeight });
    function geo() {
      const { w, h } = size();
      const sc = Math.min(w / opts.slideW, h / opts.slideH);
      return { w, h, sc, xo: (w - opts.slideW * sc) / 2, yo: (h - opts.slideH * sc) / 2 };
    }
    function sizeCanvases() {
      if (tutor) return;
      const { w, h } = size();
      const dpr = window.devicePixelRatio || 1;
      for (const c of [notes, wb, grid]) {
        c.width = w * dpr; c.height = h * dpr;
        c.style.width = w + "px"; c.style.height = h + "px";
        c.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      drawGrid();
    }
    function drawGrid() {
      const g = grid.getContext("2d");
      const { w, h, sc } = geo();
      g.clearRect(0, 0, w, h);
      const d = GRID.distance * sc;
      g.strokeStyle = GRID.color;
      g.lineWidth = GRID.width * sc;
      g.lineCap = "round";
      const fx = w / 2 - d / 2 - Math.floor((w - d) / 2 / d) * d;
      for (let x = fx; x < w; x += d) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); }
      const fy = h / 2 - d / 2 - Math.floor((h - d) / 2 / d) * d;
      for (let y = fy; y < h; y += d) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
    }

    // ── Aktuelle Position ────────────────────────────────────────────
    function pos() {
      const i = reveal.getIndices();
      return { h: i.h || 0, v: i.v || 0, f: i.f == null ? -1 : i.f, uid: L.uidFor(i.h || 0, i.v || 0) };
    }
    const row = (uid, mode, f) => (cache.get(uid) || new Map()).get(mode + "|" + f);
    // Whiteboard ist global (Pseudo-Folie L.WB, eine Zeile je Seite)
    const wbRows = () => [...(cache.get(L.WB) || new Map()).entries()].filter(([k]) => k.startsWith("1|")).map(([, r]) => r);
    const wbHasInk = () => wbRows().some((r) => r.events.length > 0);

    // Whiteboard sichtbar? Folgen: wie der gefolgte Tutor (im Whiteboard + dessen Seite).
    // Sonst nur, wenn der Student es selbst per Button geöffnet hat.
    function wbState() {
      if (followUid != null) {
        const t = L.presence.find((e) => e.uid === followUid);
        return t && t.mode === 1 ? { show: true, board: t.board || 0 } : { show: false, board };
      }
      return wbManual && wbHasInk() ? { show: true, board } : { show: false, board };
    }

    function render() {
      if (tutor) return;
      const G = geo(), p = pos();
      const on = visible && L.shared && !!p.uid;
      notes.style.display = on ? "" : "none";
      nctx.clearRect(0, 0, G.w, G.h);
      wctx.clearRect(0, 0, G.w, G.h);
      const ws_ = on ? wbState() : { show: false };
      stage._wb.style.display = ws_.show ? "" : "none";
      if (!on) return;
      const r0 = row(p.uid, 0, p.f);
      if (r0) for (const e of r0.events) { if (e.type === "clear") nctx.clearRect(0, 0, G.w, G.h); else drawEvent(nctx, 0, e, G, WHITE); }
      if (ws_.show) {
        board = ws_.board;
        wbNum.textContent = String(board + 1);
        const r1 = row(L.WB, 1, board);
        if (r1) for (const e of r1.events) {
          if (e.type === "clear") wctx.clearRect(0, 0, G.w, G.h); else drawEvent(wctx, 1, e, G, WHITE);
        }
      }
    }

    // Whiteboard-Seite wechseln (Pfeil-Buttons / ↑ ↓): nur bis zur letzten beschriebenen Seite;
    // beendet das Folgen (Student bleibt im Whiteboard, auf eigener Seite)
    function stepBoard(d) {
      if (!wbState().show) return;
      const pages = [...(cache.get(L.WB) || new Map()).entries()]
        .filter(([k, r]) => k.startsWith("1|") && r.events.length).map(([k]) => Number(k.slice(2)));
      const nb = board + d;
      if (nb < 0 || nb > Math.max(0, board, ...pages)) return;
      if (followUid != null) { followUid = null; wbManual = true; }
      board = nb;
      refreshUi();
      render();
    }
    const onKey = (e) => {
      if ((e.keyCode !== 38 && e.keyCode !== 40) || e.altKey || e.ctrlKey || e.metaKey
        || /^(INPUT|SELECT|TEXTAREA)$/.test(e.target?.tagName || "") || !visible || !L.shared || !wbState().show) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      stepBoard(e.keyCode === 40 ? 1 : -1);
    };
    if (!tutor) {
      document.addEventListener("keydown", onKey, true);
      offs.push(() => document.removeEventListener("keydown", onKey, true));
    }

    // ── Laden ────────────────────────────────────────────────────────
    async function ensureLoaded() {
      if (tutor || !visible || !L.shared) return;
      const p = pos();
      if (!p.uid) return;
      await Promise.all([p.uid, L.WB].map(loadUid));   // Folie + globales Whiteboard
      refreshUi();
      render();
    }
    async function loadUid(uid) {
      if (cache.has(uid)) return;
      L.beginLoad(uid);
      try {
        const r = await fetch(`/api/courses/${opts.courseId}/slides/${opts.deckId}/annotations/slide?uid=${encodeURIComponent(uid)}`,
          { credentials: "same-origin" });
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        const m = new Map();
        for (const x of d.rows) m.set(x.mode + "|" + x.f, { rev: x.rev, events: x.events });
        cache.set(uid, m);
        L.markLoaded(uid, d.rows);
      } catch (e) {
        L.endLoad(uid);
      }
    }

    // ── UI-Zustand ───────────────────────────────────────────────────
    function refreshUi() {
      if (tutor) {
        toggleBtn.innerHTML = L.shared ? EYE : EYE_OFF;
        toggleBtn.title = L.shared ? "Notizen sind für Studierende freigegeben (klicken zum Beenden)" : "Notizen für Studierende freigeben";
        toggleBtn.classList.toggle("slive-off", !L.shared);
      } else {
        toggleBtn.style.display = L.shared ? "" : "none";
        toggleBtn.innerHTML = visible ? EYE : EYE_OFF;
        toggleBtn.title = visible ? "Notizen des Dozenten ausblenden" : "Notizen des Dozenten einblenden";
        toggleBtn.classList.toggle("slive-off", !visible);
        const wbOn = visible && L.shared && wbState().show;
        boardBtn.style.display = visible && L.shared && (wbHasInk() || wbOn) ? "" : "none";
        boardBtn.classList.toggle("slive-on", wbOn);
      }
      // Folgen-Dropdown: eine Zeile je Tutor (außer mir); Studierende nur wenn freigegeben
      const others = L.presence.filter((e) => e.uid !== opts.selfUserId);
      const show = (tutor || L.shared) && (others.length > 0 || followUid != null);
      select.style.display = show ? "" : "none";
      select.innerHTML = "";
      const none = new Option("Niemandem folgen", "");
      select.add(none);
      for (const e of others) {
        const hv = L.hvFor(e.slide);
        const n = hv ? ` (Folie ${hv.h + 1}${hv.v ? "." + (hv.v + 1) : ""})` : "";
        select.add(new Option("Folgen: " + e.name + n, String(e.uid)));
      }
      select.value = followUid != null && others.some((e) => e.uid === followUid) ? String(followUid) : "";
    }

    // ── Folgen ───────────────────────────────────────────────────────
    function goToFollowed() {
      if (followUid == null) return;
      const e = L.presence.find((x) => x.uid === followUid);
      if (!e) { followUid = null; toast("Dozent hat das Deck verlassen – Folgen beendet."); refreshUi(); return; }
      const hv = L.hvFor(e.slide);
      if (!hv) return;
      const cur = reveal.getIndices();
      if (cur.h === hv.h && (cur.v || 0) === hv.v && (cur.f == null ? -1 : cur.f) === (e.f == null ? -1 : e.f)) return;
      navLock++;
      reveal.slide(hv.h, hv.v, e.f >= 0 ? e.f : undefined);
      setTimeout(() => { navLock--; }, 200);
    }
    select.addEventListener("change", () => {
      followUid = select.value === "" ? null : Number(select.value);
      goToFollowed();
      refreshUi(); render();   // auch ohne Navigation: Whiteboard-Zustand des Tutors übernehmen
    });
    const onNav = () => {
      if (followUid != null && !navLock) {
        const e = L.presence.find((x) => x.uid === followUid);
        const p = pos();
        if (!e || e.slide !== p.uid || (e.f == null ? -1 : e.f) !== p.f) {
          followUid = null;        // manuelles Navigieren beendet das Folgen
          refreshUi();
        }
      }
      wbManual = false;
      render();
      ensureLoaded();
      refreshUi();
    };
    reveal.on("slidechanged", onNav);
    reveal.on("fragmentshown", onNav);
    reveal.on("fragmenthidden", onNav);

    // ── Toast ────────────────────────────────────────────────────────
    function toast(msg) {
      const t = document.createElement("div");
      t.className = "slive-toast";
      t.textContent = msg;
      root.appendChild(t);
      setTimeout(() => t.remove(), 3500);
    }

    // ── Laser (Punkte anderer Tutoren) ───────────────────────────────
    // Der Sender schickt Pakete (`pts`, ~50 ms); sie werden hier gleichmäßig
    // über die Paketdauer abgespielt (Queue je Laser) und die Spur wird als
    // geglättete Kurve gezeichnet.
    const BATCH_MS = 50;
    const lasers = new Map(); // cid → {el, pts[], queue[], pressed, on, t, slide, endT, lastDue}
    const trail = document.createElement("canvas");
    trail.className = "slive-trail";
    laserBox.appendChild(trail);
    let raf = 0;
    function sizeTrail() {
      const { w, h } = size();
      const dpr = window.devicePixelRatio || 1;
      trail.width = w * dpr; trail.height = h * dpr;
      trail.style.width = w + "px"; trail.style.height = h + "px";
      trail.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    sizeTrail();
    function applyLaser(s, it, t) {
      if (!it.on) {
        if (s.pressed) s.endT = t;
        s.pressed = false; s.on = false;
        return;
      }
      s.on = true;
      s.el.style.transform = `translate(${it.x}px, ${it.y}px) scale(${geo().sc / PEN_REF})`;
      if (it.pressed) {
        if (!s.pressed) s.pts = [];
        s.pts.push({ x: it.x, y: it.y });
        if (s.pts.length > 4000) s.pts.splice(0, 2000);
      } else if (s.pressed) s.endT = t;
      s.pressed = it.pressed;
    }
    function laserFrame(t) {
      const { w, h } = size();
      const g = trail.getContext("2d");
      g.clearRect(0, 0, w, h);
      const p = pos();
      const lw = 4 * geo().sc / PEN_REF;
      let any = false;
      for (const [cid, s] of lasers) {
        while (s.queue.length && s.queue[0].due <= t) applyLaser(s, s.queue.shift(), t);
        if (s.queue.length) any = true;
        const stale = !s.queue.length && performance.now() - s.t > LASER_STALE_MS;
        if (stale || !s.on) { s.el.style.display = "none"; if (stale && !s.pts.length) { s.el.remove(); lasers.delete(cid); } }
        else if (s.slide === p.uid && (tutor || visible) && L.shared) { s.el.style.display = "block"; any = true; }
        else s.el.style.display = "none";
        if (s.pts.length && s.slide === p.uid && (tutor || visible)) {
          let a = 0.55;
          if (!s.pressed) { a = 0.55 * Math.exp(-(t - s.endT) / 1000 / 0.25); if (a < 0.01) { s.pts = []; continue; } }
          g.strokeStyle = `rgba(255,59,59,${a})`; g.lineWidth = lw; g.lineCap = "round"; g.lineJoin = "round";
          const P = s.pts;
          g.beginPath(); g.moveTo(P[0].x, P[0].y);
          for (let i = 1; i < P.length - 1; i++) g.quadraticCurveTo(P[i].x, P[i].y, (P[i].x + P[i + 1].x) / 2, (P[i].y + P[i + 1].y) / 2);
          if (P.length > 1) g.lineTo(P[P.length - 1].x, P[P.length - 1].y);
          g.stroke(); any = true;
        }
      }
      raf = (any || lasers.size) ? requestAnimationFrame(laserFrame) : 0;
    }
    on("laser", (m) => {
      if (m.cid === L.cid) return;
      let s = lasers.get(m.cid);
      if (!s) {
        const el = document.createElement("div");
        el.className = "slive-laser";
        laserBox.appendChild(el);
        s = { el, pts: [], queue: [], pressed: false, on: false, t: 0, slide: null, endT: 0, lastDue: 0 };
        lasers.set(m.cid, s);
      }
      const now = performance.now();
      if (s.slide !== m.slide) { s.queue = []; s.pts = []; s.pressed = false; }
      s.slide = m.slide; s.t = now;
      if (!m.on) { s.queue.push({ on: false, due: Math.max(now, s.lastDue) }); s.lastDue = 0; }
      else {
        const { w, h } = size();
        const raw = m.pts && m.pts.length ? m.pts : [[m.x, m.y]];
        // Rückstand abbauen: läuft die Queue > 300 ms hinterher, direkt aufholen
        const base = s.lastDue > now + 300 ? now : Math.max(now, s.lastDue);
        const step = raw.length > 1 ? BATCH_MS / raw.length : 0;
        raw.forEach((pt, i) => {
          const q = L.fromCfg(pt[0], pt[1], w, h);
          s.queue.push({ on: true, x: q.x, y: q.y, pressed: !!m.pressed, due: base + (i + 1) * step });
        });
        s.lastDue = base + raw.length * step;
      }
      if (!raf) raf = requestAnimationFrame(laserFrame);
    });

    // ── WS-Ereignisse ────────────────────────────────────────────────
    on("presence", () => { refreshUi(); goToFollowed(); render(); });
    on("share", (shared) => {
      if (!shared && !tutor) { cache.clear(); L.unloadAll(); }
      refreshUi(); render(); ensureLoaded();
    });
    on("open", () => {              // Reconnect: Stand der aktuellen Folie neu laden
      if (tutor) return;
      cache.clear(); L.unloadAll(); ensureLoaded();
    });
    on("gap", (d) => {
      if (tutor) return;
      cache.delete(d.uid); L.unloadAll(); ensureLoaded();
    });
    on("remote-op", (m) => {
      if (tutor) return;
      if (m.kind === "reset_all") { cache.clear(); render(); refreshUi(); return; }
      let rows = cache.get(m.slide);
      if (!rows) return;
      const key = m.mode + "|" + m.f;
      let r = rows.get(key);
      if (!r) rows.set(key, (r = { rev: 0, events: [] }));
      r.rev = m.rev;
      if (m.kind === "append") {
        const gone = m.events.some((e) => e.type === "clear");
        r.events.push(...m.events);
        const p = pos();
        const here = m.mode === 1 ? m.slide === L.WB : (p.uid === m.slide && p.f === m.f);
        if (!gone && here && visible && L.shared && (m.mode === 0 || wbState().show)) {
          const G = geo();
          for (const e of m.events) {
            if (m.mode === 1 && (e.board || 0) !== board) continue;
            drawEvent(m.mode === 0 ? nctx : wctx, m.mode, e, G, WHITE);
          }
          refreshUi();
          return;
        }
      } else if (m.kind === "remove") {
        const sids = new Set(m.sids);
        r.events = r.events.filter((e) => !sids.has(e.sid));
      }
      refreshUi();
      render();
    });
    const onResize = () => { sizeCanvases(); sizeTrail(); render(); };
    window.addEventListener("resize", onResize);
    offs.push(() => window.removeEventListener("resize", onResize));

    sizeCanvases();
    refreshUi();
    render();
    ensureLoaded();
    return { render, refreshUi };
  }

  window.aicampusLiveViewer = { mount, drawEvent, GRIDS };
})();
