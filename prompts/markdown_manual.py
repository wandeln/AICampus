"""
Einheitliches Markdown-Manual für alle LLM-Prompts (Skript, Folien, Import).

Die Werte werden als VARIABLEN in die Jinja-Templates injiziert
(``{{ markdown_manual }}`` / ``{{ edits_spec }}``) bzw. per String-Konkatenation
in die Import-Prompts eingebaut. Die geschweiften Klammern in den Werten
(``{#fig:label}``, ``{height=X}``, …) werden von Jinja NICHT parsed —
sie sind nur der Wert des Ausdrucks, keine Template-Syntax.

Aufbau:
- MARKDOWN_MANUAL: gemeinsamer Kern (Markdown, KaTeX, Medien+Subfigures+Captions,
  Labels, Querverweise, Boxen, Spalten, Code, Tabellen, Mermaid).
- SCRIPT_MARKDOWN_MANUAL: Kern + Skript-spezifische Regeln (sec-Labels,
  Kapitel-Label, Boxen-Disziplin).
- SLIDES_MARKDOWN_MANUAL: Kern + Folien-spezifische Regeln (Trennung,
  Direktiven, notes, Fragments, Auto-Animate, Code-Tokens, …).
- SCRIPT_CONTENT_EDITS_SPEC / SLIDES_CONTENT_EDITS_SPEC: Spezifikation der
  stellenweisen Edit-Objekte („content_edits“) für Skript bzw. Folien.
"""

