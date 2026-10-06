/*
 * Folien: Client-Spiegel von services/slides_service.py.
 *
 * - parseSlides(content)   → lenientes Parsen (Folientrenner `---` und
 *                            Unterfolien-Trenner `--` fence-aware, Direktiven,
 *                            Spaltentrenner `||`). Ein Block mit ≥2 `--`-Seg-
 *                            menten wird zu einem Stack: leere Eltern-Folie mit
 *                            `children` (vertikale Unterfolien für Reveal).
 *                            Ungültige Direktiven bleiben als Text stehen (der
 *                            Server meldet bei Speichern die genaue Meldung).
 *                            (async) Am Ende wird eine auto-Quellen-Folie
 *                            angehängt, wenn Zitate (@cite:/@citet:/@citep:)
 *                            vorkommen — analog options.bibliography im Skript
 *                            (nur zitierte Quellen, nach kursweiter Nummer);
 *                            manuell geschriebene "class: quellen"-Folien
 *                            (altes Import-Prompt-Format) werden verworfen.
 * - aicampusRegisterSlideReveal(rootEl, inst) → registriert die Reveal-
 *                            Instanz pro .reveal-Root (WeakMap), damit der
 *                            globale Zitations-Click-Handler embeddede
 *                            Instanzen (Kacheln, Editor-Vorschau) navigieren
 *                            kann: @cite:-Links (data-refkey) springen zur
 *                            auto-Quellen-Folie desselben Decks und flashen
 *                            den jeweiligen Eintrag.
 * - countSlides(slides)    → Anzahl anzeigbarer (Blatt-)Folien: jeder Stack
 *                            zählt seine Unterfolien (wie Reveal's Zähler).
 * - renderSlideInto(slide, el) → rendert eine Folie (Markdown via
 *                            renderMarkdown, bleibt in .markdown-preview).
 * - renderSlideThumb(slide, thumbEl, themeClass) → Kachel-Vorschau:
 *                            Folie in 960×(960/Ratio) rendern und per
 *                            transform auf die Kachelbreite skalieren.
 *                            (Ratio = --slides-aspect des Themes.)
 *                            — nicht mehr in Verwendung (Kacheln und
 *                            Design-Vorschau nutzen echte Reveal-Instanzen).
 * - buildSlideSection(slide, footerText, slidePos) → <section> für Reveal
 *                            (Inhalt + Sprechernotiz als <aside class="notes");
 *                            slidePos = { deckId, h } (Folien-Block-Index) →
 *                            wird mit v (Unterfolien-Index) an renderMarkdown
 *                            weitergereicht (S-Nummern unlabeled
 *                            Subfigure-Komplexe, s. slides-refmap `figures`);
 *                            Standard-Transition ist "autoanimate"
 *                            (data-auto-animate); "transition: fade|slide|
 *                            zoom|none" setzt eine klassische Transition.
 *                            Gezoomte Applets ({zoom=X}) überspringt das
 *                            Auto-Animate per autoAnimateMatcher-Config
 *                            (siehe aicampusAutoAnimateMatcher) — der
 *                            transform-Zoom bleibt erhalten.
 */

const SLIDE_LAYOUTS = new Set(["center", "topleft"]);
const SLIDE_TRANSITIONS = new Set(["fade", "slide", "zoom", "none", "autoanimate"]);
const SLIDE_DEFAULT_TRANSITION = "autoanimate";

const SLIDE_FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})/;
const SLIDE_DIRECTIVE = /^(layout|transition|class|notes|background):\s*(\S.*)$/;
const SLIDE_CLASS = /^[A-Za-z0-9_-]+$/;
// background: Markdown-Bild-/Applet-Snippet ![Titel](/media/…) ohne Zusätze
// (kein Label/Attribute) — der Pfad darf kein Whitespace und kein { enthalten
// (sonst würde ein falsch platziertes {zoom=X} innerhalb der Klammer still-
//  schweigend als Pfadteil verschluckt; ungültige Zeile bleibt stattdessen
//  literal sichtbar). Parität mit slides_service._BG_IMAGE.
// Optionaler {zoom=X} (führender Punkt optional) gilt NUR für Applet-/Website-
// (Iframe-)Backgrounds — Reveal kennt kein natives Bg-Zoom, s. wireBgZoom.
const SLIDE_BG_IMAGE = /^!\[([^\]]*)\]\(([^)\s{]+)\)(?:[ \t]*\{\.?zoom=([\d.]+)\})?$/;
// ?print-pdf-Modus: dort liegt das Folien-Bg als Element in der Folie (pro
// PDF-Seite, interaktiv vor dem Druck) — Reveal's natives Bg-System
// (data-background-*) ist im Print-Modus ausgeblendet und funktioniert
// nicht pro Seite.
const SLIDES_IS_PRINT_PDF = /print-pdf/.test(window.location.search);

