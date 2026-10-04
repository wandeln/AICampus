"""
Prompt-Template für die LLM-gestützte Aufgabengenerierung von
MULTIPLE-CHOICE-Aufgaben (task_type=mc).

Tutor gibt Thema + Schwierigkeit + max. Punkte + zu generierende Felder
(Tick-Boxen) ein → LLM generiert Titel, Einleitung, Musterlösung und
v. a. die Fragen (`mc_questions`) inkl. Optionen und Feedback je Option.

Das LLM setzt KEIN penalty_enabled und KEIN shuffle_options (das sind
manuelle Tutor-Einstellungen) und KEIN task.max_points (das wird
serverseitig aus der Punkte-Summe der Fragen berechnet).
"""

MC_TASK_PROMPT_TEMPLATE = """\
Du bist ein erfahrener Tutor. Du sollst eine Multiple-Choice-Übungsaufgabe
erstellen bzw. bestehende Felder einer solchen Aufgabe ändern/verbessern.

AUFGABENTYP: Multiple-Choice-Aufgabe (mehrere Fragen, Single- oder Multiple-Select)
THEMA: {{ topic }}
SCHWIERIGKEIT: {{ difficulty }}
PUNKTEBUDGET: Verteile die Punkte so über die Fragen, dass insgesamt
ca. {{ max_points }} Punkte zusammenkommen (ganze Zahlen je Frage).

ZULÄSSIGE FELDER — die Liste bestimmt, WELCHE Felder du in deiner Antwort
bearbeiten darfst (nicht: welche du zwingend bearbeiten MUSST):
{{ generate_list }}
Liefere als Antwort ein gültiges JSON-Objekt, dessen Schlüssel ausschließlich
aus dieser Liste stammen. Ein Schlüssel, der NICHT auf der Liste steht, wird
vom System IGNORIERT — produziere keine solchen Schlüssel. Ein Feld, das du
weglässt, bleibt unverändert (bestehender Inhalt wird behalten).

Mögliche Schlüssel und deren Bedeutung (NICHT nutzen: penalty_enabled,
shuffle_options — das sind manuelle Tutor-Einstellungen):
- "title": Kurzer, prägnanter Titel (z.B. „Blatt3-04: Grenzwerte")
- "description": Einleitung/Aufgabeninstruktion für Studierende (Kontext,
  Hinweise — z. B. „Bei einigen Fragen können mehrere Antworten richtig sein."
  Falls es Fragen mit multi_select=true gibt, HINWEISEN, dass dort genau alle
  richtigen Antworten ausgewählt werden müssen, sonst die Frage falsch ist.)
- "model_solution": Kurze Erläuterung der richtigen Antworten (eine Zeile je
  Frage) inkl. Bewertungskriterien: volle Punkte nur bei exakt richtigen
  Auswahlmengen, sonst keine Punkte der Frage.
- "mc_questions": Array der Fragen (siehe Format unten).

Format von "mc_questions":
[
  {
    "question": "Fragetext (Markdown und $Math$ erlaubt)",
    "points": 5,
    "multi_select": false,
    "options": [
      {"text": "...", "correct": true,  "feedback": "..."},
      {"text": "...", "correct": false, "feedback": "..."}
    ]
  },
  {
    "question": "Fragetext (Markdown und $Math$ erlaubt)",
    "points": 3,
    "multi_select": true,
    "options": [
      {"text": "...", "correct": true,  "feedback": "...", "feedback_wrong": "..."},
      {"text": "...", "correct": false, "feedback": "...", "feedback_wrong": "..."}
    ]
  }
]

Keine zusätzlichen Texte, keine Code-Blöcke (```json ... ```).
Achte dabei auf korrektes Escaping von special Characters. In Latex-Umgebungen
muss insbesondere der Backslash escaped werden (z.B. $\\text{...}$ oder
$$A \\rightarrow B$$). Dollar-Zeichen außerhalb von Code-Blöcken, die kein
Latex triggern sollen können mit Backslash \\$ escaped werden.

{% if script_chapters %}

SKRIPT-KAPITEL DES KURSES (mit ihren internen Zusammenfassungen):
{% for ch in script_chapters %}
- {{ ch.title }}{% if ch.summary %} — {{ ch.summary }}{% endif %}
{% endfor %}
Halte die Notation, Schreibweisen und Begriffswahl konsistent mit dem Skript
(z.B. gleiche Symbole für gleiche Größen), wo dies sinnvoll ist.
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
Zitiere mit @cite:key (im Fließtext: @citet:key bzw. @citep:key) — KEINE
geschweiften Klammern um den Key — nur wenn eine Aussage tatsächlich auf eine
der gelisteten Quellen zurückgeht. NUR tatsächlich gelistete Keys verwenden,
KEINE erfinden.
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
{% if current_questions %}
BESTEHENDE FRAGEN (JSON — als Kontext/Änderungsgrundlage):
{{ current_questions }}
{% endif %}

Regeln:
- Liefere NUR Schlüssel aus der obigen Liste „ZULÄSSIGE FELDER“ — die Ausgabe zu jedem anderen Feld wird vom System ignoriert und ist zu vermeiden.
- Falls für ein angefordertes Feld bereits ein Inhalt existiert (s. o.), überarbeite/verbessere ihn — halte am Thema fest und gestalte die Aufgabe nicht grundlos neu.
- Falls kein Inhalt existiert, erstelle das Feld neu passend zum Thema.
- Die Felder müssen zueinander passen: Beschreibung, Musterlösung und Fragen müssen inhaltlich konsistent sein.
- Anzahl Fragen: {{ question_count_hint }} (je nach Schwierigkeit), jede mit 3–5 Optionen.
- points: positive ganze Zahlen, Summe ≈ {{ max_points }}.
- multi_select=false (Single-Select/Radio): GENAU EINE richtige Option.
  multi_select=true (Multiple-Select/Checkbox): 2–3 richtige Optionen.
  Setze multi_select=true, wenn die Frage das didaktisch gut erlaubt
  (z. B. „Welche der folgenden Aussagen sind richtig?“); sonst false.
  Es sollte mindestens eine Frage mit multi_select=true geben, wenn das Thema es zulässt.
- Falsche Optionen: PLAUSIBLE Distraktoren — typische Fehler, Missverständnisse
  oder Näherungen, die nicht auf den ersten Blick als falsch erkennbar sind.
  Nicht trivial Falsches (z. B. offensichtliche Rechenfehler) verwenden.
- Feedback je Option ist OPTIONAL (leeres Feld → das System zeigt
  einfach „Richtig“/„Falsch“), soll aber dort gesetzt werden, wo es
  lehrreich ist — 1–2 Sätze, präzise:
  - multi_select=false: EIN Feld "feedback" je Option, das angezeigt
    wird, wenn der Student diese Option auswählt:
    * richtige Option: warum ist sie richtig (kurze Begründung/Herleitung).
    * falsche Option: warum ist sie falsch — welchen konkreten Irrtum
      sie widerspiegelt.
  - multi_select=true: ZWEI Felder je Option:
    * "feedback": erscheint, wenn die Option KORREKT behandelt wurde
      (richtige Option ausgewählt ODER falsche Option NICHT ausgewählt).
      Bei richtigen Optionen: warum sie richtig ist.
    * "feedback_wrong": erscheint, wenn die Option FALSCH behandelt wurde
      (falsche Option ausgewählt ODER richtige Option übersehen).
      Bei falschen Optionen: welchen Irrtum sie widerspiegelt; bei
      richtigen Optionen: warum sie wichtig/richtig ist.
  Das Feedback wird dem Studenten ANSCHLIEßEND zum Einreichen angezeigt.
- Aufgabenstellung: präzise formuliert, der Schwierigkeit entsprechend.
  Verwende Markdown-Formatierung (**fett**, *kursiv*, Listen, $Math$, $$Display-Math$$) und bei Bedarf Mermaid (```mermaid ... ```).
- Medien: Du DARFST Medien aus der obigen Medien-Liste in die Einleitung einbinden, wenn sie inhaltlich wirklich passen (max. 1-2) — verwende dafür exakt den angegebenen /media/-Pfad. Erfinde KEINE andere Medien-Pfade.
- Querverweise: Bezug auf Abbildungen/Gleichungen/Code/Boxen aus dem Skript per @fig:label / @eq:label / @code:label / @box:label / @tab:label — verwende NUR Labels, die in den obigen Kapitel-Zusammenfassungen vorkommen. Lege in der Aufgabe selbst KEINE neuen fig/eq/code/box/tab-Labels an (Kollisionsgefahr).
  WICHTIG: @fig:label / @eq:label / @code:label / @box:label / @tab:label sind KEIN Code — schreibe sie IMMER als normalen Fließtext, NIEMALS in Backticks, Code-Blöcke oder Anführungszeichen.
- Musterlösung ("model_solution"): knapp. Je Frage: richtige Option(en) + ein Satz Begründung.
  Referenziere die Optionen NUR per Text (Auszug in „Anführungszeichen") —
  KEINE Buchstaben (die Optionen werden in der Student-View gemischt und
  haben keine A/B/C-Labels). Danach die Bewertungskriterien (siehe oben: binär pro Frage, kein Teilpunkte).
"""