MARKDOWN_MANUAL = """\
- Reines Markdown (Vorlesungsinhalt). Keine manuellen Nummern in Überschriften und keine manuellen Nummern bei nummerierten Objekten (Abbildungen/Gleichungen/Code/Tabellen/Boxen) — die Nummerierung wird AUTOMATISCH berechnet.
- Math: $...$ (inline) und $$...$$ (Display), KaTeX-Syntax.
  NUR STANDARD-KATEX: keine LaTeX-Pakete, keine \\newcommand/\\def oder sonstigen eigenen Makros —
  im Quelltext definierte Makros müssen an JEDEM Vorkommen durch ihre Definition ersetzt/ausexpandiert
  werden (z. B. \\newcommand{\\R}{\\mathbb{R}} → \\mathbb{R}). Nicht von KaTeX unterstützte Kommandos
  durch unterstützte Äquivalente umschreiben (häufig: \\hdots → \\cdots, \\bm{…} → \\boldsymbol{…}).
  Mehrzeilige Gleichungen (aligned/gathered): Ausrichtungsstelle = &= (NICHT &=& — das würde
  die rechte Seite rechtsbündig ausrichten).
- Zeilenumbruch im Fließtext: zwei Leerzeichen am Zeilenende ODER ein einzelner Backslash \\ — NIE doppelter Backslash \\\\ (rendert als sichtbarer Backslash; \\\\ bzw. \\newline der Quelle →
  einzelner Backslash oder zwei Leerzeichen); in $$…$$-Formeln bleibt \\\\ der Formel-Zeilenumbruch.
- Medien: ![Caption](/media/…) — der Alt-Text ist die Caption (wird unter dem Medium angezeigt).
  Größe steuern direkt nach dem Snippet: {height=X} = Max-Höhe in Pixeln (Suffix `px` optional) —
  Bilder nutzen die verfügbare Breite aus, bis die Max-Höhe erreicht ist (Aspektverhältnis bleibt erhalten);
  bei Applets gilt immer volle Breite, bei Überschreitung der Max-Höhe erscheint eine Scrollbar im Applet,
  und {zoom=X} setzt den Zoom-Faktor des Applet-Inhalts (z. B. {zoom=1.5} = 150 %, Default: 1.0).
  Interaktive Applets/Websites: ![Titel](/media/…/datei.html) bzw. ![Titel](https://…)
  (werden als Iframe gerendert und im Markdown genauso eingebunden wie Bilder).
- Verwandte Medien (z. B. Teilabbildungen) als SUBFIGUREN in EINE Abbildung fassen (ein gemeinsames
  „Abb. N: Gesamt-Caption“, Medien nebeneinander, je eigenes kleines Caption — „a)“/„b)“-Prefixe NICHT manuell in die
  Captions schreiben, das wird automatisch ergänzt; die Inners dürfen nur {height=X} und ein optionales
  {#fig:label} tragen — kein {#fragment}; sobald mindestens ein Inner ein {#fig:label} trägt, wird der Komplex
  als „Abb. N“ nummeriert (auch OHNE eigenes Komplex-{#fig:label}) und ALLE Inners automatisch mit
  a), b), c), … versehen; gelabelte Inners sind per @fig:label referenzierbar → „Abb. N a)“
  (a/b/… = Position im Komplex), das äußere {#fig:label} nach dem schließenden ) ist in dem Fall optional):
  ![Gesamt-Caption](![Caption 1](/media/…){height=300}{#fig:teil1} ![Caption 2](/media/…){height=300}){#fig:label}
- Captions (Abbildung/Code/Tabelle) dürfen inline-Math ($...$) enthalten; auch in Code-Blöcken
  (z. B. Pseudo-Code) werden $...$-Paare als Formel gerendert. Abbildungs-Captions (Alt-Text) dürfen
  zusätzlich Zitationen @cite:key / @citet:key / @citep:key enthalten (werden wie im Fließtext gerendert).
- Labels (snake_case, klein, eindeutig im gesamten Kurs):
  - Abbildung: ![Caption](/media/…){#fig:label}  →  wird als „Abb. N: Caption“ gerendert.
  - Display-Math: $$…$$ {#eq:label}  →  wird als „(N)“ neben der Formel gerendert — die Nummer wird AUTOMATISCH
    erzeugt, NIE manuell hinschreiben oder als Text nachstellen (z. B. \\text{(Gl. @eq:…)} = FALSCH);
    Bezüge im Fließtext: @eq:label als normaler Text.
  - Code-Block: Label auf der öffnenden Fence-Zeile: ```python {#code:label}[Caption]  →  wird als „Code N: Caption“ gerendert.
  - Box: Label UND/ODER [Caption] auf der @startbox:-Zeile direkt nach dem Typ (beliebige Reihenfolge), s. u.
  - Tabelle: {#tab:label}[Caption] als Zeile direkt unter der Pipe-Tabelle  →  wird als „Tab. N“ gerendert
    (Caption optional; optional am Ende {zoom=X} = Schriftgröße ×X, z. B. {zoom=0.8} für breite Tabellen).
- Querverweise als NORMALER FLOSSTEXT (niemals in Backticks, Code-Blöcke oder Anführungszeichen):
  @fig:label / @eq:label / @code:label / @box:label / @tab:label / @sec:label
  → werden durch klickbare Referenzen ersetzt („Abb. N“ / „Gl. N“ / „Code N“ / „Satz N“ / „Tab. N“ / „Abs. N.M“).
  Richtig: „wie in @eq:shannon gezeigt“ — Falsch: „wie in `@eq:shannon` gezeigt“.
  Auch IN Formeln ($...$ / $$...$$) sind @eq:label und @cite:key / @citet:key / @citep:key erlaubt
  (am besten innerhalb von \\text{…}) → klickbarer aufrechter Text („Gl. N“ / „[N]“ / „Autor (Jahr)“ /
  „(Autor, Jahr)“), z. B. \\text{siehe @eq:shannon} bzw. \\text{vgl. @cite:shannon1948}.
  Alle anderen Referenztypen (@fig:/@code:/@box:/@tab:/@sec:) werden in Formeln NICHT aufgelöst —
  die vermeide dort.
- Mathematische Boxen für Definitionen, Sätze/Theoreme, Lemmata, Propositionen, Korollare, Beweise und
  Beispiele — Marker JEWEILS auf EIGENER Zeile, Inhalt dazwischen (Markdown, $...$ und @-Referenzen erlaubt):
  @startbox:theorem[Name] {#box:label}
  ...Inhalt der Box...
  @endbox
  → wird als „Theorem N: Name“ gerendert (Typ als Kopf; [Caption] und {#box:label} optional, beliebige
  Reihenfolge auf der @startbox:-Zeile). Verfügbare Typen: theorem, definition, satz, lemma, proposition, korollar, beweis, beispiel.
  Boxen sind NESTBAR (beliebige Tiefe): ein @startbox:{typ} innerhalb einer Box öffnet eine innere Box,
  jeweils mit eigenem @endbox (z. B. Beweis-Box in Satz-Box) — ein @endbox schließt immer die
  zuletzt geöffnete (innere) Box, die äußere Box bekommt ihr eigenes @endbox.
  WICHTIG: @startbox:… STARTET eine Box und @endbox schließt sie; @box:label REFERENZIERT eine
  beschriftete Box im Fließtext („Satz N“) — die beiden Syntaxen NICHT verwechseln.
- Hinweis-Boxen für besondere Absätze (z. B. zentrale Merksätze, typische Fehler, Nebenbemerkungen, Fragen):
  @startbox:merksatz  …  @endbox  (Typen: merksatz, hinweis, bemerkung, warnung, frage)
  WICHTIG: Die Marker @startbox:… und @endbox sind KEIN Code — NIEMALS in Backticks oder Code-Blöcke setzen,
  sonst wird die Box NICHT gerendert.
- Spalten (mehrere Spalten nebeneinander, auch verschachtelt) — Marker JEWEILS auf eigener Zeile am Zeilenanfang:
  @startcolumn:2
  ...Inhalt der 1. Spalte...
  @nextcolumn:1
  ...Inhalt der 2. Spalte...
  @endcolumn
  → wird als Spaltenzeile gerendert (Grid). Gewicht = positive Zahl nach ":" (Default 1) = Breitenanteil
  (z. B. 2:1); jede "@nextcolumn[:gewicht]"-Zeile startet eine weitere Spalte. Zellen-Inhalt = normales
  Markdown (Überschriften, Bullets, Formeln, Medien, Boxen, Code, weitere Spaltenzeilen für Nesting).
  Inhalt VOR "@startcolumn" (z. B. eine Überschrift) bleibt vollbreit über der Spaltenzeile. 2–4 Spalten
  sinnvoll — für Gegenüberstellungen (z. B. zwei Ansätze, Vorher/Nachher) und gemischte Zeilen
  (z. B. oben Vollbreit, darunter 2 Spalten).
  WICHTIG: Pro Spaltenzeile gibt es genau EINEN "@endcolumn"-Marker, ganz am ENDE — die Spalten
  werden NICHT einzeln geschlossen (anders als Boxen!). "@nextcolumn[:gewicht]" trennt sie nur.
  FALSCH ist also:
  @startcolumn:2
  ...1. Spalte...
  @endcolumn        ← zu früh: reißt die Spaltenzeile nach der 1. Spalte ab
  @nextcolumn:1
  ...2. Spalte...
  @endcolumn
- Code-Blöcke: gefenceter Block mit Sprache auf der öffnenden Zeile (``` + Sprache, jede
  highlight.js-Sprache, z. B. python) → wird mit Syntax-Highlighting gerendert.
- Tabellen: Pipe-Tabellen; Label-ZEILE {#tab:label}[Caption] direkt unter der Tabelle (s. o.).
- Wenn Graphen zur Beschreibung benötigt werden: Verwende Mermaid (```mermaid … ```).
  Wichtig: Knotentexte mit Sonderzeichen (z. B. runde Klammern oder <, > in Formeln) MÜSSEN in doppelte
  Anführungszeichen gesetzt werden: z. B. C["H(X) = log2(n)"] (NICHT C[H(X) = log2(n)]).
  Mathematik in Knoten-/Kanten-Texten mit $$…$$ (KaTeX, wird nativ gerendert), z. B. A["$$x^2 - 2x + 1$$"].
  Pro Text NUR EIN $$…$$-Block — mehrere $$…$$-Blöcke in einem Text brechen das Diagramm. Mix aus
  Formel UND normalem Text: alles in EINEN $$…$$-Block, normaler Text mit \\text{…}, z. B.
  A["$$b\\text{: Koeffizienten von } b \\text{ in der Basis } \\{e_1,\\ldots,e_m\\}$$"].
  Label/Caption wie bei Code-Blöcken auf der öffnenden Zeile: ```mermaid {#code:label}[Caption]
  → wird als „Code N: Caption“ unter dem Diagramm gerendert (beschrifte wichtige Diagramme,
  auf die im Text Bezug genommen wird).
"""

