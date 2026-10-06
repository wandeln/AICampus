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

    Ein Begriff matcht, wenn ein Wort von `text` damit beginnt
    (Wort-Präfix; Bindestrich gehört zum Wort). So matcht "ÜG" auch
    "ÜG1"/"ÜG-1", "Blatt rek" auch "Blatt 4: Rekursion" — aber "1"
    nicht "Jahrgang 2001".
    """
    terms = [t.casefold() for t in filter_text.split()]
    if not terms or not text:
        return False
    words = [w.casefold() for w in text.split()]
    return all(any(word.startswith(term) for word in words) for term in terms)
