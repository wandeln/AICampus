// ─────────────────────────────────────────────────────────────────────
// Live-Notizen (Stift/Whiteboard/Laser) — Verbindung + Sync-Kern.
//
// Ein WebSocket pro Deck (/ws/courses/{c}/slides/{d}/live), geteilt von
// Tutor (sendet + empfängt) und Student (nur empfangen). Singleton:
// überlebt Reveal-Remounts. Siehe docs/LLM-plans/slide-annotations-live-share.md.
//
// Zuordnung Folie ↔ slide_uid: der Server liefert `slide_uids` (Blatt-Folien
// in Reveal-Reihenfolge); slides.js setzt `data-slide-uid` an jedes <section>.
// L.setDeck() baut daraus die Map uid ↔ (h,v). Das Wire-Protokoll kennt nur uids.
//
// Events (L.on): open, close, status, share, presence, laser, remote-op, gap
// ─────────────────────────────────────────────────────────────────────
(function () {
  const L = (window.aicampusLive = {
    active: false,   // Tutor: Server-Sync aktiv (Plugin meldet Events) — Plugin prüft das
    tutor: false,
    shared: false,
    userKey: "0",
    presence: [],
    status: "ok",    // ok | offline
    cid: null,
  });

  let cfg = null;
  let ws = null;
  let retry = 0;
  const handlers = {};
  L.on = (t, fn) => {
    (handlers[t] = handlers[t] || []).push(fn);
    return () => { handlers[t] = handlers[t].filter((x) => x !== fn); };   // Abmelden (Remount)
  };
  const emit = (t, d) => (handlers[t] || []).forEach((fn) => {
    try { fn(d); } catch (e) { console.error("[slide-live]", t, e); }
  });

  // ── Folie ↔ uid ────────────────────────────────────────────────────
  const uidToHv = new Map();
  const hvToUid = new Map();
  const fragUids = new Set(); // Folien mit .fragment (Reveal: f=-1 statt undefined ohne sichtbares Fragment)
  L.setDeck = function (root) {
    uidToHv.clear();
    hvToUid.clear();
    fragUids.clear();
    let h = 0;
    for (const sec of root.children) {
      if (sec.tagName !== "SECTION") continue;
      const kids = [...sec.children].filter((c) => c.tagName === "SECTION");
      const reg = (el, v) => {
        const uid = el.getAttribute("data-slide-uid");
        if (!uid) return;
        uidToHv.set(uid, { h, v });
        if (el.querySelector(".fragment")) fragUids.add(uid);
        hvToUid.set(h + "," + v, uid);
      };
      if (kids.length) kids.forEach((k, v) => reg(k, h, v)); else reg(sec, 0);
      h++;
    }
  };
  L.uidFor = (h, v) => hvToUid.get(h + "," + (v || 0));
  L.hvFor = (uid) => uidToHv.get(uid);
  L.hasFrag = (uid) => fragUids.has(uid);

  // ── Config-Raum ↔ Pixel (Laser): wie die Plugin-Geometrie (min-Skala, zentriert) ──
  L.toCfg = (px, py, w, h) => {
    const sc = Math.min(w / cfg.slideW, h / cfg.slideH);
    return { x: (px - (w - cfg.slideW * sc) / 2) / sc, y: (py - (h - cfg.slideH * sc) / 2) / sc };
  };
  L.fromCfg = (x, y, w, h) => {
    const sc = Math.min(w / cfg.slideW, h / cfg.slideH);
    return { x: (w - cfg.slideW * sc) / 2 + x * sc, y: (h - cfg.slideH * sc) / 2 + y * sc };
  };

  // ── Verbindung ─────────────────────────────────────────────────────
  const cs = Math.random().toString(36).slice(2, 12);
  let seq = 0;
  const unacked = [];       // [{seq, msg, key}]
  const revs = new Map();   // "uid|mode|f" → rev
  const loaded = new Set(); // Student: Folien mit geladenem Stand
  const loading = new Map();// Student: uid → gepufferte Ops während des Fetches
  let flushWaiters = [];

  const rowKey = (uid, mode, f) => uid + "|" + mode + "|" + (f == null ? -1 : f);
  L.rowKey = rowKey;
  L.revs = revs;

  function setStatus(s) {
    if (L.status === s) return;
    L.status = s;
    emit("status", s);
  }

  function rawSend(obj) {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  L.init = function (c) {
    cfg = c;
    L.tutor = !!c.tutor;
    L.userKey = String(c.userId);
    connect();
  };

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws/courses/${cfg.courseId}/slides/${cfg.deckId}/live`);
    } catch (e) {
      ws = null;
      return setTimeout(connect, 5000);
    }
    const mine = ws;
    mine.onopen = () => {
      retry = 0;
      setStatus("ok");
      for (const u of unacked) rawSend(u.msg); // unbestätigte Ops erneut (Server dedupliziert)
      emit("open");
    };
    mine.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      handle(m);
    };
    mine.onclose = () => {
      if (ws === mine) ws = null;
      setStatus("offline");
      emit("close");
      setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
    };
  }

  function handle(m) {
    switch (m.type) {
      case "hello":
        L.cid = m.cid;
        L.shared = !!m.shared;
        emit("share", L.shared);
        break;
      case "ack": {
        const i = unacked.findIndex((u) => u.seq === m.seq);
        if (i >= 0) {
          const u = unacked.splice(i, 1)[0];
          if (m.rev != null && u.key) revs.set(u.key, Math.max(revs.get(u.key) || 0, m.rev));
          if (m.err) console.warn("[slide-live] Op vom Server abgelehnt", u.msg);
        }
        if (!unacked.length) checkFlushed();
        break;
      }
      case "op":
        incomingOp(m);
        break;
      case "presence":
        L.presence = m.tutors || [];
        emit("presence", L.presence);
        break;
      case "laser":
        emit("laser", m);
        break;
      case "share":
        L.shared = !!m.shared;
        emit("share", L.shared);
        break;
    }
  }

  // ── Eingehende Ops (von anderen Tutoren) ───────────────────────────
  function incomingOp(m) {
    if (m.kind === "reset_all") {
      revs.clear();
      loaded.clear();
      emit("remote-op", m);
      return;
    }
    if (loading.has(m.slide)) { loading.get(m.slide).push(m); return; }
    if (!L.tutor && !loaded.has(m.slide)) return;   // Student: nur geladene Folien
    const key = rowKey(m.slide, m.mode, m.f);
    const known = revs.get(key) || 0;
    if (m.rev <= known) return;                      // schon gesehen
    if (m.rev !== known + 1) { emit("gap", { uid: m.slide }); return; }
    revs.set(key, m.rev);
    emit("remote-op", m);
  }

  // Student: Folie laden (Ops während des Fetches puffern, danach nach rev anwenden)
  L.beginLoad = (uid) => loading.set(uid, []);
  L.markLoaded = (uid, rows) => {
    for (const k of [...revs.keys()]) if (k.startsWith(uid + "|")) revs.delete(k);
    for (const r of rows) revs.set(rowKey(uid, r.mode, r.f), r.rev);
    loaded.add(uid);
    const buf = loading.get(uid) || [];
    loading.delete(uid);
    buf.forEach(incomingOp);
  };
  L.endLoad = (uid) => loading.delete(uid);
  L.unloadAll = () => { loaded.clear(); loading.clear(); revs.clear(); };

  // ── Ausgehende Ops (Tutor) ─────────────────────────────────────────
  const pending = new Map(); // key → {uid, mode, f, events[]}
  let pendTimer = 0;

  function sendOp(msg, key) {
    msg.type = "op";
    msg.cs = cs;
    msg.seq = ++seq;
    unacked.push({ seq: msg.seq, msg, key });
    if (!rawSend(msg)) setStatus("offline");
  }

  function flushPending() {
    clearTimeout(pendTimer);
    pendTimer = 0;
    for (const p of pending.values()) {
      for (let i = 0; i < p.events.length; i += 400) {
        sendOp({ kind: "append", slide: p.uid, mode: p.mode, f: p.f, events: p.events.slice(i, i + 400) },
          rowKey(p.uid, p.mode, p.f));
      }
    }
    pending.clear();
  }

  // Plugin-Hooks (s. plugin.js, AICAMPUS PATCH „Live-Notizen“)
  L.onEvent = (mode, idx, ev) => {
    const uid = L.uidFor(idx.h, idx.v);
    if (!uid) return;
    const f = idx.f == null ? -1 : idx.f;
    const key = rowKey(uid, mode, f);
    let p = pending.get(key);
    if (!p) pending.set(key, (p = { uid, mode, f, events: [] }));
    p.events.push(ev);
    if (!pendTimer) pendTimer = setTimeout(flushPending, 120);
  };
  L.onRemove = (mode, idx, sids) => {
    const uid = L.uidFor(idx.h, idx.v);
    if (!uid) return;
    flushPending();
    const f = idx.f == null ? -1 : idx.f;
    sendOp({ kind: "remove", slide: uid, mode, f, sids }, rowKey(uid, mode, f));
  };
  L.onResetAll = () => {
    flushPending();
    sendOp({ kind: "reset_all" }, null);
    revs.clear();
  };

  function checkFlushed() {
    if (pending.size || unacked.length) return;
    flushWaiters.forEach((r) => r());
    flushWaiters = [];
  }
  /** Wartet (max. 2.5 s), bis alles gesendet + bestätigt ist (vor Remount/Reload). */
  L.flush = () => new Promise((res) => {
    flushPending();
    if (!unacked.length) return res();
    flushWaiters.push(res);
    setTimeout(res, 2500);
  });
  window.addEventListener("pagehide", flushPending);

  // ── Presence / Laser / Freigabe (Tutor) ────────────────────────────
  let lastView = "";
  L.sendView = (v) => {
    if (!L.tutor) return;
    const uid = L.uidFor(v.h, v.v);
    if (!uid) return;
    const msg = { type: "view", slide: uid, f: v.f == null ? -1 : v.f, mode: v.mode, board: v.board || 0, active: v.active };
    const s = JSON.stringify(msg);
    if (s === lastView) return;
    lastView = s;
    rawSend(msg);
  };
  L.resendView = () => { lastView = ""; };

  // Laser: alle Mauspunkte werden gesammelt und ~20×/s als Paket gesendet
  // (`pts`, Config-Raum) → der Betrachter spielt sie gleichmäßig ab (glatte
  // Linie statt 25-Hz-Ecken). Zustandswechsel (gedrückt/aus) schicken das
  // Paket sofort, damit Reihenfolge und Endpunkt stimmen.
  const LASER_BATCH_MS = 50;
  let laserBuf = [], laserTimer = 0, laserState = null; // laserState: {slide, pressed}
  function flushLaser() {
    clearTimeout(laserTimer);
    laserTimer = 0;
    if (!laserBuf.length || !laserState) return;
    const last = laserBuf[laserBuf.length - 1];
    rawSend({ type: "laser", slide: laserState.slide, on: true, pressed: laserState.pressed, x: last[0], y: last[1], pts: laserBuf });
    laserBuf = [];
  }
  L.sendLaser = (px, py, pressed, on, w, h, idx) => {
    if (!L.tutor || !ws) return;
    const uid = L.uidFor(idx.h, idx.v);
    if (!uid) return;
    pressed = !!pressed;
    if (laserState && (laserState.slide !== uid || laserState.pressed !== pressed || !on)) flushLaser();
    if (!on) {
      laserState = null;
      rawSend({ type: "laser", slide: uid, on: false, pressed: false });
      return;
    }
    laserState = { slide: uid, pressed };
    const q = L.toCfg(px, py, w, h);
    laserBuf.push([Math.round(q.x * 10) / 10, Math.round(q.y * 10) / 10]);
    if (laserBuf.length >= 40) flushLaser();
    else if (!laserTimer) laserTimer = setTimeout(flushLaser, LASER_BATCH_MS);
  };

  L.setShared = (shared) => rawSend({ type: "share", shared: !!shared });

  // ── Tutor: Server-Stand → Plugin-Storage (sessionStorage) seeden ────
  // Das Plugin lädt `config.storage` synchron beim Init; wir schreiben den
  // kanonischen Serverstand mit den AKTUELLEN Indizes (h,v) davor hinein →
  // Notizen wandern mit ihren Folien, auch nach Remounts.
  L.seedTutor = async function (storageKey) {
    try {
      await L.flush();
      const r = await fetch(`/api/courses/${cfg.courseId}/slides/${cfg.deckId}/annotations`, { credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      L.shared = !!d.shared;
      emit("share", L.shared);
      const storage = [0, 1].map(() => ({ width: cfg.slideW, height: cfg.slideH, data: [] }));
      revs.clear();
      for (const row of d.rows) {
        revs.set(rowKey(row.uid, row.mode, row.f), row.rev);
        const hv = L.hvFor(row.uid);
        if (!hv || !row.events.length) continue;
        const slide = { h: hv.h, v: hv.v };
        if (row.f >= 0 || (row.f === -1 && L.hasFrag(row.uid))) slide.f = row.f;
        storage[row.mode].data.push({
          slide,
          events: row.events.map((e, i) => Object.assign({}, e, { time: i })),
          duration: row.events.length,
        });
      }
      sessionStorage.setItem(storageKey, JSON.stringify(storage));
      L.active = true;
    } catch (e) {
      console.warn("[slide-live] Notizen nicht vom Server geladen — nur lokal:", e);
      L.active = false;
      setStatus("offline");
    }
  };

  /** Tutor: Zeilen einer Folie vom Server holen (Gap/Reconnect) → Liste {uid,mode,f,rev,events}. */
  L.fetchRows = async (uid) => {
    const url = uid
      ? `/api/courses/${cfg.courseId}/slides/${cfg.deckId}/annotations/slide?uid=${encodeURIComponent(uid)}`
      : `/api/courses/${cfg.courseId}/slides/${cfg.deckId}/annotations`;
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    return d.rows.map((x) => Object.assign({ uid }, x));
  };
})();
