// ─────────────────────────────────────────────────────────────────────
// Zugriffsstufe eines Slide-Decks (Übersicht-Kachel + Editor): ein Button mit
// EINEM Icon; Klick öffnet ein kleines Menü mit den drei Stufen + Erklärung.
//   <button data-deck-access data-course="1" data-deck="2" data-level="hidden|visible|notes">
// Setzt per PUT /api/courses/{c}/slides/{d}/access (Sichtbarkeit + Notizen-Freigabe)
// und lädt danach die Seite neu. Siehe docs/LLM-plans/slide-access-levels-plan.md.
// ─────────────────────────────────────────────────────────────────────
(function () {
  const SVG = (inner) => '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + "</svg>";
  const EYE = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>';
  const EYE_OFF = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a16.54 16.54 0 0 1 4.1-5.79"></path><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.55 18.55 0 0 1-2.06 3.28"></path><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line>';
  // Auge (links oben, verkleinert) + kleiner Stift (rechts unten)
  const EYE_PEN = '<g transform="translate(1.5 .5) scale(.8)">' + EYE + '</g><g transform="translate(11 11) scale(.5)" stroke-width="3.4"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></g>';

  const LEVELS = [
    { key: "hidden", icon: SVG(EYE_OFF), color: "#d1d5db", title: "Versteckt", desc: "Nur Tutor/Prof sehen das Deck." },
    { key: "visible", icon: SVG(EYE), color: "#9ca3af", title: "Sichtbar ohne Notizen", desc: "Alle Kursteilnehmenden sehen das Deck, aber keine Notizen." },
    { key: "notes", icon: SVG(EYE_PEN), color: "#9ca3af", title: "Sichtbar mit Notizen", desc: "Zusätzlich sehen sie deine Stift-, Whiteboard- und Laser-Notizen." },
  ];
  const by = Object.fromEntries(LEVELS.map((l) => [l.key, l]));
  let menu = null;

  function closeMenu() {
    if (!menu) return;
    menu.remove();
    menu = null;
    document.removeEventListener("keydown", onKey, true);
  }
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); closeMenu(); } };

  async function setLevel(btn, level) {
    closeMenu();
    if (level === btn.dataset.level) return;
    try {
      const r = await fetch(`/api/courses/${btn.dataset.course}/slides/${btn.dataset.deck}/access`, {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Fehler.");
      window.location.reload();
    } catch (err) {
      if (typeof showToast === "function") showToast("Fehler: " + err.message, "error");
      else alert("Fehler: " + err.message);
    }
  }

  function openMenu(btn) {
    closeMenu();
    menu = document.createElement("div");
    menu.setAttribute("data-deck-access-menu", "");
    menu.style.cssText = "position:fixed;z-index:100000;width:290px;background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
      + "box-shadow:0 10px 30px rgba(0,0,0,.18);padding:4px;font-family:inherit";
    for (const l of LEVELS) {
      const b = document.createElement("button");
      b.type = "button";
      const cur = l.key === btn.dataset.level;
      b.style.cssText = "display:flex;gap:10px;align-items:flex-start;width:100%;text-align:left;padding:8px 10px;border:0;border-radius:8px;cursor:pointer;"
        + "background:" + (cur ? "#eff6ff" : "transparent");
      b.onmouseenter = () => { if (!cur) b.style.background = "#f3f4f6"; };
      b.onmouseleave = () => { b.style.background = cur ? "#eff6ff" : "transparent"; };
      b.innerHTML = '<span style="color:#6b7280;margin-top:2px;display:inline-flex">' + l.icon + "</span>"
        + '<span style="flex:1"><span style="display:block;font-size:13px;font-weight:600;color:#111827">' + l.title + "</span>"
        + '<span style="display:block;font-size:12px;color:#6b7280">' + l.desc + "</span></span>"
        + (cur ? '<span style="color:#2563eb;font-size:14px">✓</span>' : "");
      b.addEventListener("click", () => setLevel(btn, l.key));
      menu.appendChild(b);
    }
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    const w = menu.offsetWidth;
    menu.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
    const h = menu.offsetHeight;
    // unterhalb des Buttons; passt es nicht in den Viewport → darüber
    menu.style.top = (r.bottom + 6 + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 6) : r.bottom + 6) + "px";
    document.addEventListener("keydown", onKey, true);
  }

  function init(root) {
    root.querySelectorAll("[data-deck-access]").forEach((btn) => {
      const l = by[btn.dataset.level] || by.visible;
      btn.innerHTML = l.icon;
      btn.style.color = l.color;
      btn.title = l.title + " – Zugriff ändern";
      btn.setAttribute("aria-haspopup", "true");
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (menu) closeMenu(); else openMenu(btn);
      });
    });
  }
  document.addEventListener("click", (e) => { if (menu && !menu.contains(e.target)) closeMenu(); });
  window.aicampusDeckAccess = { init, LEVELS };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => init(document));
  else init(document);
})();
