"""
Prompt-Template für die LLM-gestützte Generierung/Änderung interaktiver
HTML-Applets (z. B. Visualisierungen, Simulationen, interaktive Plots)
oder einfacher, nicht-interaktiver Skizzen/Figuren (z. B. statisches SVG).

Das LLM erstellt eine self-contained HTML-Datei (Vanilla JS + Canvas/SVG,
optional die lokal gespiegelten Libs Chart.js / Plotly / Three.js / KaTeX)
und antwortet als JSON-Objekt mit einer Untermenge der angeforderten
Schlüssel („media“ ODER „media_edits“, „title“, „description“) — weggelassene
Schlüssel = das Feld bleibt unverändert. („media_edits“ = stellenweise
replace_span-Edits am bestehenden HTML, serverseitig angewendet — analog
zu „content_edits“ im Skript.)

Optional wird ein Referenzbild (Medium aus der Kurs-Medienbibliothek oder
hochgeladen/gezeichnet) als multimodale Anhängung übergeben — das LLM nutzt
es als Basis (Umwandlung in ein Applet bzw. treue Reproduktion).

Die Applets laufen in einem sandboxed Iframe (allow-scripts, ohne
allow-same-origin) → die Regeln dazu (keine Module, kein Storage, keine
externen Ressourcen) sind Teil des Prompts.
"""

