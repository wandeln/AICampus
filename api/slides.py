"""
Folien-API: Slide-Theme (Design) pro Kurs + LLM-Deck-Generierung.

- GET /api/courses/{cid}/slides-theme: aufgelöstes Theme + Logo-URL (alle Mitglieder)
- PUT /api/courses/{cid}/slides-theme: Theme setzen (PROF/TUTOR)
- POST /api/courses/{cid}/slides/ai-generate: LLM generiert/ändert ein Slide-Deck (PROF/TUTOR)

Das Theme ist ein JSON-Objekt (validiert: services.slides_service.validate_theme);
die Auflösung auf Template-Defaults übernimmt resolve_theme().
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from api.script import (
    _chapter_label,
    _get_membership,
    _scan_box_labels,
    _scan_code_labels,
    _scan_figures,
    _scan_headings,
    _scan_labels,
    _scan_previews,
    _scan_tables,
)
from database.base import get_session
from database.models import (
    Course,
    CourseMaterial,
    CourseMedia,
    CourseRole,
    CourseSlidesTheme,
    GlobalUserRole,
    MaterialType,
    ScriptSection,
    Task,
    User,
    UserCourse,
)
from services import media_service
from services.auth_service import get_current_user, require_course_access
from services.llm_service import LLMService
from services.references_service import build_references_text
from services.settings_resolver import get_effective_llm_config
from services.slides_service import (
    SlideError,
    THEME_TEMPLATES,
    apply_slide_edits,
    numbered_slide_content,
    parse_slides,
    resolve_theme,
    validate_theme,
)

router = APIRouter(prefix="/api", tags=["Folien"])
llm_service = LLMService()


def _check_member(user: User, session: Session, course_id: int) -> None:
    """Jedes Kurs-Mitglied (oder Admin) darf lesen."""
    if user.role == GlobalUserRole.ADMIN:
        return
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")


def _logo_url(session: Session, course_id: int, theme: dict) -> Optional[str]:
    """URL des hinterlegten Logos (nur bei gültigem Bild-Medium desselben Kurses)."""
    logo_id = theme.get("logo_media_id")
    if not logo_id:
        return None
    media = session.get(CourseMedia, logo_id)
    if media and media.course_id == course_id and media.media_type == "image":
        return media_service.media_url(media)
    return None


def _slide_md_parts(slide) -> list[str]:
    """Markdown-Parts eines Leaf-Slides in Visu-Reihenfolge (höchstens ein
    Part: der nicht-leere Folientext; Spalten sind Renderer-Seite) —
    für die Label-Zählung."""
    return [col for col in slide.columns if col]


@router.get("/courses/{course_id}/slides-theme")
async def get_slides_theme(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Aufgelöstes Folien-Theme (inkl. Template-Defaults) + Logo-URL."""
    _check_member(user, session, course_id)
    row = session.get(CourseSlidesTheme, course_id)
    theme = resolve_theme(row.theme if row else None)
    return {
        "theme": theme,
        "logo_url": _logo_url(session, course_id, theme),
        "templates": list(THEME_TEMPLATES.keys()),
    }