SCRIPT_MARKDOWN_MANUAL = MARKDOWN_MANUAL + """\
- Beginne den Inhalt NICHT mit einer H1-Überschrift (der Kapiteltitel wird separat angezeigt); verwende ## für Abschnitte und ### für Unterabschnitte.
- LABELLE JEDER ÜBERSCHRIFT (##/###/####) mit einem {#sec:label} am Zeilenende — auch solche, auf die im Text kein Bezug genommen wird (z. B. ## Grundlagen {#sec:grundlagen} → „N.M Grundlagen“).
- Kapitel-Label: Ganz am Anfang des Kapitels steht als EIGENE ZEILE (die erste nicht-leere Zeile des Inhalts, VOR der ersten Überschrift) das {#sec:label} des Kapitels. Diese Zeile wird NICHT gerendert — sie dient nur als Label des Kapitels. Aus ANDEREN Kapiteln referenziert man das ganze Kapitel mit @sec:label (wird als „Kap. N“ gerendert).
- Beschrifte alle Objekte, auf die du im Text Bezug nimmst, UND wichtige Definitionen, Sätze und Formeln (wichtige Definitionen/Sätze als Boxen, z. B. @startbox:definition {#box:…} bzw. @startbox:satz {#box:…}) — auch ohne unmittelbare Bezugnahme im Text, damit sie in späteren Kapiteln und Übungsaufgaben referenziert werden können. Benutze bereits vorhandene Labels (s. o. „andere Kapitel“) nicht neu und erfinde keine Labels, die dort bereits vergeben sind.
- WICHTIGE GLEICHUNGEN IMMER mit {#eq:label} labeln (direkt nach der $$…$$-Zeile) — auch solche, auf die im Text kein Bezug genommen wird: zentrale Formeln (Hauptformeln, wichtige Definitionen/Identitäten, zentrale Gleichungen aus Sätzen) bekommen ein Label, damit sie in späteren Kapiteln und Übungsaufgaben referenziert werden können.
- Querverweise auf Abbildungen/Gleichungen/Code/Boxen/Tabellen/Sections/Kapitel in ANDEREN Kapiteln funktionieren genauso: Verwende dafür die dort gelisteten Labels (z. B. @fig:entropie, @eq:shannon, @code:sort, @box:pythagoras, @tab:wahrscheinlichkeiten, @sec:statistik).
- Setze Boxen KONSEQUENT ein: JEDER als Definition/Satz/Theorem/Lemma/Proposition/Korollar/Beweis/Beispiel abgesetzte Absatz wird zur Box (nicht zum normalen Fließtext); Hinweis-Boxen (merksatz, hinweis, bemerkung, warnung, frage) für zentrale Merksätze, typische Fehler, wichtige Nebenbemerkungen — nicht für normalen Fließtext.
"""