/**
 * Teilt den Inhalt an Zeilen mit exakt `---` (Folientrenner) und exakt `--`
 * (Unterfolien-Trenner, nur innerhalb einer Folie) — beides fence-aware.
 * Liefert eine Liste von Folien-Blöcken; jeder Block ist eine Liste von
 * Segmente-Strings (1 Segment = normale Folie, ≥2 = vertikal gestapelte
 * Unterfolien). Exakter Zeilenabgleich (trim === "--"/"---"), damit `--`
 * nie die ersten beiden Zeichen von `---` matcht.
 */
function _splitSlideBlocks(content) {
  const blocks = [];
  let segments = [];
  let current = [];
  let fenceChar = "";
  let fenceLen = 0;

  // Invariante: `segments` = abgeschlossene Segmente des aktuellen Blocks,
  // `current` = aktive (noch nicht in segments enthaltene) Zeilen.
  const newSegment = () => {
    segments.push(current);
    current = [];
  };
  const newBlock = () => {
    segments.push(current);
    blocks.push(segments);
    segments = [];
    current = [];
  };

  for (const line of (content || "").split(/\r?\n/)) {
    if (fenceChar) {
      current.push(line);
      const lead = line.length - line.trimStart().length;
      const rest = line.trim();
      if (
        lead <= 3 &&
        rest.length >= fenceLen &&
        rest.length > 0 &&
        new Set(rest).size === 1 &&
        rest[0] === fenceChar
      ) {
        fenceChar = "";
      }
    } else {
      const t = line.trim();
      if (t === "---") {
        newBlock();
        continue;
      }
      if (t === "--") {
        newSegment();
        continue;
      }
      const m = line.match(SLIDE_FENCE_OPEN);
      if (m) {
        fenceChar = m[1][0];
        fenceLen = m[1].length;
      }
      current.push(line);
    }
  }
  segments.push(current);
  blocks.push(segments);
  return blocks.map((segs) => segs.map((l) => l.join("\n")));
}

function _parseSlideBlock(block, index) {
  const slide = {
    layout: "topleft",
    transition: null,
    css_class: null,
    notes: null,
    background: null,
    columns: [""],
    children: [], // nur bei Stacks (`--`) befüllt
  };
  const lines = block.split("\n");
  const seen = new Set();
  let i = 0;
  while (i < lines.length && !lines[i].trim()) i++;

  while (i < lines.length) {
    const m = lines[i].match(SLIDE_DIRECTIVE);
    if (!m) break;
    const key = m[1];
    const value = m[2].trim();
    let consumed = false;
    if (!seen.has(key)) {
      if (key === "layout" && SLIDE_LAYOUTS.has(value)) {
        slide.layout = value;
        consumed = true;
      } else if (key === "transition" && SLIDE_TRANSITIONS.has(value)) {
        slide.transition = value;
        consumed = true;
      } else if (key === "class" && SLIDE_CLASS.test(value)) {
        slide.css_class = value;
        consumed = true;
      } else if (key === "background") {
        const bm = value.match(SLIDE_BG_IMAGE);
        if (bm) {
          const zoom = bm[3] !== undefined ? parseFloat(bm[3]) : null;
          // Zoom nur für Applet/Website (Iframe) — bei Bildern bleibt die
          // Zeile literal (ungültige Schreibweise fällt so auf, s. 1e).
          if (zoom === null || isAppletSrc(bm[2])) {
            slide.background = { alt: bm[1], src: bm[2], zoom };
            consumed = true;
          }
        }
      } else if (key === "notes") {
        slide.notes = value;
        consumed = true;
      }
    }
    if (!consumed) break; // ungültig/doppelt → als Text stehen lassen
    seen.add(key);
    i++;
  }

  // Spalten werden hier NICHT geparsed (das alte twocol + "||" ist weg):
  // die @startcolumn … @nextcolumn … @endcolumn-Marker sind Renderer-Seite
  // (markdown-renderer.js) — der Folientext bleibt ein einziger Part.
  const body = lines.slice(i);
  slide.columns = [body.join("\n").trim()];
  return slide;
}

