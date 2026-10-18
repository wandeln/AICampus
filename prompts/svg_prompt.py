"""
Prompt-Template für die LLM-gestützte Generierung/Änderung von SVG-Applets
(vektorbasierte Grafiken für Kursplattformen — statisch oder animiert via
SMIL/CSS-@keyframes).

Das LLM erstellt ein rohes SVG-Dokument (kein HTML-Wrapper!) und antwortet
als JSON-Objekt mit einer Untermenge der angeforderten Schlüssel
(„media“ ODER „media_edits“, „title“, „description“) — weggelassene
Schlüssel = das Feld bleibt unverändert. („media_edits“ = stellenweise
replace_span-Edits am bestehenden SVG, serverseitig angewendet.)

SVGs werden über <img> eingebettet (kein JS, kein Sandbox-Iframe nötig) —
interaktive Elemente (Slider, Buttons, …) sind NICHT möglich; dafür gibt es
HTML-Applets (Formatwechsel im Applet-Studio möglich).

Optional wird ein Referenzbild (Medium aus der Kurs-Medienbibliothek oder
hochgeladen/gezeichnet) als multimodale Anhängung übergeben — das LLM nutzt
es als Basis (treue Vektor-Reproduktion).
"""

SVG_PROMPT_TEMPLATE = """\
Du bist ein Experte für technische Illustration und Vektorgrafik für
Lehrzwecke. Du erstellst (oder überarbeitest) ein SVG für eine Kursplattform.

ANFRAGE:
{{ prompt }}

ZULÄSSIGE FELDER — gib als Antwort ein gültiges JSON-Objekt. Erlaubt sind NUR diese Schlüssel — jeder andere Schlüssel wird vom System IGNORIERT:
{{ generate_list }}
Jeder Schlüssel der Liste ist OPTIONAL: Wenn die Anweisung ein Feld inhaltlich NICHT betrifft und das Feld bereits einen Inhalt hat, lass den Schlüssel einfach WEG — der vorhandene Wert bleibt dann unverändert. Wenn das Feld geändert werden soll, liefere den aktualisierten Wert. Für Felder OHNE vorhandenen Inhalt ist der Schlüssel PFLICHT.
{% if existing_media and '"media"' in generate_list %}
Außerdem: Bei rein lokalen Änderungen am vorhandenen Code darf der Schlüssel "media" durch "media_edits" ersetzt werden (Format siehe unten).
{% endif %}
{% if existing_media %}

BESTEHENDES SVG — überarbeite es gemäß der Anfrage (funktionierende Teile
beibehalten, nicht grundlos neu gestalten):
```svg
{{ existing_media }}
```
{% endif %}
{% if has_image %}

REFERENZBILD:
Es wurde ein Bild angehängt (Medium aus der Kurs-Medienbibliothek oder
hochgeladen/gezeichnet). Reproduziere den Inhalt (Abb., Diagramm, Schaltung,
Geometrie, Formeln, …) als VEKTOR-Grafik treu im SVG — Beschriftungen,
Einheiten, Struktur und Farben so wie abgebildet. Erfinde NICHTS hinzu, das
nicht zu sehen ist.
{% endif %}
{% if current_title %}

BESTEHENDER TITEL:
{{ current_title }}
{% endif %}
{% if current_description %}

BESTEHENDE BESCHREIBUNG:
{{ current_description }}
{% endif %}
Das SVG wird über <img> eingebettet (kein JavaScript, kein Sandbox-Kontext).
Halte dich DARUM STRIKT an diese Regeln:

SVG-Struktur:
- Liefere EINE einzige, komplette <svg>-Wurzel (kein HTML-Wrapper, kein XML-Erklärungsteil): <svg xmlns="http://www.w3.org/2000/svg" viewBox="…">…</svg>.
- viewBox ist PFLICHT; breite/höhe in px setzen (optional), Layout skalierungsfest über das Seitenverhältnis (preserveAspectRatio="xMidYMid meet" Default reicht).
- KEIN <script>, KEIN foreignObject, KEINE Event-Handler (on*-Attribute), KEINE externen Ressourcen: keine URLs (http/https/data:) in href/src, kein @import, keine url(http…) in Styles.
- <style>-Blöcke sind erlaubt (CSS im SVG), aber nur mit eingebauten Regeln — keine externen Refs, keine url() auf Dateien.

Animation (auf Wunsch, ansonsten statisch):
- Nur SMIL (<animate>, <animateTransform>, <animateMotion>) ODER CSS @keyframes im <style>-Block — beides läuft in <img>.
- SMIL: begin="0s", Dauer/Durchläufe sinnvoll (z. B. repeatCount="indefinite" nur bei Dauerschleifen), begin auf Maus-Ereignisse (begin="click") NICHT nutzen — Interaktion ist in <img> nicht möglich.
- CSS @keyframes: animation im <style> auf Klassen/IDs beziehen, keine JS-gesteuerten Klassen-Umschaltungen möglich.

Mathematische Formeln:
- Als <text>/<tspan> in sauber gesetzter mathematischer Schreibweise (Unicode-Symbole: ∑ ∫ √ π ≈ ± θ Δ λ …; Exponenten/Subscripts per <tspan baseline-shift="super|sub"> oder kleinerer Font).
- Keine LaTeX-Engine im reinen SVG — Formeln müssen als Text/Symbole gut lesbar sein; komplexe Brüche/Summen aus Elementen (Rechtecke, Linien, <text>) aufbauen.

Design:
- Lehrbuch-Qualität: präzise Geometrie, klare Beschriftungen, saubere Achsen/Legenden bei Diagrammen, konsistente Farbgebung (max. 4–5 Farben), gut lesbare Typografie (font-family: system-ui, sans-serif; Achsen 12–14 px).
- PLATZSPAREND und responsiv: kein fester weißer Kartenrahmen mit großem Padding; der Inhalt füllt die viewBox. Wichtige Elemente bei Bedarf mit eigenem Hintergrund, falls die Einbettung auf hellen/dunklen Hintergründen steht.
- Achsen/Ticks/Gridlines dezent (graue, dünne Linien); Datenpunkte/Flächen in kräftigen, kontrastreichen Farben.
- Gruppierung mit <g> + IDs/Klassen für sinnvolle Bausteine (erleichtert spätere Änderungen per media_edits).

Antwort:
Antworte NUR mit einem gültigen JSON-Objekt (keine Code-Blöcke, kein zusätzlicher Text). Erlaubt sind NUR die in „ZULÄSSIGE FELDER“ aufgeführten Schlüssel — jeder andere Schlüssel wird vom System IGNORIERT:
{% if '"title"' in generate_list %}- "title": Kurzer, aussagekräftiger Titel für die Grafik (max. 8 Wörter).
{% endif %}{% if '"description"' in generate_list %}- "description": Kurze Beschreibung (2-4 Sätze), was die Grafik zeigt und für welchen Lehrinhalt sie passt.
{% endif %}{% if '"media"' in generate_list %}- "media" ODER "media_edits" (genau eines davon):
  - "media": Die komplette SVG-Datei als String (nur die <svg>-Wurzel, ohne HTML-Wrapper). Verwende diesen Schlüssel für NEUE Grafiken und bei größeren Überarbeitungen.
  - "media_edits": Nur bei BESTEHENDEN Grafiken und KLEINEN, lokalen Änderungen — eine Liste von Edit-Objekten der Form {"op": "replace_span", "old": "<exakter Text, der EXAKT EINMAL im bestehenden SVG vorkommt>", "new": "<Ersetzung>"}. Nimm für "old" möglichst kurze, eindeutige Snippets (einzelne Zeile oder ein kurzes Fragment, niemals das ganze Dokument).
Achte in "media" bzw. den Edit-Strings auf korrektes JSON-Escaping (insbesondere Anführungszeichen und Newlines).
{% endif %}"""