@router.put("/courses/{course_id}/slides-theme")
async def put_slides_theme(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Folien-Theme setzen (PROF/TUTOR). Body = Theme-JSON (s. slides_service)."""
    user, _ = user_and_course
    body = await request.json()

    try:
        theme = validate_theme(body)
    except SlideError as e:
        raise HTTPException(400, str(e))

    # Logo muss ein Bild aus der Medienbibliothek dieses Kurses sein
    if theme["logo_media_id"]:
        media = session.get(CourseMedia, theme["logo_media_id"])
        if not media or media.course_id != course_id or media.media_type != "image":
            raise HTTPException(400, "Das Logo muss ein Bild aus der Medienbibliothek dieses Kurses sein.")

    row = session.get(CourseSlidesTheme, course_id)
    if row:
        row.theme = theme
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = CourseSlidesTheme(course_id=course_id, theme=theme)
    session.add(row)
    session.commit()

    return {
        "message": "Folien-Design gespeichert.",
        "theme": theme,
        "logo_url": _logo_url(session, course_id, theme),
    }


@router.post("/courses/{course_id}/slides/ai-generate")
async def ai_generate_slide_deck(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """
    LLM generiert/ändert Titel und Inhalt eines Slide-Decks.

    Request:
        {
            "deck_id": 5,
            "topic": "Folien zu Kapitel 3 (Rekursion) erstellen",
            "minutes": 90,
            "current_title": "...",
            "current_content": "...",
            "generate_fields": {"title": true, "content": true}
        }

    Response:
        {
            // Leeres Feld = LLM hat das Feld nicht geändert (bestehender Wert bleibt).
            "title": "...", "content": "...",
            "edits_applied": 2,  // Anzahl angewendeter „content_edits“ (0 = Volltext/keine Änderung)
            "warnings": [],
            "latency_ms": 123
        }
    """
    body = await request.json()
    gen = body.get("generate_fields", {}) or {}
    generate_fields = [f for f in ("title", "content") if gen.get(f)]
    if not generate_fields:
        raise HTTPException(400, "Keine Felder angefordert.")

    deck_id = body.get("deck_id")
    deck = session.get(CourseMaterial, deck_id) if deck_id else None
    if not deck or deck.course_id != course_id or deck.material_type != MaterialType.SLIDES:
        raise HTTPException(404, "Slide-Deck nicht gefunden.")

    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    # Skript-Kapitel mit INHALT — das LLM baut die Folien daraus. Content pro
    # Kapitel wird gekürzt (Kontextgröße), max. 20 Kapitel. Die vorhandenen
    # Labels sind wichtig: gleiche Objekte → gleiche Labels → gleiche Nummer
    # wie im Skript + klickbarer Link zum Skript.
    chapters = []
    for s in session.exec(
        select(ScriptSection)
        .where(ScriptSection.course_id == course_id)
        .order_by(ScriptSection.display_order.asc())  # type: ignore[attr-defined]
    ).all()[:20]:
        figs, eqs = _scan_labels(s.content)
        code_labels = [l for l, _cap in _scan_code_labels(s.content)]
        box_labels = [f"@box:{l} ({t})" for l, t in _scan_box_labels(s.content)]
        tab_labels = [l for _cap, l in _scan_tables(s.content)]
        sec_labels = [h["label"] for h in _scan_headings(s.content) if h["label"]]
        ch_label = _chapter_label(s.content)
        if ch_label and ch_label in sec_labels:
            sec_labels.remove(ch_label)
            sec_labels.insert(0, ch_label + " (Kapitel-Label)")
        chapters.append(
            {
                "title": s.title,
                "labels":
                    [f"@fig:{l}" for l in figs]
                    + [f"@eq:{l}" for l in eqs]
                    + [f"@code:{l}" for l in code_labels]
                    + box_labels
                    + [f"@tab:{l}" for l in tab_labels]
                    + [f"@sec:{l}" for l in sec_labels],
                "content": (s.content or "")[:4000],
            }
        )

    # Alle Medien des Kurses — Medien dürfen in Skript UND Slides vorkommen,
    # daher nicht nur die skript-unabhängigen übergeben (ggf. einbinden bzw.
    # mit demselben @fig:-Label wie im Skript wiederverwenden).
    course_media = media_service.all_media_for_course(session, course_id)

    # Übungsaufgaben des Kurses (ID + Titel) — ggf. per @task:{id} im Deck
    # einbindbar (max. 1–2, eigene Folie „Übung“).
    course_tasks = [
        {"id": t.id, "title": t.title}
        for t in session.exec(
            select(Task)
            .where(Task.course_id == course_id)
            .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
        ).all()
    ]

    try:
        minutes = int(body.get("minutes"))
    except (TypeError, ValueError):
        minutes = 90
    minutes = max(5, min(minutes, 480))

    current_title = (body.get("current_title") or "").strip()
    current_content = (body.get("current_content") or "").strip()
    # Für das LLM: Folien mit expliziten Nummern („%% Folie N %%“), damit es
    # bei „content_edits“ fehlerfrei auf Folien referenzieren kann (nur wenn
    # „content“ angefordert ist — sonst fehlen die Marker-Erklärung und die
    # STELLENWEISE-Sektion). Die Rohversion bleibt für das serverseitige
    # Anwenden der Edits erhalten.
    llm_content = (
        numbered_slide_content(current_content)
        if current_content and "content" in generate_fields
        else current_content
    )
    llm_cfg = get_effective_llm_config(session, course_id)
    references = build_references_text(session, course_id)

    # DB-Connection vor dem langen LLM-Call freigeben (sonst hält die
    # Session sie die gesamte Generierung; s. database/base.py). Der
    # Rollback beendet nur das Read-Transaction — ORM-Objekte bleiben nutzbar.
    session.rollback()

    result = await llm_service.generate_slide_deck(
        course_name=course.name,
        topic=(body.get("topic") or "").strip(),
        minutes=minutes,
        generate_fields=generate_fields,
        current_title=current_title,
        current_content=llm_content,
        chapters=chapters,
        course_media=course_media,
        course_tasks=course_tasks,
        references=references,
        config=llm_cfg,
    )
    if not result.get("success"):
        raise HTTPException(502, f"LLM-Fehler: {result.get('error', 'Unbekannter Fehler')}")

    data = result.get("data") or {}
    response = {
        "title": "", "content": "", "edits_applied": 0, "warnings": [],
        "latency_ms": result.get("latency_ms", 0),
    }
    if "title" in generate_fields:
        response["title"] = (data.get("title") or "").strip()
    if "content" in generate_fields:
        new_content = (data.get("content") or "").strip()
        content_edits = data.get("content_edits")
        if new_content:
            # Serverseitig validieren, damit ein Format-Fehler nie in den
            # Editor gelangt (parse_slides ist strikt und wirft SlideError
            # mit deutscher Meldung).
            try:
                parse_slides(new_content)
            except SlideError as e:
                raise HTTPException(400, f"Das LLM lieferte ein ungültiges Slide-Deck: {e}")
            response["content"] = new_content
        elif content_edits:
            # Stellenweise Bearbeitung: Edits serverseitig auf den bestehenden
            # Inhalt anwenden (Anker müssen eindeutig sein, sonst HTTP 400 —
            # der Inhalt bleibt unverändert).
            if not current_content:
                raise HTTPException(400, "LLM lieferte stellenweise Edits („content_edits“), aber es existiert kein Inhalt zum Editieren. Bitte erneut versuchen.")
            try:
                merged = apply_slide_edits(current_content, content_edits)
            except SlideError as e:
                raise HTTPException(400, f"LLM-Edits nicht anwendbar: {e}")
            response["content"] = merged
            response["edits_applied"] = len(content_edits)
    return response


@router.get("/courses/{course_id}/slides-refmap")
async def slides_refmap(
    course_id: int,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Live-berechnete Referenz-Map der Slide-Decks (ohne DB-Speicherung).

    Ergänzt die Skript-Ref-Map (script-refmap): Labels, die im Skript vorkommen,
    tragen ihre Nummer dort und sind hier NICHT enthalten (keine Doppel-
    Nummerierung). Alle übrigen (slide-eigenen) Labels werden über alle
    Slide-Decks hinweg in kanonischer Deck-Reihenfolge (display_order, id)
    #    fortlaufend mit S-Nummern nummeriert (S1, S2, …) — je Typ (fig/eq/code/box/tab)
    #    ein eigener Zähler, analog zur Skript-Nummerierung. Die Reihenfolge kommt
    aus der DB (nicht aus der Render-Reihenfolge), damit sie bei Reorder der
    Decks stabil bleibt. Duplikate: erstes Vorkommen gewinnt (wie script-refmap).

    Payload:
        mode:       "edit" (PROF/TUTOR/Admin) bzw. "reading" (Student)
        courseId:   Kurs-ID
        deckOrder:  Deck-IDs in kanonischer Reihenfolge (display_order, id)
        labels:     {"<kind>:<label>": {kind, deckId, h, v, num, type?, preview?}}
                    # Key ist typ-qualifiziert, weil derselbe Label-NAME in
                    # verschiedenen Typen verschiedene Objekte bezeichnet
                    # (preview: gekürzter Objektinhalt für Hover-Tooltips)
                    # (wie die @kind:label-Referenzen); kind: fig/eq/code/box/tab;
                    # box: type = Box-Typ (z. B. "satz" → Referenz-Text „Satz S{n}");
                    # fig: sub = Letter a/b/… (Position des Inners im Komplex) bei
                    # gelabelten Subfigure-Innern, num = S-Nummer des PARENT-Komplexes
                    # → Referenz „Abb. S{n} a)"; num = S-Nummer (int, ohne „S");
                    # deckId/h/v = Reveal-Koordinaten der Folie (h = Block-, v =
                    # Stack-Index, 0-basiert) → Link-Ziel:
                    # /courses/{cid}/slides/{deckId}/present#/{h}/{v}
        maxSFig/maxSEq/maxSCode/maxSBox/maxSTab: höchst vergebene S-Nummer je Typ — Basis für
                    ungespeicherte Labels in der Editor-Vorschau
        figures:  [{deckId, h, v, p, num}] — NUR unlabeled Subfigure-Komplexe
                    (gelabelte Inners, kein äußeres {#fig:label}) mit ihrer
                    S-Nummer + Position (p = Teil-Index innerhalb der
                    Folie, s. _slide_md_parts). Der Client
                    (markdown-renderer.js) rendert pro Teil und bekommt so
                    die exakte Server-Nummer per Positions-Zip (Parität zur
                    Skript-Refmap `figures`); ohne die Liste müsste er nach
                    dem Maximum aller Decks weiterzählen (falsche Nummern).
    """
    _check_member(user, session, course_id)
    # Live-berechneter abgeleiteter Wert → niemals cachen (Browser/Proxy)
    response.headers["Cache-Control"] = "no-store"
    membership = _get_membership(session, user, course_id)
    is_tutor = user.role == GlobalUserRole.ADMIN or (
        membership is not None
        and membership.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    )

    # 1) Skript-Labels JE TYP (gleiche Sichtbarkeits-Regeln wie script-refmap):
    #    Nur ein Skript-Label desselben Typs trägt seine Nummer im Skript und
    #    schließt aus — @eq:foo und @fig:foo sind verschiedene Objekte, also
    #    darf ein Skript-Gleichungs-Label „foo“ eine Slide-Abbildung „foo“
    #    nicht von der S-Nummerierung ausschließen (und umgekehrt).
    sq = select(ScriptSection).where(ScriptSection.course_id == course_id)
    if not is_tutor:
        sq = sq.where(ScriptSection.is_visible == True)  # noqa: E712
    script_labels: dict[str, set[str]] = {"fig": set(), "eq": set(), "code": set(), "box": set(), "tab": set()}
    for s in session.exec(sq.order_by(ScriptSection.display_order.asc())).all():  # type: ignore[attr-defined]
        for _cap, label, inners in _scan_figures(s.content):
            if label:
                script_labels["fig"].add(label)
            for sub_label, _inner_cap in inners or []:
                if sub_label:
                    script_labels["fig"].add(sub_label)
        script_labels["eq"].update(_scan_labels(s.content)[1])
        script_labels["code"].update(label for label, _cap in _scan_code_labels(s.content))
        script_labels["box"].update(label for label, _typ in _scan_box_labels(s.content))
        script_labels["tab"].update(label for _cap, label in _scan_tables(s.content))

    # 2) Decks in kanonischer Reihenfolge; slide-eigene Labels → S-Nummern
    #    (fortlaufend über ALLE Decks, je Typ eigener Zähler).
    dq = select(CourseMaterial).where(
        CourseMaterial.course_id == course_id,
        CourseMaterial.material_type == MaterialType.SLIDES,
    )
    if not is_tutor:
        dq = dq.where(CourseMaterial.is_visible == True)  # noqa: E712
    decks = session.exec(
        dq.order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())
    ).all()  # type: ignore[attr-defined]

    labels: dict[str, dict[str, object]] = {}
    deck_order: list[str] = []
    counters = {"fig": 0, "eq": 0, "code": 0, "box": 0, "tab": 0}
    figures: list[dict[str, object]] = []  # s. Docstring (Client-Zip)

    def _claim(
        kind: str,
        label: str,
        deck_id: Optional[int],
        h: int,
        v: int,
        box_type: Optional[str] = None,
        preview: Optional[str] = None,
        sub: Optional[str] = None,
        num: Optional[int] = None,
    ) -> Optional[int]:
        # Key "<kind>:<label>": gleicher Label-NAME in verschiedenen Typen
        # (z. B. eq + fig + code „demo_1“) sind verschiedene Objekte mit
        # eigenen S-Nummern — analog zu den @kind:label-Referenzen.
        if label in script_labels[kind] or f"{kind}:{label}" in labels:
            return None  # Skript-Label (gleicher Typ) oder Duplikat → keine S-Vergabe
        if num is None:
            counters[kind] += 1
            num = counters[kind]
        entry: dict[str, object] = {"kind": kind, "deckId": deck_id, "h": h, "v": v, "num": num}
        if sub is not None:
            entry["sub"] = sub
        if box_type is not None:
            entry["type"] = box_type
        if preview:
            entry["preview"] = preview
        labels[f"{kind}:{label}"] = entry
        return num

    for deck in decks:
        try:
            slides = parse_slides(deck.content)
        except SlideError:
            slides = []  # ungültig (sollte nicht vorkommen: validiert beim Speichern)
        for h, slide in enumerate(slides):
            leaves = slide.children if slide.children else [slide]
            for v, leaf in enumerate(leaves):
                for p, part in enumerate(_slide_md_parts(leaf)):
                    previews = _scan_previews(part)  # "kind:label" → Hover-Preview
                    for _cap, label, inners in _scan_figures(part):
                        snum = None
                        if label is not None:
                            snum = _claim("fig", label, deck.id, h, v, preview=previews.get(f"fig:{label}"))
                        elif inners and any(l is not None for l, _inner_cap in inners):
                            # Ungelabelter Komplex mit gelabelten Inners: zahlt eine
                            # S-Nummer (Parität zum Skript), aber ohne eigenen Key.
                            # Position mitliefern → Client-Zip (s. Docstring).
                            counters["fig"] += 1
                            snum = counters["fig"]
                            figures.append({"deckId": deck.id, "h": h, "v": v, "p": p, "num": snum})
                        # Gelabelte Subfigure-Inners: eigene S-Nummerierung NUR
                        # wenn der Parent-Komplex eine S-Nummer bekommen hat
                        # (slide-eigenes Label); Komplex-Labels aus dem Skript
                        # gehören in die script-refmap (num = S-Nummer des Parent).
                        if inners and snum:
                            for i, (sub_label, _inner_cap) in enumerate(inners):
                                if sub_label:
                                    _claim(
                                        "fig", sub_label, deck.id, h, v,
                                        preview=previews.get(f"fig:{sub_label}"),
                                        sub=chr(97 + i), num=snum,
                                    )
                    for label in _scan_labels(part)[1]:
                        _claim("eq", label, deck.id, h, v, preview=previews.get(f"eq:{label}"))
                    for label, _cap in _scan_code_labels(part):
                        _claim("code", label, deck.id, h, v, preview=previews.get(f"code:{label}"))
                    for label, box_type in _scan_box_labels(part):
                        _claim("box", label, deck.id, h, v, box_type=box_type, preview=previews.get(f"box:{label}"))
                    for _cap, label in _scan_tables(part):
                        _claim("tab", label, deck.id, h, v, preview=previews.get(f"tab:{label}"))
        deck_order.append(str(deck.id))

    return {
        "mode": "edit" if is_tutor else "reading",
        "courseId": course_id,
        "deckOrder": deck_order,
        "labels": labels,
        "figures": figures,
        "maxSFig": counters["fig"],
        "maxSEq": counters["eq"],
        "maxSCode": counters["code"],
        "maxSBox": counters["box"],
        "maxSTab": counters["tab"],
    }