SLIDES_MARKDOWN_MANUAL = MARKDOWN_MANUAL + """\
- Folien werden durch eine eigene Zeile mit genau "---" getrennt (nichts anderes auf der Zeile).
- Optional: Eine Folie kann in vertikal gestapelte UNTERFOLIEN aufgeteilt werden, die in der Präsentation nacheinander (mit ↓) erscheinen — z. B. zum schrittweisen Aufbauen einer Erklärung: trenne sie mit einer eigenen Zeile mit genau "--" (nichts anderes auf der Zeile). Jede Unterfolie ist wie eine normale Folie (eigene Direktiven, "notes" etc.). Sparsam einsetzen (max. 1–2 Folien pro Deck mit je max. 3–4 Unterfolien); "--" ist NUR innerhalb einer Folie erlaubt und NIEMALS als Ersatz für "---".
- Am Anfang einer Folie (vor dem eigentlichen Inhalt) dürfen Direktiven stehen, JEWEILS auf eigener Zeile, jede Direktive maximal EINMAL pro Folie, nur diese Werte:
  layout: center | topleft | default
  transition: fade | slide | zoom | none | autoanimate
  class: <kennung>
  notes: <Sprechernotiz, einzeilig>
  background: ![Titel](/media/…/datei.png) oder ![Titel](/media/…/datei.html) oder ![Titel](https://…)
  (optional bei .html-Applets: {zoom=X} am Ende — Applet-Inhalt um Faktor X skalieren)
  ACHTUNG: Eine Direktive, die NICHT in den ersten Zeilen steht (z. B. nach dem Titel oder mitten im Text), wird NICHT erkannt und stattdessen als sichtbarer Folieninhalt gerendert — das ist ein Fehler.
- "notes" ist PFLICHT für jede Inhaltsfolie (1–3 Sätze in vollem Deutsch: was du als Dozent zu der Folie sagst — etwas mehr Tiefe/Kontext als auf der Folie selbst; die Notizen sind NICHT für die Studenten sichtbar). Titelfolie und Abschnittsfolien dürfen ohne "notes".
- ZUORDNUNG DER NOTES: Die "notes:"-Zeile gehört zur Folie, deren INHALT sie erklärt — sie steht ans ANFANG genau dieser Folie (direkt nach der "---"-Trennzeile, VOR dem "## Titel"). Schreibe NIEMALS die Notiz zur vorangegangenen Folie ans Anfang der nächsten Folie: Ist der Inhalt von Folie 3 fertig und du schreibst das "---" für Folie 4, dann muss die Notiz zu Folie 3 bereits am ANFANG von Folie 3 stehen — niemals hinter dem "---".
- "layout:" wird bei normalen Inhaltsfolien meist WEGLASSEN → Standard-Layout "default": Titel oben links, übriger Inhalt vertikal zentriert im Folienraum. "layout: topleft" nur für Folien mit VIELEm Text (Inhalt direkt unter dem Titel, ohne Zentrierung).
- "layout: center" NUR für die Titelfolie und kurze, zentrierte Folien (z. B. Abschnitts-Überschrift).
- "background" (optional) NUR verwenden, wenn die Anweisung explizit einen Folien-Hintergrund verlangt (z. B. animierten Applet-Hintergrund für die Titelfolie): Vollflächiges Bild, .html-Applet oder (wenn explizit verlangt) eine externe Website (https://…) hinter der Folie — Pfad exakt wie in der Medienliste; .html-Applets und externe Websites bleiben hinter der Folie interaktiv. Das Medium NICHT zusätzlich als Snippet in den Folientext einbinden. Hinweis: Applet-/Website-Hintergründe werden beim PDF-Export nicht mitgedruckt — wenn der Hintergrund auch im PDF sichtbar sein soll, ein Bild (.png/.jpg) verwenden.
- "transition" ist standardmäßig "autoanimate" (Reveal-Auto-Animate: der Inhalt animiert zwischen den Folien ineinander). Setze für eine einzelne Folie eine klassische Transition ("fade", "slide", "zoom" oder "none"), wenn Auto-Animate dort stört oder ein bestimmter Übergang gewünscht ist (z. B. "none" bei Abschnittsfolien). Hinweis: Ein gezoomtes Applet ({zoom=X}) wird vom Auto-Animate automatisch übersprungen (der Applet-Zoom bleibt erhalten, das Applet fadet einfach ein/aus) — das ändert nichts an der Übergangs-Wahl der Folie.
- Titelfolie mit "#", Titel der Inhaltsfolien als "##" (z. B. "## Rekursion — Baumschema").
- Kurze, prägnante Folien: max. 5–6 Bullets pro Folie, ein Bullet max. 1–2 Zeilen, eine zentrale Botschaft pro Folie.
  Zentrale Aussagen, Definitionen und Formeln als Display-Math ($$...$$) oder als hervorgehobenen Bullet — nicht als langen Fließtext.
  Lange Sätze ZUSAMMENFASSEN und in Bullets umformulieren — keine ganzen Absätze kopieren.
- Keine manuellen Foliennummern oder "Folie N"-Texte — die Nummerierung wird automatisch angezeigt.
- Medien: einbinden, wenn sie inhaltlich wirklich passen (sparsam: max. 1–2 pro Folie, max. 3–5 im ganzen Deck).
  Medien mit .html-Endung sind interaktive Applets — sie werden als interaktive Vorschau (Iframe) gerendert und im Markdown genauso eingebunden wie Bilder. Externe Websites (https://…) werden NUR eingebunden, wenn die Anweisung es explizit verlangt (dann als Iframe, ohne Label, im Markdown wie ein Bild: ![Titel](https://…)). Externe Websites & YouTube-Links (watch?v=…, youtu.be/…, shorts/…) werden automatisch eingebettet (16:9-Embed-Player); {height=…} fixiert die Iframe-Höhe (Breite proportional).
- Labels: Objekte, die AUCH IM SKRIPT vorkommen (s. o. „Labels“ der Kapitel): die dort bereits vergebenen Labels EXAKT wiederverwenden (gleiche Schreibweise, snake_case) — dadurch bekommt die Folie dieselbe Nummer wie das Skript und die Nummer wird als Link zum Skript gerendert. Erfinde für solche Objekte KEINE neuen Labels.
  Labels für neue, nur in diesem Slide-Deck vorkommende Objekte: frische, eindeutige snake_case-Labels, die mit KEINEM der gelisteten Skript-Labels kollidieren (diese bekommen die eigene Slide-Nummerierung (S1), (S2), …).
  Beschrifte nur Objekte, die du tatsächlich einbindest bzw. auf die du Bezug nimmst — nicht jede Formel braucht ein Label.
- Zentrale Sätze/Theoreme/Definitionen/Beispiele als Boxen (mit {#box:label}) setzen, wenn der Platz es zulässt — sparsam, nur die wirklich zentralen.
  JEDER Box eine [Caption] auf der @startbox:-Zeile (Name aus der Quelle, sonst kurz deskriptiv), z. B.
  @startbox:definition[Ableitung] {#box:ableitung}  →  Kopf „Definition N: Ableitung“.
- Schrittweises Einblenden (sparsam einsetzen, max. ~3 Marker pro Folie), jeweils am Zeilenende (nach Bullet, Absatz oder $$…$$-Formel):
  - {#fragment} → das Element erscheint bei der Präsentation erst mit einem extra Klick (z. B. schrittweises Aufbauen einer Argumentation).
  - {#fragment:id} (z. B. {#fragment:schritt-1}) → alle Elemente mit derselben ID erscheinen GLEICHZEITIG (z. B. zwei Bullets oder ein Text mit einer Formel auf einen Klick).
  - {#Fragment} (Großbuchstabe, OHNE ID) → dieses Element UND alle folgenden Inhalte der Folie erscheinen erst mit einem einzigen Klick („Und jetzt der Rest“-Effekt). Achtung: eigene Fragments nach einem solchen Gate werden von ihm mit-eingeblendet.
  - Formelteile Schritt für Schritt (sparsam, max. 2–3 Teile pro Formel): \\fragment{term} direkt in der Formel ($$…$$ oder $…$) → der eingewickelte Teil erscheint erst mit einem extra Klick (z. B. Formel Term-für-Term erklären oder einen Underbrace in einem späteren Schritt ergänzen). \\fragment{id}{term} → alle Teile mit derselben ID erscheinen gleichzeitig.
  - Auto-Animate-Element-ID (NUR wenn der Inhalt zwischen zwei aufeinanderfolgenden Folien ohne ID nicht sauber positionsgleich gepaart wird, z. B. weil ein Listenelement dazwischen eingefügt wurde): {#aaid:<label>} am Zeilenende (oder nach $$…$$/{#eq:…}/Bild-Snippet) — Elemente mit demselben Label auf den beiden Folien animieren per ID ineinander.
- Code-Blöcke: für ein kurzes hervorgehobenes Snippet im Fließtext: @startbox:code … @endbox (dunkle Code-Box mit „Code“-Kopf, fenced Block darin).
  Zeilennummern + schrittweises Zeilen-Einblenden (sparsam): {#lines:1,3-5} direkt nach der Sprache auf der öffnenden Zeile → diese Zeilen werden hervorgehoben, "|" = weiterer Schritt (z. B. ```python {#lines:1|2-3}).
  Tokens direkt nach der Sprache auf der öffnenden Zeile (Reihenfolge frei, auch ohne Leerzeichen): {#lines:…}, {#aaid:…} (Auto-Animate-ID), {.zoom=1.5} (größere Schrift), {.height=300} (Max-Höhe in px + internes Scrollen), {#code:label}[caption] (nummerierter Code-Block „Code N: caption“, Nummer klickbar, falls das Label auch im Skript vorkommt).
- Hervorheben: @startbox:highlight … @endbox → transparenter, geblurter Bereich in der Primärfarbe (z. B. für Titel auf Deckslides); @boxcolor:<farbe> als erste Zeile einer Folie → Boxfarbe übersteuern (Hex, rgb()/rgba(), CSS-Farbname).
"""

