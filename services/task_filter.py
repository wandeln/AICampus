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


def parse_type_filter(raw: str) -> list[str]:
    """Aufgaben-Typ-Filter parsen: Comma-separierte Liste (OR) —
    'workspace' → ['workspace'], 'text,mc' → ['text', 'mc'], '' → [].
    """
    return [t.strip() for t in raw.split(",") if t.strip()]


def task_matches_challenge(task, filter_text: str, type_filter: str) -> bool:
    """Matcht `task` den Challenge-Filter? (AND: Titelsuche + Typen,
    gleiche Semantik wie die Aufgabenlisten-Filter.)"""
    if not multi_keyword_matches(task.title, filter_text):
        return False
    type_values = parse_type_filter(type_filter)
    return not type_values or task.task_type.value in type_values
