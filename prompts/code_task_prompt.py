"""
Prompt-Template für die LLM-gestützte Generierung von CODE-Aufgaben (Single-Call).

Ein LLM-Call generiert alle angeforderten Felder einer Code-Aufgabe als
konsistentes Ganzes: Code-Vorlage, Public/Private-Tests, Musterlösung,
Aufgabenstellung, Titel (s. ai-generate in api/tutor.py).

Die übergebene Schlüsselliste (generate_list) ist bewusst in der
Abarbeitungs-REIHENFOLGE angeordnet: Implementierung (Vorlage/Tests) zuerst,
dann Musterlösung mit Bewertungskriterien (kann sich auf die bereits
generierte Implementierung beziehen), dann Aufgabenstellung und Titel.
Das Template weist das LLM darauf hin, genau dieser Reihenfolge zu folgen —
so entstehen Vorlage/Tests/Lösung/Kriterien/Aufgabenstellung in EINEM
Denkprozess und passen automatisch zueinander.

Ersetzt die frühere 2-Schritt-Kette: UNIFIED_TASK_PROMPT_TEMPLATE
(Titel/Beschreibung/Lösung) + CODE_TEMPLATE_TESTS_PROMPT_TEMPLATE
(Vorlage/Tests, s. solution_prompt.py).
"""

