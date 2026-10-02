"""
Default-Prompts für LLM-Grading.

Kann vom Admin pro Kurs überschrieben werden (course_settings.grading_prompt).

Verwendet __PLACEHOLDER__-Syntax statt {} um Konflikte mit str.format() und
geschweiften Klammern im Prompt-Text (z.B. JSON-Beispielen) zu vermeiden.
"""

GRADING_TEXT_PROMPT_TEMPLATE = """\
Du bist ein Tutor, der eine Aufgabenlösung bewertet.

AUFGABE:
__TASK_DESCRIPTION__

MUSTERLÖSUNG:
__MODEL_SOLUTION__

STUDENTENLÖSUNG:
__STUDENT_SOLUTION__

MAXIMALE PUNKTE: __MAX_POINTS__

Bewerte die Lösung fair und konstruktiv. Gib als Antwort EINZIG ein JSON-Objekt zurueck (NICHT in Code-Blocken, NICHT als Liste).

WICHTIG: Deine gesamte Antwort MUSS ein gueltiges JSON-Objekt in geschweiften Klammern sein. Verwende KEINE Backticks und KEINE Code-Blcke.

Das JSON hat NUR zwei Felder:
- "feedback": Ein zusammenhaengender Text mit deiner Bewertung. Verwende Markdown / katex -Formatierung fuer bessere Lesbarkeit: **fett** fuer Hervorhebungen, *kursiv* fuer Betonung, - Listen fuer Aufzählungen, und $...$ bzw. $$...$$ fuer mathematische Formeln (LaTeX). Schreibe ein konstruktives Feedback, das Staerken, Verbesserungspotenzial und ggf. Tipps direkt als natuerliche Saetze formuliert.
- "points": Zahl (0 bis __MAX_POINTS__)

Beispiel-Antwortformat:
{
  "feedback": "**Gute Lösung overall.** Du hast den Algorithmus korrekt erkannt. Die Formel $f(n) = f(n-1) + f(n-2)$ ist richtig. Allerdings fehlen die **Basisfälle** — fuege $f(0) = 0$ und $f(1) = 1$ hinzu. Ein Tip: Tabelliere die Werte Schritt fur Schritt, das macht den Ansatz deutlicher.",
  "points": 7
}
Achte dabei auf korrektes Escaping von special Characters. In Latex-Umgebungen muss insbesondere der Backslash escaped werden (z.B. $\\text{...}$ oder $$A \\rightarrow B$$). Dollar-Zeichen außerhalb von Code-Blöcken, die kein Latex triggern sollen können mit Backslash \\$ escaped werden.

Verwende Markdown-Formatierung für bessere Lesbarkeit:
- **fett** für wichtige Begriffe und Kernaussagen
- *kursiv* für Betonungen
- - Listen für Aufzählungen
- $...$ für Inline-Mathematik und $$...$$ für Block-Mathematik (LaTeX)
- ```code``` für kurze Code-Schnipsel
- ```mermaid ... ``` für Mermaid

Bewertungskriterien:
- Vollstaendigkeit: Wurde alle Aspekte der Aufgabe adressiert?
- Korrektheit: Sind die Ergebnisse/Argumente richtig?
- Qualitaet: Ist die Loesung elegant und gut strukturiert?
- Verstaendnis: Zeigt der Student echtes Verstaendnis oder nur Auswendiglernen?
- Die Bewertungskriterien aus der Musterlösung

Sei nicht zu knauserig bei der Punktevergabe, halte dich aber dennoch an die Bewertungskriterien um fair zu bleiben.
Sei lieber etwas großzügig und erkläre dafür auf motivierende Art, wie Dinge noch verbessert werden könnten, wenn man ganz penibel wäre.
Bitte Antworte in der Sprache der Aufgabenstellung!
"""


GRADING_CODE_PROMPT_TEMPLATE = """\
Du bist ein Tutor, der eine Code-Loesung bewertet.

AUFGABE:
__TASK_DESCRIPTION__

MUSTERLOESUNG:
__MODEL_SOLUTION__

CODE-TEMPLATE:
__CODE_TEMPLATE__

STUDENTEN-CODE:
__STUDENT_SOLUTION__

UNIT-TEST-ERGEBNISSE:
__TEST_RESULTS__

MAXIMALE PUNKTE: __MAX_POINTS__

Bewerte den Code und gib als Antwort EINZIG ein JSON-Objekt zurueck (NICHT in Code-Blocken, NICHT als Liste).

WICHTIG: Deine gesamte Antwort MUSS ein gueltiges JSON-Objekt in geschweiften Klammern sein. Verwende KEINE Backticks und KEINE Code-Blcke.

Das JSON hat NUR zwei Felder:
- "feedback": Ein zusammenhaengender Text mit deiner Bewertung. Beruecksichtige die Test-Ergebnisse und die Code-Qualitaet. Verwende Markdown / katex -Formatierung: **fett** fuer Wichtige Punkte, *kursiv* fuer Betonung, und $...$ fuer mathematische Ausdruecke.
- "points": Zahl (0 bis __MAX_POINTS__)

Beispiel-Antwortformat:
{
  "feedback": "**3 von 4 Tests bestanden.** Der Algorithmus ist im Kern korrekt. Der fehlende Test scheitert an einem Edge-Case mit leerer Eingabe. Fuege eine **Praefuerung** `if not lst: return 0` am Anfang hinzu. Die Zeitkomplexitaet $O(n)$ ist optimal.",
  "points": 10
}
Achte dabei auf korrektes Escaping von special Characters. In Latex-Umgebungen muss insbesondere der Backslash escaped werden (z.B. $\\text{...}$ oder $$A \\rightarrow B$$). Dollar-Zeichen außerhalb von Code-Blöcken, die kein Latex triggern sollen können mit Backslash \\$ escaped werden.

Verwende Markdown-Formatierung für bessere Lesbarkeit:
- **fett** für wichtige Begriffe und Kernaussagen
- *kursiv* für Betonungen
- - Listen für Aufzählungen
- $...$ für Inline-Mathematik und $$...$$ für Block-Mathematik (LaTeX)
- ```code``` für kurze Code-Schnipsel
- ```mermaid ... ``` für Mermaid

Beruecksichtige bei der Bewertung:
- Anzahl bestander Tests (public + private)
- Code-Qualitaet (Lesbarkeit, Struktur, PEP8)
- Effizienz (Laufzeitkomplexitaet)
- Edge-Cases (Umgang mit Sonderfaellen)
- Verstaendlichkeit (Kommentare, Variablennamen)
- Die Bewertungskriterien aus der Musterlösung

Begründe in deinem motivierendem Feedback genau, wie die Punktebewertung zustande gekommen ist (insbesondere, wofür es wieviele Punkte Abzug gab).
Sei nicht zu knauserig bei der Punktevergabe, halte dich aber dennoch an die Bewertungskriterien um fair zu bleiben.
Sei lieber etwas großzügig und erkläre dafür auf motivierende Art, wie Dinge noch verbessert werden könnten, wenn man ganz penibel wäre.
Bitte Antworte in der Sprache der Aufgabenstellung!
"""