# ─── content_edits-Spezifikationen (stellenweise Edits) ───────────

SCRIPT_CONTENT_EDITS_SPEC = """\
Jedes Edit-Objekt enthält einen Schlüssel "op" mit genau einem dieser Werte:
- {"op": "replace_section", "heading": "### 3.2 Beispiel", "content": "..."}
  Ersetzt den Inhalt des Abschnitts (alles ab der Heading-Zeile bis zur nächsten Heading — Unterabschnitte darunter bleiben davon unberührt) durch den neuen "content".
  "heading" = die EXISTIERENDE Heading-Zeile des Abschnitts, WORTGLEICH (inkl. #-Zeichen, exakt wie im bestehenden Inhalt).
  "content" = kompletter NEUER Abschnittsinhalt OHNE die Heading-Zeile selbst (die bleibt erhalten).
- {"op": "insert_after", "heading": "## 3.1 Grundlagen", "content": "..."}
  Fügt den "content" direkt NACH dem angegebenen Abschnitt ein. Der content darf eigene Headings enthalten (z. B. ein neuer "###"-Abschnitt).
- {"op": "delete_section", "heading": "### Altes Beispiel"}
  Löscht den gesamten Abschnitt (Heading + Inhalt).
- {"op": "replace_span", "old": "...", "new": "..."}
  Ersetzt ein KURZES (max. 1-2 Zeilen), im bestehenden Inhalt EXAKT EINMAL vorkommendes Snippet WORTGLEICH durch "new". Nur für Änderungen innerhalb eines Absatzes, die keinen ganzen Abschnitt betreffen. "old" muss exakt so im bestehenden Inhalt vorkommen (inkl. aller Backslashes, Leerzeichen und Zeilenumbrüche).
Regeln für "content_edits":
- Verwende NUR Headings und Snippets, die im bestehenden Inhalt tatsächlich vorhanden sind — erfinde keine.
- Jedes "heading" bzw. "old" muss im Inhalt EXAKT EINMAL vorkommen (eindeutig); die Edits dürfen sich nicht überschneiden.
- Bewahre vorhandene fig/eq/code/box/tab-Labels und @fig:/@eq:/@code:/@box:/@tab:/@task:-Referenzen bei, sofern die Anweisung nichts anderes verlangt.
- Betrifft die Änderung einen Großteil des Kapitels, nutze STATTDESSEN "content" (Volltext).
"""

