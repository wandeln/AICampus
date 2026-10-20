// ─────────────────────────────────────────────────────────────────────
// Live-Update: Kurs-Inhalte ändern sich (via MCP) → die offene Seite
// lädt NUR den betroffenen Inhalt nach, ohne Page-Reload.
//
// Nutzt den bestehenden User-Event-Stream der Glocke
// (/api/notifications/stream, s. base.html): Das Event dient nur als
// TRIGGER (course_id + entity_type + entity_id, OHNE Inhalt) — der
// Client holt sich den Inhalt über die normale API, d. h. Rechte
// bleiben enforced. Wie beim Forum: pro Page eine eigene EventSource.
//
// Usage (pro Content-Page):
//   aicampusWatchContent(courseId, (data) => {
//     // data = { course_id, entity_type, entity_id }
//   });
// ─────────────────────────────────────────────────────────────────────
(function () {
  const DEBOUNCE_MS = 700; // MCP-Edit-Serien → ein Refetch
  const timers = new Map();  // "type:id" → Debounce-Timer
  const pending = new Map(); // "type:id" → Event (Tab war hidden)
  const handlers = [];

  function schedule(data, onEvent) {
    const key = data.entity_type + ":" + data.entity_id;
    clearTimeout(timers.get(key));
    timers.set(key, setTimeout(() => {
      timers.delete(key);
      if (document.hidden) {
        pending.set(key, data); // Refetch erst, wenn der Tab wieder sichtbar ist
        return;
      }
      try {
        onEvent(data);
      } catch (e) {
        console.error("[content-live] Handler-Fehler:", e);
      }
    }, DEBOUNCE_MS));
  }

  // Tab wieder sichtbar → währenddessen eingegangene Events nachholen
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !pending.size) return;
    const events = [...pending.values()];
    pending.clear();
    for (const data of events) {
      const h = handlers.find((h) => h.courseId === data.course_id);
      if (h) h.onEvent(data);
    }
  });

  window.aicampusWatchContent = function (courseId, onEvent) {
    handlers.push({ courseId, onEvent });
    try {
      const es = new EventSource("/api/notifications/stream");
      es.onmessage = (e) => {
        let data;
        try {
          data = JSON.parse(e.data);
        } catch {
          return; // fremdes/ungültiges Event (z. B. Glocke)
        }
        if (data.event !== "content_updated" || data.course_id !== courseId) return;
        // Eingebettetes Applet geändert → Bust-Eintrag für den Renderer:
        // beim Re-Render bekommt die Medien-URL einen ?v=-Suffix. Ohne das
        // liefert Chromes Bild-Memory-Cache (keyed pro URL) beim in-place
        // Re-Render stummen Stale-Content — Cache-Header (no-cache/no-store)
        // werden für die bereits geladene URL nicht beachtet.
        if (data.origin_media) {
          (window.aicampusMediaBusts = window.aicampusMediaBusts || {})[data.origin_media] = Date.now();
        }
        schedule(data, onEvent);
      };
    } catch { /* EventSource unsupported → kein Live-Update */ }
  };
})();