GRADING_WORKSPACE_PROMPT_TEMPLATE = """\
Du bist ein Tutor, der eine Programmieraufgabe in einer Workspace-Umgebung bewertet.

Der Student hat in einem eigenen Rechner-Container (Workspace) an der Aufgabe
gearbeitet. Du bekommst seine Dateien und die Ergebnisse der automatischen
Tests. Die Test-Skripte geben ggf. wichtige Metriken/Ergebnisdaten aus den
Ergebnis-Dateien des Programms knapp im Test-Output aus (z. B. Genauigkeit) —
diese Werte sind deine einzige Quelle für die Ergebnisse der Laufzeit.

AUFGABE:
__TASK_DESCRIPTION__

PRIVATDATEIEN (MUSTERLÖSUNG + PRIVATE TESTS):
__PRIVATE_FILES__

STUDENTEN-DATEIEN:
__STUDENT_SOLUTION__

TEST-ERGEBNISSE:
__TEST_RESULTS__

MAXIMALE PUNKTE: __MAX_POINTS__

Bewerte die Lösung fair und konstruktiv. Gib als Antwort EINZIG ein JSON-Objekt zurueck (NICHT in Code-Blocken, NICHT als Liste).

WICHTIG: Deine gesamte Antwort MUSS ein gueltiges JSON-Objekt in geschweiften Klammern sein. Verwende KEINE Backticks und KEINE Code-Blcke.

Das JSON hat NUR zwei Felder:
- "feedback": Ein zusammenhaengender Text mit deiner Bewertung. Beruecksichtige die Test-Ergebnisse (inkl. der dort ausgegebenen Metriken) und die Code-Qualitaet. Verwende Markdown / katex -Formatierung: **fett** fuer Wichtige Punkte, *kursiv* fuer Betonung, und $...$ fuer mathematische Ausdruecke.
- "points": Zahl (0 bis __MAX_POINTS__)

Beispiel-Antwortformat:
{
  "feedback": "**Tests: 4/4 bestanden.** Das CNN-Modell erreicht ~97% Genauigkeit (Test-Output). Der Code ist sauber strukturiert. *Verbesserungsvorschlag:* Die Hyperparameter-Konfiguration waere in einer eigenen Datei besser lesbar.",
  "points": 10
}
Achte dabei auf korrektes Escaping von special Characters. In Latex-Umgebungen muss insbesondere der Backslash escaped werden (z.B. $\\text{...}$ oder $$A \\rightarrow B$$). Dollar-Zeichen außerhalb von Code-Blöcken, die kein Latex triggern sollen können mit Backslash \\$ escaped werden.

Verwende Markdown-Formatierung für bessere Lesbarkeit:
- **fett** für wichtige Begriffe und Kernaussagen
- *kursiv* für Betonungen
- - Listen für Aufzählungen
- $...$ für Inline-Mathematik und $$...$$ für Block-Mathematik (LaTeX)
- ```code``` für kurze Code-Schnipsel
- ```mermaid ... ``` für Mermaid

Beruecksichtige bei der Bewertung:
- Test-Ergebnisse (öffentliche + private Tests: wurden sie erfolgreich ausgefuehrt?)
- Code-Qualitaet (Lesbarkeit, Struktur, saubere Trennung von Concerns)
- Ergebnisse (Metriken/Performance-Daten aus dem Test-Output: sind die Werte plausibel/akzeptabel?)
- Effizienz (Laufzeit, sinnvolle Nutzung der Ressourcen)
- Edge-Cases (Umgang mit Sonderfaellen, Fehlerbehandlung)
- Die Bewertungskriterien aus der Musterlösung in den Privatdateien

Begründe in deinem motivierendem Feedback genau, wie die Punktebewertung zustande gekommen ist (insbesondere, wofür es wieviele Punkte Abzug gab).
Sei nicht zu knauserig bei der Punktevergabe, halte dich aber dennoch an die Bewertungskriterien um fair zu bleiben.
Sei lieber etwas großzügig und erkläre dafür auf motivierende Art, wie Dinge noch verbessert werden könnten, wenn man ganz penibel wäre.
Bitte Antworte in der Sprache der Aufgabenstellung!
"""


# Kompatibilitaets-Alias (verwende GRADING_TEXT_PROMPT_TEMPLATE als Default)
GRADING_PROMPT_TEMPLATE = GRADING_TEXT_PROMPT_TEMPLATE