/** Alle Zitations-Keys des Decks (@cite:/@citet:/@citep:, fence-aware,
 *  eindeutig, in Reihenfolge des ersten Vorkommens) — Basis der
 *  auto-Quellen-Folie. Das alte manuelle Quellen-Folie-Format "[@cite:key] …"
 *  wird vom selben Regex erwischt (das @cite: in den Klammern). */
function _collectCitedKeys(content) {
  const keys = [];
  const seen = new Set();
  let fenceChar = "";
  let fenceLen = 0;
  for (const line of (content || "").split(/\r?\n/)) {
    if (fenceChar) {
      const lead = line.length - line.trimStart().length;
      const rest = line.trim();
      if (
        lead <= 3 &&
        rest.length >= fenceLen &&
        rest.length > 0 &&
        new Set(rest).size === 1 &&
        rest[0] === fenceChar
      ) {
        fenceChar = "";
      }
      continue;
    }
    const m = line.match(SLIDE_FENCE_OPEN);
    if (m) {
      fenceChar = m[1][0];
      fenceLen = m[1].length;
      continue;
    }
    for (const cm of line.matchAll(/@(citep|citet|cite):([\p{L}0-9_-]+)/gu)) {
      if (!seen.has(cm[2])) {
        seen.add(cm[2]);
        keys.push(cm[2]);
      }
    }
  }
  return keys;
}

async function parseSlides(content) {
  if (!content || !content.trim()) return [];
  // Manuell geschriebene Quellen-Folien (altes Import-Prompt-Format,
  // "class: quellen") verwerfen — die auto-Quellen-Folie unten ersetzt sie
  // (sonst würden beide erscheinen).
  let slides = _splitSlideBlocks(content)
    .map((segs, i) =>
      segs.length === 1
        ? _parseSlideBlock(segs[0], i + 1)
        : _parseStackBlock(segs, i + 1)
    )
    .map((s) => {
      if (!s.children.length || s.css_class === "quellen") return s;
      const kept = s.children.filter((c) => c.css_class !== "quellen");
      return kept.length === s.children.length ? s : { ...s, children: kept };
    })
    .filter((s) => s.css_class !== "quellen");
  // Auto-Quellen-Folie am Deck-Ende (analog options.bibliography im Skript):
  // nur die tatsächlich zitierten Quellen, in kursweiter Nummernreihenfolge;
  // die Einträge rendert @bibentry: im Design des Skript-Quellenverzeichnisses.
  // Zitations-Links verweisen per Click-Handler (unten) auf die jeweilige
  // Quelle auf dieser Folie — der Quellen-Tab ist für Studenten nicht erreichbar.
  const citedKeys = _collectCitedKeys(content);
  if (citedKeys.length) {
    const refMap = await getCourseRefMap();
    const refs = (refMap && refMap.references) || {};
    citedKeys.sort(
      (a, b) => ((refs[a] && refs[a].num) || 0) - ((refs[b] && refs[b].num) || 0)
    );
    slides.push({
      layout: "topleft",
      transition: null,
      css_class: "quellen",
      notes: null,
      background: null,
      columns: ["# Quellen\n\n" + citedKeys.map((k) => "- @bibentry:" + k).join("\n")],
      children: [],
    });
  }
  return slides;
}

// ─── Zitations-Links → auto-Quellen-Folie ─────────────────────────────────
// Die auto-Quellen-Folie (parseSlides) trägt die Einträge mit id + data-refkey.
// Zitations-Links (@cite: etc., data-refkey) navigieren zur jeweiligen Quelle
// im selben Deck — der Quellen-Tab ist für Studenten nicht erreichbar und
// ein Cross-Page-Anker unnötig. Embedded Reveal-Instanzen (Kachel-Vorschau,
// Editor, Präsentation) haben keinen globalen Reveal-Zugriff → Instanzen pro
// .reveal-Root im WeakMap (aicampusRegisterSlideReveal beim Initialisieren).
const _slideReveals = new WeakMap();
function aicampusRegisterSlideReveal(rootEl, inst) {
  if (rootEl) _slideReveals.set(rootEl, inst);
}

