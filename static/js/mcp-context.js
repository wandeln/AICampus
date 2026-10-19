// MCP-Client-Kontext (get_user_focus):
// Meldet den aktuellen UI-Zustand der Kurs-Seiten an das Backend, damit der
// MCP-Client (z. B. ChatGPT) sieht, wo der User gerade arbeitet:
//
//   - URL (Server leitet Kurs + Seite daraus ab)
//   - markierten Text + Kontext (±120 Zeichen, gleiche Normalisierung wie
//     die „Fragen im Skript"-Quote-Mechanik in script.html)
//   - aktuelles Skript-Kapitel (data-section-id-Karte)
//   - aktuelle Slide (Reveal-Hook, inkl. Folien-Text)
//
// - Nur auf /courses/{id}/…-Seiten und nur wenn der Tab sichtbar ist
//   (Multi-Tab: letzter aktivierter Tab gewinnt — versteckte Tabs melden nie)
// - Fire-and-forget: Fehler still, kein UI, kein Analytics, kein Content-Log
// - Slide-Hook (in slides_edit.html / slides_present.html NACH
//   reveal.initialize() einrichten):
//       aicMcpWireReveal(reveal, slidesEl)
//   Alle anderen Seiten (Aufgaben, Applet Studio, …) brauchen keinen Hook:
//   URL + Auswahl reichen.
// Details: docs/LLM-plans/mcp-client-context.md
(function () {
  "use strict";

  if (!/^\/courses\/\d+/.test(location.pathname)) return;

  // ── Text-Extraktion ─────────────────────────────────────────────────

  // Unsichtbare/übersprungene Texte (KaTeX-MathML, SVG, Sprechernotizen):
  // gleicher Filter für Auswahl- UND Folien-Text-Extraktion.
  const AUX_SELECTOR = ".katex-mathml, svg, aside.notes";
  function auxFilter(node) {
    if (node.nodeType !== Node.TEXT_NODE) return NodeFilter.FILTER_SKIP;
    const p = node.parentElement;
    if (p && p.closest(AUX_SELECTOR)) return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT;
  }

  // Plain-Text einer DOM-Teilstruktur (Whitespace kollabiert), max. limit.
  function extractText(root, limit) {
    let out = "";
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, auxFilter);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      out += " " + n.nodeValue;
      if (out.length > limit + 200) break;
    }
    out = out.replace(/\s+/g, " ").trim();
    return out.slice(0, limit) || null;
  }

  // Normierter Volltext der Root + Offsets-Karte: wie script.html
  // (Whitespace → einzelnes ' '); pos[i] = Originalindex von norm[i].
  function textMap(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, auxFilter);
    const nodes = [];
    let full = "";
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      nodes.push(n);
      full += n.nodeValue;
    }
    if (!full.length) return null;
    let norm = "";
    const pos = [];
    for (let i = 0; i < full.length; i++) {
      const ch = full[i];
      if (/\s/.test(ch)) {
        if (norm && /\s/.test(norm[norm.length - 1])) continue;
        norm += " ";
      } else {
        norm += ch;
      }
      pos.push(i);
    }
    // Inverse Karte: Originalindex → normierter Index (weitere Whitespace-
    // Zeichen eines Laufs erben den Index des ersten)
    const inv = new Array(full.length);
    for (let i = 0; i < pos.length; i++) inv[pos[i]] = i;
    for (let i = 1; i < full.length; i++) if (inv[i] === undefined) inv[i] = inv[i - 1];
    return { nodes, full, norm, inv };
  }

  // Ist der Range invertiert (rechts→links gezogen)? Gleiche Primitives
  // wie script.html (nur compareDocumentPosition).
  function rangeInverted(r) {
    const sc = r.startContainer, so = r.startOffset;
    const ec = r.endContainer, eo = r.endOffset;
    if (sc === ec) return eo < so;
    const rel = ec.compareDocumentPosition(sc);
    if (rel & Node.DOCUMENT_POSITION_FOLLOWING) return true;
    if (rel & Node.DOCUMENT_POSITION_CONTAINS) {
      let c = sc;
      while (c.parentNode !== ec && c.parentNode) c = c.parentNode;
      let k = 0;
      for (let x = ec.firstChild; x && x !== c; x = x.nextSibling) k++;
      return eo <= k;
    }
    if (rel & Node.DOCUMENT_POSITION_CONTAINED_BY) {
      let c = ec;
      while (c.parentNode !== sc && c.parentNode) c = c.parentNode;
      let k = 0;
      for (let x = sc.firstChild; x && x !== c; x = x.nextSibling) k++;
      return so > k;
    }
    return false;
  }

  // Auswahl → Quote (≤1000) + Kontext (±120 im normierten Raum, bei sehr
  // langen Auswahlen weggelassen — die Quote ist dann eindeutig) + Offset
  // der Auswahl im Kontext. Logik wie sqSelectionInfo in script.html.
  function selectionInfo(root, range) {
    let sc = range.startContainer, so = range.startOffset;
    let ec = range.endContainer, eo = range.endOffset;
    if (rangeInverted(range)) [sc, so, ec, eo] = [ec, eo, sc, so];
    const nr = document.createRange();
    nr.setStart(sc, so);
    nr.setEnd(ec, eo);
    const map = textMap(root);
    if (!map) return null;
    let a = null, b = null, running = 0;
    for (const n of map.nodes) {
      if (nr.intersectsNode(n)) {
        let x = 0, y = n.nodeValue.length;
        if (nr.startContainer === n) x = nr.startOffset;
        if (nr.endContainer === n) y = nr.endOffset;
        if (y > x) {
          if (a === null) a = running + x;
          b = running + y;
        }
      }
      running += n.nodeValue.length;
    }
    if (a === null || b === null || b <= a) return null;
    let s = map.inv[a], e = map.inv[b - 1] + 1;
    while (s < e && map.norm[s] === " ") s++;
    while (e > s && map.norm[e - 1] === " ") e--;
    if (e <= s) return null;
    const quote = map.norm.slice(s, e).slice(0, 1000);
    const cStart = Math.max(0, s - 120);
    const cEnd = Math.min(map.norm.length, e + 120);
    let ctx = null, off = 0;
    if (cEnd - cStart <= 1990) {
      ctx = map.norm.slice(cStart, cEnd);
      off = s - cStart;
    }
    return { quote, ctx, off };
  }

  // Aktuelle Auswahl → {quote, ctx, off, sectionId} oder null.
  // Auswahl in Textfeldern (Edit-Modus) zählt nicht.
  function captureSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
    const range = sel.getRangeAt(0);
    const anc = range.commonAncestorContainer;
    if (anc.nodeType === Node.ELEMENT_NODE &&
        (anc.tagName === "TEXTAREA" || anc.tagName === "INPUT")) return null;
    let el = anc.nodeType === Node.TEXT_NODE ? anc.parentElement : anc;
    if (!el || !el.closest) return null;
    const card = el.closest("[data-section-id]");
    const sectionId = card ? (parseInt(card.getAttribute("data-section-id")) || null) : null;
    // Kontext-Root: Slide-Section > Markdown-Content (Skript-Kapitel,
    // Aufgabenstellung/Lösung, …)
    let root = null;
    if (el.closest(".reveal .slides")) root = el.closest("section");
    else root = el.closest(".markdown-preview");
    const info = root ? selectionInfo(root, range) : null;
    let quote = info ? info.quote : (sel.toString() || "").replace(/\s+/g, " ").trim();
    if (!quote) return null;
    return {
      quote: quote.slice(0, 1000),
      ctx: info ? info.ctx : null,
      off: info ? info.off : 0,
      sectionId,
    };
  }

  // Aktuelles Skript-Kapitel: mit Auswahl → die Karte, die sie enthält;
  // ohne → die Karte, die die Viewport-Mitte abdeckt (sonst die nächste).
  function currentSectionId(selInfo) {
    if (selInfo && selInfo.sectionId) return selInfo.sectionId;
    const cards = document.querySelectorAll("[data-section-id]");
    if (!cards.length) return null;
    const mid = window.innerHeight / 2;
    let best = null, bestDist = Infinity;
    for (const c of cards) {
      const r = c.getBoundingClientRect();
      if (!r.height) continue;
      if (r.top <= mid && r.bottom >= mid) {
        return parseInt(c.getAttribute("data-section-id")) || null;
      }
      const d = Math.min(Math.abs(r.top - mid), Math.abs(r.bottom - mid));
      if (d < bestDist) { bestDist = d; best = c; }
    }
    return best ? (parseInt(best.getAttribute("data-section-id")) || null) : null;
  }

  // ── Slide-Zustand (wird durch aicMcpWireReveal gesetzt) ─────────────
  let slideState = null; // {index (1-basiert, flach), count}

  // Aktuelle Slide-Section per Reveal's .present-Klasse; bei
  // Mehrfach-Previews (z. B. Deck-Übersicht) die bei der Viewport-Mitte.
  function currentSlideSection() {
    const sections = document.querySelectorAll(".reveal .slides section.present");
    if (!sections.length) return null;
    if (sections.length === 1) return sections[0];
    const mid = window.innerHeight / 2;
    let best = null, bestDist = Infinity;
    for (const s of sections) {
      const r = s.getBoundingClientRect();
      if (!r.height) continue;
      if (r.top <= mid && r.bottom >= mid) return s;
      const d = Math.min(Math.abs(r.top - mid), Math.abs(r.bottom - mid));
      if (d < bestDist) { bestDist = d; best = s; }
    }
    return best;
  }

  // Aktuelle Folie → {index?, count?, text?} oder null (keine Folie
  // sichtbar → null, statt veralteter Werte zu melden).
  function currentSlide() {
    const section = currentSlideSection();
    if (!section) return null;
    const out = {};
    if (slideState) {
      out.index = slideState.index;
      out.count = slideState.count;
    }
    const text = extractText(section, 2000);
    if (text) out.text = text;
    return Object.keys(out).length ? out : null;
  }

  // ── Melden ──────────────────────────────────────────────────────────

  function buildPayload() {
    const selInfo = captureSelection();
    const payload = { url: location.pathname };
    if (selInfo) {
      payload.selection = selInfo.quote;
      if (selInfo.ctx) {
        payload.selection_context = selInfo.ctx;
        payload.selection_offset = selInfo.off;
      }
    }
    if (/^\/courses\/\d+\/script\/?$/.test(location.pathname)) {
      const sectionId = currentSectionId(selInfo);
      if (sectionId) payload.section_id = sectionId;
    }
    const slide = currentSlide();
    if (slide) payload.slide = slide;
    return payload;
  }

  function report() {
    if (document.visibilityState !== "visible") return;
    fetch("/api/mcp/client-context", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    }).catch(() => {}); // fire-and-forget
  }

  // Auswahl: 300 ms Debounce (Ziehen soll keine Report-Flut machen);
  // nur bei Änderung — kollabierte Auswahl meldet einmal selection: null.
  let lastSelJson = "";
  let selTimer = null;
  document.addEventListener("selectionchange", () => {
    if (document.visibilityState !== "visible") return;
    clearTimeout(selTimer);
    selTimer = setTimeout(() => {
      const json = JSON.stringify(captureSelection());
      if (json === lastSelJson) return;
      lastSelJson = json;
      report();
    }, 300);
  });

  // Tab wieder aktiviert → sofort eigenen Zustand melden (letzt-aktivierter
  // Tab gewinnt; versteckte Tabs melden nie).
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") report();
  });

  // ── Slide-Hook (slides_edit.html / slides_present.html) ─────────────
  // aicMcpWireReveal(reveal, slidesEl): NACH reveal.initialize() aufrufen.
  // Flachzählung: Stacks zählen ihre Unterfolien (wie Reveal's eigener
  // Zähler und die Markdown-Quelle '---'/'--').

  window.aicMcpSetSlide = function (index, count) {
    slideState = count > 0 ? { index, count } : null;
    report();
  };

  window.aicMcpWireReveal = function (reveal, slidesEl) {
    const update = () => {
      try {
        const idx = reveal.getIndices();
        let flat = 0;
        let count = 0;
        for (let i = 0; i < slidesEl.children.length; i++) {
          const el = slidesEl.children[i];
          const n = el.classList.contains("slides-stack") ? el.children.length : 1;
          count += n;
          if (i < idx.h) flat += n;
        }
        flat += idx.v || 0;
        if (count > 0) window.aicMcpSetSlide(flat + 1, count);
      } catch (e) { /* Instanz im Umbau */ }
    };
    reveal.on("slidechanged", update);
    // Initiale Folie (inkl. Deep-Link): das erste slidechanged feuert
    // während initialize() — VOR der Listener-Registrierung → einmal direkt.
    update();
  };

  // Seiten-Load → Initial-Report (Script am Ende von <body> → DOM fertig).
  report();
})();