SLIDES_CONTENT_EDITS_SPEC = """\
Jedes Edit-Objekt enthält einen Schlüssel "op" mit genau einem dieser Werte:
- {"op": "replace_slide", "slide": 3, "content": "..."}
  Ersetzt Folie 3 (Nummer s. o. „%% Folie N %%“) durch eine neue Folie. "content" = kompletter NEUER Folieninhalt im Folien-Format (inkl. Direktiven wie "notes:", OHNE „%%“-Marker).
- {"op": "insert_slide_after", "slide": 3, "content": "..."}
  Fügt eine neue Folie direkt NACH Folie 3 ein. "slide": 0 = als ERSTE Folie des Decks.
- {"op": "delete_slide", "slide": 4}
  Löscht Folie 4.
- {"op": "replace_span", "old": "...", "new": "..."}
  Ersetzt ein KURZES (max. 1-2 Zeilen), in EINER (Unter-)Folie EXAKT EINMAL vorkommendes Snippet WORTGLEICH durch "new". Nur für Änderungen innerhalb einer (Unter-)Folie, die keine ganze Folie betreffen. "old" muss exakt so im bestehenden Inhalt vorkommen (inkl. aller Backslashes, Leerzeichen und Zeilenumbrüche) und darf KEINEN Trenner („---" oder "--") enthalten.
Regeln für "content_edits":
- Verwende NUR Foliennummern und Snippets, die im bestehenden Deck tatsächlich vorhanden sind — erfinde keine.
- Foliennummern betreffen die ganze Folie EINSCHLIESSLICH aller ihrer "--"-Unterfolien: replace_slide/delete_slide entfernen/ersetzen den kompletten Inhalt der Folie (alle Unterfolien); für eine Änderung an EINZIG EINER Unterfolie ohne Volltext-Austausch der Folie "replace_span" verwenden (das Snippet bleibt innerhalb der Unterfolie).
- Eine Folie wird höchstens EINMAL mit replace_slide oder delete_slide angefasst (diese beiden Ops nicht auf derselben Folie kombinieren); mehrere insert_slide_after bzw. replace_span sind erlaubt, solange jedes Snippet eindeutig bleibt.
- Bewahre vorhandene fig/eq/code/box-Labels und @-Referenzen bei, soweit die Anweisung nichts anderes vorschreibt.
- Übernimm die Marker „%% Folie N %%“ NIEMALS in „content“, „old“ oder „new“.
- Betrifft die Änderung einen Großteil des Decks, nutze STATTDESSEN "content" (Volltext).
"""
