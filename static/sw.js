/* AICampus Service Worker (M3: Web Push via FCM)
 *
 * Läuft auf / (Scope = ganze App). Zuständig für:
 * - push: Push-Payload entschlüsseln (macht der Browser selbst) und als
 *   Browser-Benachrichtigung anzeigen — außer ein AICampus-Tab ist gerade
 *   sichtbar (dann zeigt die Glocke via SSE an → OS-Popup wäre doppelt).
 * - notificationclick: Benachrichtigung als gelesen markieren (SW-Fetch ist
 *   same-origin → access_token-Cookie wird automatisch mitgesendet) und den
 *   Ziel-Tab fokussieren bzw. öffnen.
 * - pushsubscriptionchange: Rotation der Push-Keys → neue Subscription
 *   ans Backend melden (gleicher Dedupe-Key: Endpoint).
 */

const ICON = "/static/aicampus_icon_bg_white.png";

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = (event.data && event.data.json()) || {};
  } catch (e) {
    data = {};
  }
  const options = {
    icon: ICON,
    body: data.body || "",
    tag: data.tag ? String(data.tag) : undefined,
    data: { url: data.url || "/", id: data.id || null },
  };

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: false })
      .then((clients) => {
        const visible = clients.some((client) => {
          try {
            return client.visibilityState === "visible";
          } catch (e) {
            return false;
          }
        });
        // Sichtbarer AICampus-Tab → Glocke (SSE) ist aktuell, kein OS-Popup.
        if (!visible) {
          return self.registration.showNotification(data.title || "AICampus", options);
        }
      })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const nData = event.notification.data || {};
  const url = nData.url || "/";

  event.waitUntil(
    (async () => {
      if (nData.id) {
        try {
          await fetch(`/api/notifications/${nData.id}/read`, { method: "POST" });
        } catch (e) {
          /* Session abgelaufen o. ä. — Benachrichtigung bleibt in der Glocke */
        }
      }
      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const existing = clients.find((c) => c.url === url);
      if (existing) {
        await existing.focus();
        return;
      }
      await self.clients.openWindow(url);
    })()
  );
});

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      const sub = await self.registration.pushManager.getSubscription();
      if (!sub) return;
      try {
        await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sub),
        });
      } catch (e) {
        /* Backend kurz nicht erreichbar — wird beim nächsten Start/Refresh gemeldet */
      }
    })()
  );
});
