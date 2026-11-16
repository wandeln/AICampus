"""
Gemeinsame Mehr-Schlagwort-Filter-Logik.

Wird für alle Filter über Leerzeichen-getrennte Begriffe genutzt —
Aufgaben-Titel (Aufgabenliste Student/Tutor, Punkteübersicht,
Live-Dashboard, Excel-Export, Reports) und Studenten-Annotations
(Punkteübersicht, Excel-Export, Reports). So bleibt die Semantik
in allen Ansichten identisch.
"""

def multi_keyword_matches(text: str | None, filter_text: str) -> bool:
    """True, wenn alle per Leerzeichen getrennten Begriffe des Filters in
    `text` vorkommen (AND-Verknüpfung, Groß-/Kleinschreibung egal).

    Ein Begriff matcht, wenn er als Teilzeichenfolge irgendwo in `text`
    vorkommt. So matcht "ÜG" auch "ÜG1"/"ÜG-1", "Blatt rek" auch
    "Blatt 4: Rekursion" — und "1" auch "Jahrgang 2001".
    Leerer Filter matcht alles.
    """
    terms = [t.casefold() for t in filter_text.split()]
    if not terms:
        return True
    if not text:
        return False
    text_cf = text.casefold()
    return all(term in text_cf for term in terms)