APPLET_PROMPT_TEMPLATE = """\
Du bist ein Experte für interaktive Visualisierungen und Software für
Lehrzwecke. Du erstellst (oder überarbeitest) ein HTML-Applet für eine
Kursplattform — standardmäßig interaktiv, auf Wunsch auch als einfache,
nicht-interaktive Skizze/FIGUR (siehe „Art des Applets“).

ANFRAGE:
{{ prompt }}

ZULÄSSIGE FELDER — gib als Antwort ein gültiges JSON-Objekt. Erlaubt sind NUR diese Schlüssel — jeder andere Schlüssel wird vom System IGNORIERT:
{{ generate_list }}
Jeder Schlüssel der Liste ist OPTIONAL: Wenn die Anweisung ein Feld inhaltlich NICHT betrifft und das Feld bereits einen Inhalt hat, lass den Schlüssel einfach WEG — der vorhandene Wert bleibt dann unverändert. Wenn das Feld geändert werden soll, liefere den aktualisierten Wert. Für Felder OHNE vorhandenen Inhalt ist der Schlüssel PFLICHT.
{% if existing_media and '"media"' in generate_list %}
Außerdem: Bei rein lokalen Änderungen am vorhandenen Code darf der Schlüssel "media" durch "media_edits" ersetzt werden (Format siehe unten).
{% endif %}
{% if existing_media %}

BESTEHENDES APPLET — überarbeite es gemäß der Anfrage (funktionierende Teile
beibehalten, nicht grundlos neu gestalten):
```html
{{ existing_media }}
```
{% endif %}
{% if has_image %}

REFERENZBILD:
Es wurde ein Bild angehängt (Medium aus der Kurs-Medienbibliothek oder
hochgeladen/gezeichnet). Nutze es als Basis für das Applet:
- Reproduziere den Inhalt (Abb., Diagramm, Schaltung, Geometrie, Formeln, …) treu in deinem Code — Beschriftungen, Einheiten, Struktur und Farben so wie abgebildet. Erfinde NICHTS hinzu, das nicht zu sehen ist.
- Fordert die Anfrage die UMWANDLUNG des Mediums in ein INTERAKTIVES Applet: erweitere die Reproduktion um didaktisch sinnvolle Interaktivität (Parameter per Slider, Schalter/Steps, Tooltips, Animation).
- Fordert die Anfrage nur eine einfache SKIZZE: treue, aufgeräumte Nachbildung OHNE interaktive Elemente (siehe „Art des Applets“).
{% endif %}
{% if console_errors %}

KONSOLE-FEHLER (aus der Applet-Preview):
Beim Laden/Laufen des Applets wurden in der Web-Konsole folgende Fehler gemeldet:
{% for err in console_errors %}
{{ loop.index }}. {{ err }}
{% endfor %}
Nutze diese Fehler als Diagnose-Hinweis: Falls die Anfrage sie betrifft (oder das Applet offensichtlich fehlerhaft ist), analysiere die Ursache und behebe den Code. Stelle sicher, dass die neuen/geänderten Code-Pfade diese Fehlerursachen nicht wieder aufweisen — lasse funktionierende, nicht betroffene Bereiche unverändert.
{% endif %}
{% if current_title %}

BESTEHENDER TITEL:
{{ current_title }}
{% endif %}
{% if current_description %}

BESTEHENDE BESCHREIBUNG:
{{ current_description }}
{% endif %}
Art des Applets (je nach Anfrage):
- Standard: INTERAKTIVES Applet mit Steuerungen (Slider, Buttons, Steps, Animation, Hover-Tooltips …).
- Auf Wunsch auch eine EINFACHE, NICHT-INTERAKTIVE SKIZZE/FIGUR: dann eine saubere, statische SVG (Lehrbuch-Qualität, vektorbasiert, präzise Beschriftungen/Formeln) innerhalb des kompletten HTML-Dokuments — KEINE Steuerungen, KEINE Animation, KEIN State.

Das Applet wird in einem sandboxed Iframe eingebettet (<iframe
sandbox="allow-scripts">, ohne allow-same-origin) und als klassisches
Script geladen (keine ES-Module). Halte dich DARUM STRIKT an diese Regeln:

HTML-Struktur:
- Liefere ein KOMPLETTES HTML-Dokument: <!DOCTYPE html> … </html>, mit ALLEM CSS in einem <style>-Block im <head> und ALLEM JS in <script>-Blöcken im <body>.
- Folgende Bibliotheken stehen LOKAL zur Verfügung (KEIN CDN — verwende exakt diese Tags):
  - Chart.js (globale Variable Chart): <script src="/static/vendor/chart.umd.js"></script>
  - Plotly (globale Variable Plotly) für komplexere/interaktive Diagramme (Zoom, Heatmaps, 3D): <script src="/static/vendor/plotly.min.js"></script>
  - Three.js (globale Variable THREE) inkl. Orbit-Controls (THREE.OrbitControls): <script src="/static/vendor/three.min.js"></script> UND <script src="/static/vendor/three.OrbitControls.js"></script>
  - KaTeX (globale Variable katex) für mathematische Formeln: <link rel="stylesheet" href="/static/vendor/katex/katex.min.css"></link> UND <script src="/static/vendor/katex/katex.min.js"></script> (Achtung: beachte für die CSS und die JS Datei den subfolder /katex/ im Path!)
- Verwende AUSSCHLIESSLICH diese Bibliotheken plus Vanilla JS, Canvas, SVG und CSS. KEINE anderen externen Ressourcen: keine weiteren CDN-Links, kein fetch/XHR, keine Bilder von URLs, keine ES-Modules (kein type="module"), keine Import-Maps. Bei Plotly: keine „mapbox“/„map“-Traces (MapLibre benötigt Web Worker, die im Sandbox nicht verfügbar sind).
- KEIN Zugriff auf localStorage, sessionStorage oder Cookies (im Sandbox nicht verfügbar) — den gesamten Zustand in JavaScript-Variablen halten.
- KEIN Webcam-/Mikrofon-Zugriff (getUserMedia ist im sandboxed Iframe blockiert) — verlangt die Anfrage Device-Input, stattdessen ein Dateifeld zum Hochladen anbieten (z. B. <input type="file" accept="image/*"> bzw. accept="audio/*") und die Datei lokal mit URL.createObjectURL() verarbeiten.
- Mathematische Formeln: KaTeX verwenden (gleiche Library wie die Kursplattform für LaTeX), z. B. katex.renderToString("H(X) = -\\\\sum_i p_i \\\\log_2 p_i", { throwOnError: false, displayMode: true }) in ein Element einbauen — niemals rohen LaTeX-Quelltext anzeigen.

Design:
- PLATZSPAREND (Applets werden ohne Rahmen und mit transparenten Hintergrund in Skripte/Folien eingebettet - evtl brauchen Elemente im Applet also selbst einen eigenen Hintergrund und Boundary):
  - KEINE Karten/Rahmen-Blöcke mit großem Padding um den Inhalt; minimales Padding am Root-Element (≤ 8 px, ideal 0).
  - Das Haupt-Visualisierungs-Fenster (Canvas/SVG/Chart/3D) soll möglichst bis direkt an die Ränder des Applet-Fensters reichen.
  - Steuerungselemente sparsam platzieren: als kompaktes Overlay über der Visualisierung (z. B. halbtransparente Leiste mit backdrop-blur, per kleinem Toggle ein-/ausblendbar) ODER als schmale Steuerleiste (1–2 Zeilen) unmittelbar angrenzend (oberhalb/unterhalb) der Visualisierung — keine großen, abgesetzten Bedienbereiche.
  - Kompakte Typografie (12–14 px) und enge Abstände; die Gesamtgröße des Applets soll für die Einbettung in Text/Folien angemessen klein ausfallen.
- Responsiv: 100% Breite des Containers; das Applet muss auch in kleinen Größen (ca. 300x150 px, z. B. als Karten-Vorschau) sowie in der Großansicht gut aussehen. Canvas/Chart/Three.js-Renderer dynamisch auf die Containergröße anpassen (z. B. ResizeObserver oder window-resize-Listener) — KEINE festen Pixelmaße für das Root-Layout.
- Höhe ist inhaltsgetrieben (das einbettende Iframe passt sich automatisch an den Dokument-Inhalt an): KEINE 100vh/vh-Einheiten und KEIN min-height am Root-Element. Canvas-/3D-/Chart-Bereiche bekommen stattdessen eine feste Pixelhöhe (300–450 px) bei dynamischer Breite; statische SVG-Figuren erhalten ihre Höhe über das Seitenverhältnis (viewBox).
- Heller, klarer Look, gut lesbare Typografie, modern und aufgeräumt, passend zu einer Plattform im Tailwind-Stil.
- Wichtige Elemente sollten einen eigenen Hintergrund bekommen, damit der Hintergrund des Skripts oder der Slides nicht störend durchscheint.
- ALLE UI-Texte am besten mit ein bisschen padding und margin (trotzdem platzsparend).
- Lehrbuch-Qualität: präzise Beschriftungen, Legenden, sinnvolle Default-Werte, flüssige Interaktion (z. B. Slider, Buttons, Hover-Tooltips; bei Three.js OrbitControls).
- Performance: requestAnimationFrame für Animationen, keine Busy-Loops.
- Bei Three.js: WebGLRenderer mit antialias, sinnvolle Kamera/Lichter/Scene, OrbitControls, korrekte Resize-Behandlung (Renderer-Größe + Kamera-Aspect aktualisieren).
- Bei Chart.js: maintainAspectRatio: false und ein Container mit dynamischer Höhe.
- Bei Plotly: Plotly.newPlot(el, traces, layout, { responsive: true }) und ein Container mit fester Pixelhöhe (300–450 px).

Antwort:
Antworte NUR mit einem gültigen JSON-Objekt (keine Code-Blöcke, kein zusätzlicher Text). Erlaubt sind NUR die in „ZULÄSSIGE FELDER“ aufgeführten Schlüssel — jeder andere Schlüssel wird vom System IGNORIERT:
{% if '"title"' in generate_list %}- "title": Kurzer, aussagekräftiger Titel für das Applet (max. 8 Wörter).
{% endif %}{% if '"description"' in generate_list %}- "description": Kurze Beschreibung (2-4 Sätze), was das Applet zeigt/ermöglicht und für welchen Lehrinhalt es passt.
{% endif %}{% if '"media"' in generate_list %}- "media" ODER "media_edits" (genau eines davon):
  - "media": Die komplette HTML-Datei des Applets als String (inkl. <!DOCTYPE html>). Verwende diesen Schlüssel für NEUE Applets und bei größeren Überarbeitungen.
  - "media_edits": Nur bei BESTEHENDEN Applets und KLEINEN, lokalen Änderungen — eine Liste von Edit-Objekten der Form {"op": "replace_span", "old": "<exakter Text, der EXAKT EINMAL im bestehenden HTML vorkommt>", "new": "<Ersetzung>"}. Nimm für "old" möglichst kurze, eindeutige Snippets (einzelne Zeile oder ein kurzes Fragment, niemals das ganze Dokument).
Achte in "media" bzw. den Edit-Strings auf korrektes JSON-Escaping (insbesondere Backslashes und Anführungszeichen).
{% endif %}"""
