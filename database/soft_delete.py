"""
Soft Delete: globaler Read-Filter für soft-deletable Entitäten.

„Löschen" setzt nur `deleted_at` (Soft Delete). Damit gelöschte Zeilen
überall unsichtbar bleiben, ohne an ~112 ORM-Query-Stellen manuell
`WHERE deleted_at IS NULL` pflegen zu müssen, ergänzt ein
`do_orm_execute`-Listener jeden ORM-SELECT automatisch um die Bedingung,
soweit die betroffene Entität vorkommt (als ausgewählte Entität, in
SELECT-FROM oder in JOINs).

Bypass (Restore, Admin-Papierkorb, Purge-Script):
    session.get(Model, id, execution_options=SOFT_DELETE_BYPASS)
    session.exec(select(Model).where(...).execution_options(**SOFT_DELETE_BYPASS))

Nur SELECTs werden betroffen — UPDATE/DELETE/INSERT bleiben unverändert.
Bewusste Einschränkung: Filter in Subqueries (IN (SELECT ...)) werden NICHT
gesetzt; in der Praxis greifen die Eltern-404s (z. B. require_course_access)
vor, bevor so eine Stelle relevant wird.
"""

from sqlalchemy import Select, Table, event

from sqlmodel import Session

from database.models import (
    Course,
    CourseMaterial,
    CourseMedia,
    ScriptSection,
    Task,
    User,
)

# Entitäten mit `deleted_at`-Spalte (Kern der Paperkorb-Funktionalität)
SOFT_DELETE_MODELS = (User, Course, Task, CourseMaterial, ScriptSection, CourseMedia)

_MODEL_BY_TABLE = {m.__table__: m for m in SOFT_DELETE_MODELS}

# Bypass-Flag für Restore / Admin-Papierkorb / CLI-Purge
SOFT_DELETE_BYPASS = {"skip_soft_delete_filter": True}


def _iter_tables(f):
    """Yieldt die Tabellen eines From-Objects (rekursiv über JOINs/Aliase)."""
    if isinstance(f, Table):
        yield f
        return
    # Join-Objecte: rekursiv über beide Seiten; Aliased: das Element dahinter
    for attr in ("left", "right", "element"):
        sub = getattr(f, attr, None)
        if sub is not None:
            yield from _iter_tables(sub)


def _from_objects_of(stmt: Select) -> list:
    """Alle From-Objects eines (SQLModel-)Selects, unabhängig davon, wie
    SQLAlchemy sie intern speichert (2.0.x: `_from_obj`, `_setup_joins`,
    `_raw_columns`; hier sind `_from_objects` oft leer, weil der FROM-Teil
    erst beim Compilieren aus den Spalten abgeleitet wird)."""
    objs = list(getattr(stmt, "_from_objects", ()) or ())
    objs += list(getattr(stmt, "_from_obj", ()) or ())
    for spec in getattr(stmt, "_setup_joins", ()) or ():
        # Spec ist ein Tupel (Ziel, ON-Clause, …) — alle Elemente absuchen
        objs.extend(spec if isinstance(spec, (tuple, list)) else (spec,))
    objs += list(getattr(stmt, "_raw_columns", ()) or ())
    return objs


def _models_in_statement(stmt: Select) -> set:
    """Soft-Delete-Entitäten, die in einem SELECT vorkommen."""
    models = set()
    for desc in stmt.column_descriptions:
        entity = desc.get("entity")
        if entity in SOFT_DELETE_MODELS:
            models.add(entity)
    for f in _from_objects_of(stmt):
        for table in _iter_tables(f):
            m = _MODEL_BY_TABLE.get(table)
            if m:
                models.add(m)
    return models


@event.listens_for(Session, "do_orm_execute")
def _filter_soft_deleted(state):
    stmt = state.statement
    if not isinstance(stmt, Select):
        return
    if state.execution_options.get("skip_soft_delete_filter"):
        return
    models = _models_in_statement(stmt)
    if not models:
        return
    for m in models:
        stmt = stmt.where(m.deleted_at.is_(None))
    state.statement = stmt