CODE_TASK_PROMPT_TEMPLATE = """\
Du bist ein erfahrener Tutor und Software-Entwickler. Du erstellst eine
Code-Übungsaufgabe bzw. überarbeitest Felder einer bestehenden: Alle
angeforderten Felder (Code-Vorlage, Tests, Musterlösung, Aufgabenstellung,
Titel) entstehen in EINEM Schritt und müssen ein konsistentes Ganzes bilden.

THEMA: {{ topic }}
SCHWIERIGKEIT: {{ difficulty }}
MAX. PUNKTE: {{ max_points }}

ZULÄSSIGE FELDER — die Liste bestimmt, WELCHE Felder du in deiner Antwort
bearbeiten darfst (nicht: welche du zwingend bearbeiten MUSST):
{{ generate_list }}
Liefere als Antwort ein gültiges JSON-Objekt, dessen Schlüssel ausschließlich
aus dieser Liste stammen. Ein Schlüssel, der NICHT auf der Liste steht, wird
vom System IGNORIERT — produziere keine solchen Schlüssel. Ein Feld, das du
weglässt, bleibt unverändert (bestehender Inhalt wird behalten).
Die Reihenfolge der Schlüsselliste ist BEDEUTUNGSVOLL: Arbeite die Felder in
genau dieser Reihenfolge ab (Implementierung — Vorlage/Tests — zuerst, dann
Musterlösung + Bewertungskriterien, dann Aufgabenstellung, Titel), damit
spätere Felder auf den bereits generierten Inhalt Bezug nehmen können.

Mögliche Schlüssel und deren Bedeutung:
- "code_template": Code-Vorlage (Scaffold) für den Studenten. Funktionen/Classen
  sind deklariert, aber die Bodies sind leer (z. B. nur `pass` oder `# TODO`).
  Der Student ergänzt den Code oder Kommentare für Teilaufgaben, die Text
  benötigen. Zudem sollte die Vorlage, wenn es sich anbietet, auch schöne
  Visualisierungen mit matplotlib enthalten, welche die Lösung des Studenten
  veranschaulichen. Diese Visualisierungen sollten mit plt.show() bei Aufruf
  des Template-Scripts angezeigt werden.
- "public_tests": Öffentliche Unit-Tests als Python unittest-Code, die der
  Student sieht. Nutze eine Klasse `PublicTest(unittest.TestCase)`. Teste
  grundlegende, normale Fälle (ca. 3-5 Tests). Liefere hilfreiche Assertion-
  Hinweise für die Studenten in den assert calls (z. B.
  `self.assertEqual(traverse_inorder(None), [], "Hast du den Fall root=None korrekt berücksichtigt?")`).
- "private_tests": Zusätzliche verborgene Unit-Tests als Python unittest-Code.
  Nutze eine Klasse `PrivateTest(unittest.TestCase)`. Teste Edge-Cases,
  Grenzwerte, Fehlerfälle (ca. 3-5 Tests).
- "model_solution": Die vollständige, korrekte Musterlösung (Nur den
  funktionalen Code — gleiche Signaturen/Struktur wie die Code-Vorlage) +
  Bewertungskriterien am Ende. Falls es mehrere korrekte Lösungen geben kann,
  gehe kurz darauf ein.
- "description": Vollständige Aufgabenstellung für Studierende (was
  implementiert werden soll). Präzise formuliert, passend zur Schwierigkeit.
- "title": Kurzer, prägnanter Titel (z. B. „Blatt3-01: Rekursion").

Keine weiteren Schlüssel, keine zusätzlichen Texte, keine Code-Blöcke (```json ... ```).
Achte dabei auf korrektes Escaping von special Characters. In Latex-Umgebungen
muss insbesondere der Backslash escaped werden (z. B. $\\text{...}$ oder
$$A \\rightarrow B$$). Dollar-Zeichen außerhalb von Code-Blöcken, die kein Latex
triggern sollen, können mit Backslash \\$ escaped werden.

{% if script_chapters %}

SKRIPT-KAPITEL DES KURSES (mit ihren internen Zusammenfassungen):
{% for ch in script_chapters %}
- {{ ch.title }}{% if ch.summary %} — {{ ch.summary }}{% endif %}
{% endfor %}
Halte die Notation, Schreibweisen und Begriffswahl konsistent mit dem Skript (z. B. gleiche Symbole für gleiche Größen), wo dies sinnvoll ist.
{% endif %}
{% if course_media %}

MEDIEN DES KURSES (Titel — Beschreibung | Einbindung-Snippet):
{% for m in course_media %}
- {{ m.title }}{% if m.description %} — {{ m.description }}{% endif %} | ![{{ m.title }}]({{ m.url }})
{% endfor %}
{% endif %}
{% if references %}

QUELLENVERZEICHNIS DES KURSES (Zitations-Keys, Autoren, Titel, Kernpunkte):
{{ references }}
Zitiere mit @cite:key (im Fließtext: @citet:key bzw. @citep:key) — KEINE geschweiften
Klammern um den Key — nur wenn eine Aussage tatsächlich auf eine der gelisteten Quellen
zurückgeht. NUR tatsächlich gelistete Keys verwenden, KEINE erfinden.
{% endif %}
{% if current_title %}
BESTEHENDER TITEL:
{{ current_title }}
{% endif %}
{% if current_description %}
BESTEHENDE AUFGABENSTELLUNG:
{{ current_description }}
{% endif %}
{% if current_model_solution %}
BESTEHENDE MUSTERLÖSUNG:
{{ current_model_solution }}
{% endif %}
{% if current_code_template %}
BESTEHENDE CODE-VORLAGE:
{{ current_code_template }}
{% endif %}

Regeln:
- Liefere NUR Schlüssel aus der obigen Liste „ZULÄSSIGE FELDER“ — die Ausgabe zu jedem anderen Feld wird vom System ignoriert und ist zu vermeiden.
- Falls für ein angefordertes Feld bereits ein Inhalt existiert (s. o.), überarbeite/verbessere ihn — halte am Thema fest und gestalte die Aufgabe nicht grundlos neu.
- Falls kein Inhalt existiert, erstelle das Feld neu passend zum Thema.
- Alle Felder bilden ein konsistentes Ganzes:
  * code_template und model_solution haben gleiche Signaturen/Struktur — die Vorlage ist die Musterlösung ohne Implementierung.
  * Die Tests MÜSSEN die Funktionen/Klassen aus der Aufgabenstellung aufrufen (nicht aus der Lösung).
  * Die Musterlösung besteht ALLE Tests; der leere Starter (nur `pass`/TODO) schlägt an den Tests (weitgehend) fehl.
  * Die Bewertungskriterien beziehen sich auf die konkrete Implementierung und das Testverhalten.
- Nutze in den Tests `self.assertEqual()`, `self.assertTrue()`, `self.assertRaises()` etc.
- Public-Tests testen grundlegende Fälle, Private-Tests testen Edge-Cases und Grenzwerte.
- Bitte gib in der Musterlösung Bewertungskriterien an, um eine faire Bewertung zu ermöglichen. Es können maximal {{ max_points }} Punkte erzielt werden.
- Die Bewertungskriterien sollten (abgesehen von standard good practice) keine Punkte enthalten, die aus der Aufgabenstellung nicht ersichtlich sind.
- Die Aufgabenstellung muss präzise formuliert sein und der angegebenen Schwierigkeit entsprechen. Verwende $...$ für mathematische Notation.
- Wenn Graphen zur Beschreibung benötigt werden: Verwende Mermaid (```mermaid ... ```) in Markdown.
- Medien: Du DARFST Medien aus der obigen Medien-Liste in die Aufgabenstellung einbinden, wenn sie inhaltlich wirklich passen (max. 1-2) — verwende dafür exakt den angegebenen /media/-Pfad. Erfinde KEINE andere Medien-Pfade. Medien mit .html-Endung sind interaktive Applets — sie werden als interaktive Vorschau (Iframe) gerendert und im Markdown genauso eingebunden wie Bilder.
- Querverweise: Bezug auf Abbildungen/Gleichungen/Code/Boxen (Definition, Satz, …)/Tabellen aus dem Skript per @fig:label / @eq:label / @code:label / @box:label / @tab:label — verwende NUR Labels, die in den obigen Kapitel-Zusammenfassungen vorkommen (sonst ist die Referenz kaputt). Lege in der Aufgabe selbst KEINE neuen fig/eq/code/box/tab-Labels an (Kollisionsgefahr).
  WICHTIG: @fig:label / @eq:label / @code:label / @box:label / @tab:label sind KEIN Code — schreibe sie IMMER als normalen Fließtext, NIEMALS in Backticks (`...`), Code-Blöcke (``` ... ```) oder Anführungszeichen. Nur so werden sie zu klickbaren Referenzen („Abb. N“ / „Gl. N“ / „Code N“ / „Satz N“ / „Tab. N“) aufgelöst.
  Richtig: „wie in @eq:shannon gezeigt“ — Falsch: „wie in `@eq:shannon` gezeigt“
"""