document.addEventListener("click", (e) => {
  // Klick auf Link-Text: e.target kann ein Text-Node sein → Element holen.
  const t = e.target;
  const el = t instanceof Element ? t : (t && t.parentElement) || null;
  const a = el ? el.closest("a[data-refkey]") : null;
  if (!a) return;
  const revealEl = a.closest(".reveal");
  if (!revealEl) return; // kein Reveal (z. B. Kachel-Canvas) → Default-Anker
  e.preventDefault();
  const slidesEl = revealEl.querySelector(".slides");
  if (!slidesEl) return;
  const h = Array.from(slidesEl.children)
    .filter((el) => el.tagName === "SECTION")
    .findIndex((el) => el.classList.contains("quellen"));
  if (h !== -1) {
    // Mini-Vorschau ohne Quellen-Folie (h === -1): nichts tun — das volle
    // Deck (nach dem Upgrade) enthält sie als letzte Folie.
    const inst = _slideReveals.get(revealEl);
    if (inst) {
      try { inst.slide(h, 0); } catch (err) { /* ältere Instanz */ }
    }
  }
  _flashRefEntry(slidesEl, a.getAttribute("data-refkey"));
});

function _flashRefEntry(slidesEl, key) {
  if (!key) return;
  const entry = slidesEl.querySelector('.aicampus-bibentry[data-refkey="' + key + '"]');
  if (!entry) return;
  entry.classList.remove("ref-flash");
  void entry.offsetWidth; // Reflow, damit ein erneutes Hinzufügen den Fade neu startet
  entry.classList.add("ref-flash");
  clearTimeout(entry._refFlashTimer);
  entry._refFlashTimer = setTimeout(() => entry.classList.remove("ref-flash"), 1500);
  entry.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/** Block mit ≥2 `--`-Segmenten → Stack: reiner (leerer) Container-Eltern
 *  mit `children` — jede Unterfolie wird wie eine normale Folie geparsed
 *  (eigene Direktiven, Layout, Notiz, Hintergrund; Spiegel von parse_slides
 *  in slides_service.py). */
function _parseStackBlock(segments, index) {
  const stack = _parseSlideBlock("", index);
  stack.children = segments.map((seg) => _parseSlideBlock(seg, index));
  return stack;
}

/** Anzahl anzeigbarer (Blatt-)Folien: normale Folie zählt 1, Stack zählt
 *  seine Unterfolien (stimmt mit Reveal's Folienzähler überein). */
function countSlides(slides) {
  return slides.reduce((n, s) => n + (s.children.length > 0 ? s.children.length : 1), 0);
}

/** Rendert eine Folie in ein Element (<section> oder .slides-canvas).
 *  slideMode: true → slide-eigene Gleichungs-Labels bekommen (S1), (S2), …
 *  und Skript-Labels verlinken zur Gleichung im Skript. */
async function renderSlideInto(slide, container, slidePos) {
  container.innerHTML = "";
  // Teil-Index (p) pari zu _slide_md_parts in api/slides.py: höchstens ein
  // (nicht-leerer) Teil → p = 0 (Spalten sind Renderer-Seite, s. oben).
  // Nur relevant, wenn slidePos gesetzt ist (S-Nummern unlabeled
  // Subfigure-Komplexe aus der slides-refmap).
  const md = slide.columns[0] || "";
  if (md) await renderMarkdown(md, container, { slideMode: true, slidePos: slidePos ? { ...slidePos, p: 0 } : null });
}

/* Seitenverhältnis (Breite/Höhe) aus dem Theme — kommt als CSS-Variable
 * --slides-aspect von :root (bzw. inline bei der Design-Vorschau). */
function slideAspectRatio(el) {
  try {
    const v = getComputedStyle(el).getPropertyValue("--slides-aspect").trim();
    const n = parseFloat(v);
    if (isFinite(n) && n > 0) return n;
  } catch (e) { /* fall through */ }
  return 16 / 9;
}

/**
 * Kachel-Vorschau: erste Folie in 960×(960/Ratio) rendern, auf die
 * Kachelbreite skalieren. thumbEl = .slides-thumb (aspect-ratio aus Theme).
 */
async function renderSlideThumb(slide, thumbEl, themeClass) {
  thumbEl.innerHTML = "";
  const ratio = slideAspectRatio(thumbEl);
  const W = 960;
  const H = Math.round(W / ratio);
  const s = (thumbEl.clientWidth / W) || 0.3;

  const scaler = document.createElement("div");
  scaler.className = "slides-thumb-scaler";
  scaler.style.width = W + "px";
  scaler.style.height = H + "px";
  scaler.style.transform = `scale(${s})`;

  const canvas = document.createElement("div");
  canvas.className = "slides-canvas layout-" + slide.layout + (themeClass ? " theme-" + themeClass : "");
  scaler.appendChild(canvas);
  thumbEl.appendChild(scaler);

  await renderSlideInto(slide, canvas);
}

/**
 * Baut ein <section> für Reveal: Layout-Klasse, Transition, optionaler
 * Fußzeilen-Text (data-footer), gerenderte Folie + Sprechernotiz.
 *
 * Die Notiz wird als <aside class="notes"> gerendert (Nicht das
 * data-notes-Attribut): das lokale Reveal-NOTES-Plugin liest in seiner
 * sendState()-Funktion ausschließlich die aside.notes-Elemente — das
 * Attribut würde dort durch den (leeren) aside.notes-Branch überschrieben
 * und in der Speaker-View nie angezeigt.
 */
async function buildSlideSection(slide, footerText, slidePos) {
  // Vertikaler Stack (`--`-Unterfolien): Reveal-Nest <section>
  // <section>…</section>…</section>. Der Eltern-Section ist ein reiner
  // (inhaltloser) Container ohne Layout-Klasse — Reveal erkennt am
  // <section>-Kind automatisch den Stack; jede Unterfolie wird wie eine
  // normale Folie gebaut (eigene Transition/Notiz/Hintergrund).
  if (slide.children && slide.children.length > 0) {
    const stack = document.createElement("section");
    stack.className = "slides-stack";
    for (let v = 0; v < slide.children.length; v++) {
      // v = Unterfolien-Index (Parität zur slides-refmap-Iteration).
      const pos = slidePos ? { ...slidePos, v } : null;
      stack.appendChild(await buildSlideSection(slide.children[v], footerText, pos));
    }
    return stack;
  }
  const section = document.createElement("section");
  section.className = "layout-" + slide.layout + (slide.css_class ? " " + slide.css_class : "");
  // Standard-Transition: Auto-Animate (Inhalte animieren zwischen den
  // Folien ineinander). "transition: fade|slide|zoom|none" setzt eine
  // klassische Transition (und damit kein data-auto-animate). Gezoomte
  // Applets ({zoom=X}) überspringt das Auto-Animate per
  // autoAnimateMatcher-Config (siehe aicampusAutoAnimateMatcher) — sie
  // faden ein/aus, der Inline-Transform-Zoom bleibt erhalten.
  const transition = slide.transition || SLIDE_DEFAULT_TRANSITION;
  if (footerText) section.setAttribute("data-footer", footerText);
  const pos = slidePos ? { ...slidePos, v: 0 } : null;
  await renderSlideInto(slide, section, pos);
  // Folien-Hintergrund (background: ![Titel](…)):
  // - Präsentation/Vorschau: Reveal's natives Hintergrund-System
  //   (data-background-image / data-background-iframe) → deckt den ganzen
  //   Viewport ab, auch die Letterbox, in die Reveal .slides skaliert.
  //   .html-Applets UND externe Websites bleiben per
  //   data-background-interactive klickbar (Reveal-Design: dann ist der
  //   Folieninhalt dieser Folie nicht klickbar).
  // - ?print-pdf: Element IN der Folie (pro PDF-Seite; Applet ist
  //   interaktiv, damit Einstellungen vor dem Druck angepasst werden
  //   können) — Reveal's Bg-System ist im Print-Modus ausgeblendet.
  //   NUR NACH renderSlideInto bauen — die setzt innerHTML und würde das
  //   Element sonst löschen.
  if (slide.background) {
    // .html-Applet oder externe Website (http(s)-URL ohne Bild-Endung)
    // → Iframe; Erkennung/Sandbox-Regeln wie in markdown-renderer.js.
    const isApplet = isAppletSrc(slide.background.src);
    if (SLIDES_IS_PRINT_PDF) {
      const bg = document.createElement(isApplet ? "iframe" : "img");
      bg.className = "aicampus-slide-bg";
      bg.src = slide.background.src;
      let appletZoom = null;
      if (isApplet) {
        bg.setAttribute("sandbox", appletSandboxAttr(slide.background.src));
        bg.title = slide.background.alt;
        bg.loading = "lazy";
        const z = slide.background.zoom;
        if (z) {
          // Zoom wie bei 6a (markdown-renderer.js): Layout-Box 1/zoom +
          // transform scale — sichtbare Fläche unverändert, Inhalt ×z
          // (oben links verankert). Inline schlägt die 100%-Regel für
          // .aicampus-slide-bg (slides.css). Bei z<1 ist die Box größer
          // als die Folie → Clip-Wrapper (s. .aicampus-slide-bg-clip).
          bg.style.width = `calc(100% / ${z})`;
          bg.style.height = `calc(100% / ${z})`;
          bg.style.transform = `scale(${z})`;
          bg.style.transformOrigin = "0 0";
          appletZoom = z;
        }
      } else {
        bg.alt = slide.background.alt;
      }
      section.classList.add("slides-has-bg");
      if (appletZoom) {
        // Sections sind im print-pdf-Modus overflow:visible → ein
        // rausgezoomtes (größerer Box) Bg würde in die Nachbar-PDF-Seiten
        // bluten. In einem foliengroßen Clip-Wrapper abschneiden.
        const clip = document.createElement("div");
        clip.className = "aicampus-slide-bg-clip";
        clip.appendChild(bg);
        section.appendChild(clip);
      } else {
        section.appendChild(bg);
      }
    } else if (isApplet) {
      section.setAttribute("data-background-iframe", slide.background.src);
      section.setAttribute("data-background-interactive", "");
      // Reveal kennt kein natives Bg-Iframe-Zoom → wireBgZoom liest den
      // Faktor aus diesem Attribut und skaliert das (lazy) angelegte Iframe.
      if (slide.background.zoom) {
        section.setAttribute("data-background-zoom", String(slide.background.zoom));
      }
    } else {
      section.setAttribute("data-background-image", slide.background.src);
    }
  }
  if (transition === "autoanimate") {
    section.setAttribute("data-auto-animate", "");
  } else {
    section.setAttribute("data-transition", transition);
  }
  if (slide.notes) {
    const aside = document.createElement("aside");
    aside.className = "notes";
    aside.textContent = slide.notes; // plain text → kein HTML-Injection-Risiko
    section.appendChild(aside);
  }
  return section;
}

// transition-Werte, die reveal.css als Wrapper-Klasse kennt (s. reveal.css:
// .reveal.slide / .reveal.fade / .reveal.none / …). Mehr als wir in
// SLIDE_TRANSITIONS exponieren, damit auch fremde data-transition-Werte
// sauber geräumt/gesetzt werden.
const SLIDE_TRANSITION_CLASSES = ["none", "fade", "slide", "convex", "concave", "zoom", "linear"];

/**
 * Per-Slide-Transition (transition: fade|slide|zoom|none) intuitiv machen:
 * Reveal's data-transition wirkt CSS-seitig NUR auf die eigene Sektion —
 * die AUSSCHIEDENDE Folie animiert mit ihrer eigenen Transition bzw. (bei
 * Standard-Auto-Animate-Folien ohne data-transition) mit der GLOBALEN
 * Wrapper-Klasse (config.transition). Ohne Korrektur: hybride Übergänge
 * (Ausgang global slide + Eintreffen fade) und „transition: none“ schneidet
 * nur die Zielfolie ab, während die Ausgangsfolie noch wegglipt.
 *
 * Korrektur: Die explizite Transition der ZIEL-Folie gilt für den ganzen
 * Wechsel — beim beforeslidechange (feuert VOR updateSlides(), also noch
 * vor der past/present-Klassenänderung → ein einziger Style-Recalc mit
 * finaler Wrapper-Klasse) wird die Wrapper-Klasse auf die
 * Ziel-Transition gesetzt. Ohne explizite Ziel-Transition gilt die
 * Deck-Standard-Transition (config.transition). Auto-Animate-Paare (beide
 * Folien data-auto-animate) laufen über Reveal's AutoAnimate mit
 * disable-slide-transitions und sind von der Wrapper-Klasse unabhängig.
 *
 * Pro Reveal-Instanz aufrufen (Editor: pro renderPreview — configure()
 * räumt nur die eigene Config-Transition auf, nicht Reste älterer
 * Instanzen; deshalb setzt der Start-Reset unten die Wrapper-Klasse
 * aktiv zurück).
 */
function wirePerSlideTransitions(reveal, revealRootEl) {
  const global = reveal.getConfig().transition || "none";
  const setWrapper = (name) => {
    if (SLIDE_TRANSITION_CLASSES.indexOf(name) === -1) name = global;
    SLIDE_TRANSITION_CLASSES.forEach((c) => revealRootEl.classList.remove(c));
    revealRootEl.classList.add(name);
  };
  setWrapper(global);
  reveal.on("beforeslidechange", (ev) => {
    // Indexe je nach Reveal-Version im Event selbst oder in ev.detail
    const h = ev.indexh ?? (ev.detail && ev.detail.indexh);
    const v = ev.indexv ?? (ev.detail && ev.detail.indexv);
    if (h == null) return;
    const slidesEl = revealRootEl.querySelector(".slides");
    if (!slidesEl) return;
    let dest = slidesEl.querySelectorAll(":scope > section")[h];
    if (!dest) return;
    // Vertikaler Stack: Ziel = die aktive Unterfolie (Index aus dem
    // Event, geclamped).
    const vSlides = dest.querySelectorAll(":scope > section");
    if (vSlides.length) {
      dest = vSlides[Math.max(0, Math.min(v | 0, vSlides.length - 1))] || dest;
    }
    const t = dest.getAttribute("data-transition");
    setWrapper(SLIDE_TRANSITION_CLASSES.indexOf(t) !== -1 ? t : global);
  });
}

/**
 * Folien-Hintergrund-Zoom ({zoom=X} bei background: ![…](….html/Website)):
 * Reveal legt das Bg-Iframe mit 100%×100% an und skaliert es nicht
 * (data-background-size wirkt nur auf CSS-Background-Images) → den Zoom
 * setzen wir selbst: Layout-Box 1/zoom + transform scale(zoom) — exakt wie
 * der Applet-Zoom bei 6a (markdown-renderer.js): sichtbare Fläche bleibt
 * die Folie, der Iframe-Inhalt wird ×zoom größer (oben links verankert;
 * das Applet layoutet in der 1/zoom-großen Box und wird vergrößert).
 *
 * MÜSSE NACH `await reveal.initialize()` aufgerufen werden (wie
 * wireKatexFragmentResort): Das initiale slidechanged feuert während
 * start(), also VOR der Listener-Registrierung → die aktuelle Folie wird
 * direkt angewendet. backgrounds.update() (lazy Iframe-Anlage) läuft
 * synchron NACH dem slidechanged-Dispatch → erst im nächsten Frame greift
 * der Iframe (requestAnimationFrame).
 */
function wireBgZoom(reveal) {
  const apply = (section) => {
    if (!section) return;
    const z = parseFloat(section.getAttribute("data-background-zoom"));
    const content = section.slideBackgroundContentElement;
    const iframe = content && content.querySelector ? content.querySelector("iframe") : null;
    if (!iframe) return;
    if (!z) return; // kein Zoom → Reveal's 100%×100% bleibt wirksam
    // Reveal legt das Bg-Iframe mit max-width/max-height: 100% inline an —
    // bei z<1 würde die 1/zoom-Box damit auf die Foliengröße gekappt und
    // der Inhalt in die obere-linke Ecke geschrumpft. Caps aufheben
    // (Präzedenz: reveal.css hebt genau diese Caps für Video-Bgs auf).
    iframe.style.maxWidth = "none";
    iframe.style.maxHeight = "none";
    iframe.style.width = `calc(100% / ${z})`;
    iframe.style.height = `calc(100% / ${z})`;
    iframe.style.transform = `scale(${z})`;
    iframe.style.transformOrigin = "0 0";
  };
  // Aktuelle Folie (Deep-Link #/n: der Bg ist nach initialize() geladen).
  apply(reveal.getCurrentSlide());
  reveal.on("slidechanged", (e) => {
    requestAnimationFrame(() => apply(e.currentSlide));
  });
}

/**
 * Reveal-Config `autoAnimateMatcher`: Auto-Animate-Paare filtern und
 * gezoomte Applets (data-zoom) überspringen — sonst würde AutoAnimate den
 * Inline-Transform (scale) des Iframes animieren/zerstören. Ausgefilterte
 * Elemente werden von Reveal als nicht passende behandelt (ein-/ausfaden),
 * ihr transform-Zoom bleibt unangetastet.
 *
 * Wichtig: `this` ist die AutoAnimate-Instanz (Reveal ruft den Matcher per
 * matcher.call(this, fromSlide, toSlide) auf) → getAutoAnimatePairs steht
 * hier als Instanz-Methode zur Verfügung.
 */
function aicampusAutoAnimateMatcher(fromSlide, toSlide) {
  const pairs = this.getAutoAnimatePairs(fromSlide, toSlide);
  return pairs.filter(
    (pair) => !pair.from.hasAttribute("data-zoom") && !pair.to.hasAttribute("data-zoom")
  );
}

/**
 * Fragment-Schritte neu ableiten, sobald eine Folie gelayoutet ist (s.
 * aicampusResortFragments in markdown-renderer.js). Beim Markdown-Render
 * sind die Sections noch display:none (Reveal-CSS), daher kann die
 * visuelle Fragment-Reihenfolge (z. B. KaTeX-Underbraces) erst richtig
 * sortiert werden, wenn die Folie sichtbar ist — bzw. im Print-Modus,
 * noch bevor Reveal's setupPDF die Fragments auf PDF-Seiten paginiert.
 *
 * MÜSSE NACH `await reveal.initialize()` aufgerufen werden: Der Aufruf
 * läuft dann exakt zu dem Zeitpunkt, an dem Reveal's "ready" gerade
 * gefeuert hat — ein `.on("ready")`-Listener wäre zu spät registriert
 * (Reveal feuert es via setTimeout(1ms) aus start(); das Rennen mit
 * setupPDF's erstem rAF geht je nach Umgebung unterschiedlich aus, s. unten).
 *
 * @param {object} reveal initialisierte Reveal-Instanz
 * @param {HTMLElement} slidesEl Element, das die Sections enthält (.slides)
 */
function wireKatexFragmentResort(reveal, slidesEl) {
  const resort = (slide, resync) => {
    if (!slide || !slide.querySelector(".markdown-preview")) return;
    aicampusResortFragments(slide);
    // Sichtbarkeit an die (ggf. neuen) Indizes anpassen: Ohne angezeigte
    // Fragments ist data-fragment=-1 (Ausgangszustand); bei einem Deep-Link
    // (#/2/1) zeigt der alte Index auf den zugehörigen visuellen Schritt.
    // Im Print-Modus resync=false — Reveal's setupPDF verwaltet den
    // Fragment-Visibility-Zustand pro PDF-Seite selbst (ein vorheriges
    // .visible würde in die per Fragment-State geklonten Seiten wandern).
    if (resync) {
      try {
        reveal.fragments.update(
          parseInt(slide.getAttribute("data-fragment") || "-1", 10)
        );
      } catch (e) { /* ignore */ }
    }
  };
  if (/print-pdf/.test(window.location.search)) {
    // Die Ableitung muss VOR setupPDF's Fragment-Paginierung laufen (die
    // erfolgt 2 Animation-Frame nach dem Setzen von html.print-pdf).
    // Zwei Timing-Varianten: Unter Headless kann setupPDF's erster rAF
    // VOR Reveal's 1ms-Ready-Timer kommen → die Klasse ist dann beim
    // Aufruf bereits gesetzt (→ jetzt synchron ableiten; die Sections
    // sind gelayoutet und die Rects gültig). Andernfalls fängt der
    // MutationObserver das Class-Set synchron (Microtask) ab — ebenfalls
    // noch vor der Paginierung.
    const doPrintResort = () => {
      slidesEl.querySelectorAll("section").forEach((s) => {
        // Stack-Eltern (`--`-Stapel) NIEMALS als Container resorten:
        // Die [data-frag]-Sammlung würde dann über ALLE Kinder-Folien
        // hinweg laufen (flache, cross-slide-Liste) und Gates früherer
        // Unterfolien würden den Inhalt späterer Unterfolien "verschlingen"
        // → falsche Fragment-Schritte + leere Print-Seiten. Die Kinder
        // werden in derselben Schleife einzeln (korrekt) abgearbeitet.
        if (s.querySelector(":scope > section")) return;
        resort(s, false);
      });
    };
    if (document.documentElement.classList.contains("print-pdf")) {
      doPrintResort();
    } else {
      const mo = new MutationObserver(() => {
        if (!document.documentElement.classList.contains("print-pdf")) return;
        mo.disconnect();
        doPrintResort();
      });
      mo.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["class"],
      });
    }
  } else {
    // Präsentation/Vorschau: "ready" hat beim Aufruf bereits gefeuert
    // (initialize() löst genau dort auf) → direkt für die aktuelle Folie
    // ableiten; ein .on("ready")-Listener wäre zu spät registriert. Das
    // deckt auch Deep-Links (#/2/1) ab — das initiale slidechanged feuert
    // während start(), also VOR der Listener-Registrierung.
    resort(reveal.getCurrentSlide(), true);
    reveal.on("slidechanged", (e) => resort(e.currentSlide, true));
  }
}
