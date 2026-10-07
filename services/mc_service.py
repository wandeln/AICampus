"""
Multiple-Choice-Aufgaben (task_type=mc): zentrale Logik.

mc_data-Struktur (JSON auf Task.mc_data):
{
  "penalty_enabled": bool,
  "shuffle_options": bool,   # Default true (Key fehlt = an): die
                             # Antwortoptionen werden in der Student-View
                             # bei jedem Seitenaufruf neu gemischt
  "questions": [
    {
      "question": "Fragetext (Markdown/LaTeX)",
      "points": 5,
      "multi_select": false,
      "options": [
        {"text": "...", "correct": true,  "feedback": "..."},
        {"text": "...", "correct": false, "feedback": "...",
         "feedback_wrong": "..."}   # nur bei multi_select=true relevant
      ]
    }
  ]
}

Shuffle (shuffle_options, Default true):
- student_view() liefert die Options-Texte in einer zufälligen
  Permutation — bei JEDEM Seitenaufruf neu gemischt (fairer und
  höherer Lerneffekt, wenn die Aufgabe später ein 2. Mal gelöst
  wird; Anti-Cheating).
- Je Frage liegt dafür der Key „order“ an: die Original-Indizes der
  Optionen in der Anzeigereihenfolge. Das Frontend schickt beim Submit
  die ORIGINAL-Indizes (value aus order), grade_mc() bleibt unverändert.
- tutor_view() bleibt IMMER in Originalreihenfolge. Kommentar/Summary
  referenzieren Optionen per Text-Ausschnitt — das System nutzt keine
  Buchstaben/Indizes in Anzeigen, weil die Student-Ansicht geshuffelt
  und unbeschriftet ist.

Feedback ist OPTIONAL: Bei leerem Feld zeigt das System bei der
Anzeige einfach „Richtig“ (korrekt beantwortete Option) bzw. „Falsch“
(falsch beantwortete Option) an.
- multi_select=false (Radio): ein Feld „feedback“ je Option — wird
  angezeigt, wenn der Student die Option auswählt (richtig oder falsch).
- multi_select=true (Checkbox): zwei Felder je Option:
  - „feedback“:        korrekt beantwortet (Status tp oder tn)
  - „feedback_wrong":  falsch beantwortet  (Status fp oder fn)

Scoring (serverseitig, synchron beim Submit — kein LLM-Grading):
- Pro Frage binär: gewählte Options-Menge == korrekte Menge → volle
  Punkte der Frage, sonst falsch.
- penalty_enabled: falsch → −Punkte der Frage (statt 0).
- Gesamt = max(0, Summe) — nie negativ.
"""

from __future__ import annotations

import json
import random
from typing import Optional

# Option-Status im Grading-Ergebnis:
#   tp: korrekt & gewählt        → grünes Feedback
#   fp: falsch & gewählt          → rotes Feedback (false positive)
#   fn: korrekt & NICHT gewählt   → orangenes Feedback (false negative)
#   tn: falsch & NICHT gewählt    → gedimmtes Feedback
OPTION_STATUS = ("tp", "fp", "fn", "tn")


class McValidationError(ValueError):
    """mc_data ist strukturell ungültig (mit verständlicher Meldung)."""


# ──────────────────────────────────────────────────────────────
# Parsing & Validierung
# ──────────────────────────────────────────────────────────────

def parse_mc_data(raw: Optional[str]) -> dict:
    """mc_data (JSON-String) zu dict; wirft McValidationError bei Invalid."""
    if not raw or not str(raw).strip():
        raise McValidationError("Keine Multiple-Choice-Fragen hinterlegt.")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise McValidationError(f"mc_data ist kein gültiges JSON ({e}).")
    if not isinstance(data, dict):
        raise McValidationError("mc_data muss ein JSON-Objekt sein.")
    validate_mc_data(data)
    return data


def validate_mc_data(data: dict) -> None:
    """Strukturell validieren; wirft McValidationError mit Grund."""
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise McValidationError("Mindestens eine Frage nötig.")

    data.setdefault("penalty_enabled", False)
    if not isinstance(data["penalty_enabled"], bool):
        raise McValidationError("penalty_enabled muss true/false sein.")

    data.setdefault("shuffle_options", True)
    if not isinstance(data["shuffle_options"], bool):
        raise McValidationError("shuffle_options muss true/false sein.")

    total_points = 0
    for qi, q in enumerate(questions):
        label = f"Frage {qi + 1}"
        if not isinstance(q, dict):
            raise McValidationError(f"{label}: ungültige Struktur.")

        text = (q.get("question") or "").strip()
        if not text:
            raise McValidationError(f"{label}: Fragetext darf nicht leer sein.")

        try:
            points = int(q.get("points", 0))
        except (TypeError, ValueError):
            raise McValidationError(f"{label}: Punkte müssen eine ganze Zahl sein.")
        if points < 0:
            raise McValidationError(f"{label}: Punkte dürfen nicht negativ sein.")
        q["points"] = points
        total_points += points

        multi = bool(q.get("multi_select", False))
        q["multi_select"] = multi

        options = q.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise McValidationError(f"{label}: Mindestens 2 Antwortoptionen nötig.")
        if len(options) > 8:
            raise McValidationError(f"{label}: Maximal 8 Antwortoptionen.")

        n_correct = 0
        for oi, opt in enumerate(options):
            olabel = f"{label}, Option {oi + 1}"
            if not isinstance(opt, dict):
                raise McValidationError(f"{olabel}: ungültige Struktur.")
            otext = (opt.get("text") or "").strip()
            if not otext:
                raise McValidationError(f"{olabel}: Antworttext darf nicht leer sein.")
            opt["text"] = otext
            # Feedback ist optional — bei leerem Feld zeigt die Anzeige
            # das System-Default („Richtig“/„Falsch“). feedback_wrong
            # wird bei Mehrfachauswahl-Fragen für falsch beantwortete
            # Optionen (fp/fn) verwendet.
            opt["feedback"] = (opt.get("feedback") or "").strip()
            opt["feedback_wrong"] = (opt.get("feedback_wrong") or "").strip()
            correct = bool(opt.get("correct", False))
            opt["correct"] = correct
            n_correct += correct

        if multi:
            if n_correct < 1:
                raise McValidationError(f"{label}: Mind. eine richtige Antwort nötig.")
            if n_correct == len(options):
                raise McValidationError(f"{label}: Mind. eine FALSCHE Antwort nötig.")
        else:
            if n_correct != 1:
                raise McValidationError(
                    f"{label}: Genau EINE richtige Antwort nötig "
                    f"(sind {n_correct}) — oder „mehrere Antworten möglich“ aktivieren."
                )

    if total_points <= 0:
        raise McValidationError("Die Fragen müssen insgesamt mind. 1 Punkt wert sein.")


def mc_total_points(data: dict) -> int:
    return sum(int(q.get("points", 0)) for q in data.get("questions", []))


# ──────────────────────────────────────────────────────────────
# Ansichten
# ──────────────────────────────────────────────────────────────

def student_view(data: dict, task_id: Optional[int] = None,
                 user_id: Optional[int] = None) -> dict:
    """Sanitisierte Ansicht für Studenten: KEIN Correct-Flag, KEIN Feedback.

    Wird in den Studenten-Template-Kontext und in student-reichbare
    API-Antworten geschickt — dort darf der Student den Lösungsweg
    nicht vor dem Einreichen erkennen können.

    Bei shuffle_options (Default true) UND vorhandenem task_id/user_id
    werden die Options-Texte je Frage in einer zufälligen Permutation
    geliefert — bei JEDEM Seitenaufruf neu gemischt (System-RNG, kein
    fester Seed: fairer und besserer Lerneffekt bei Wiederholungen,
    Anti-Cheating).
    „order“ enthält die Original-Indizes in Anzeigereihenfolge; das
    Frontend sendet beim Submit die Original-Indizes, grade_mc() bleibt
    unverändert. Ohne task_id/user_id = Originalreihenfolge.
    """
    questions = []
    for qi, q in enumerate(data["questions"]):
        n = len(q["options"])
        if data.get("shuffle_options", True) and task_id is not None \
                and user_id is not None and n > 1:
            order = list(range(n))
            random.shuffle(order)  # System-RNG: bei jedem Aufruf neu
        else:
            order = list(range(n))
        questions.append({
            "question": q["question"],
            "points": q["points"],
            "multi_select": q["multi_select"],
            "options": [q["options"][i]["text"] for i in order],
            "order": order,
        })
    return {
        "penalty_enabled": data.get("penalty_enabled", False),
        "questions": questions,
    }


def tutor_view(data: dict) -> dict:
    """Vollständige Ansicht für Tutoren (Correct-Flags + Feedback).

    IMMER in Originalreihenfolge (kein Shuffle) — der Fragen-Editor und
    die Submission-Review basieren auf den Original-Indizes/-Buchstaben.
    """
    return {
        "penalty_enabled": data.get("penalty_enabled", False),
        "shuffle_options": data.get("shuffle_options", True),
        "questions": [
            {
                "question": q["question"],
                "points": q["points"],
                "multi_select": q["multi_select"],
                "options": [
                    {"text": opt["text"], "correct": opt["correct"],
                     "feedback": opt.get("feedback", ""),
                     "feedback_wrong": opt.get("feedback_wrong", "")}
                    for opt in q["options"]
                ],
            }
            for q in data["questions"]
        ],
    }


# ──────────────────────────────────────────────────────────────
# Grading (synchron, serverseitig)
# ──────────────────────────────────────────────────────────────

def _option_snippet(text: str, limit: int = 40) -> str:
    """Kurzer Text-Ausschnitt einer Option für die Summary.

    Die Anzeigen nutzen keine Buchstaben/Indizes (Student-Ansicht ist
    geshuffelt und unbeschriftet) — daher wird der Options-Text selbst
    (gekürzt, Whitespace kollabiert) referenziert.
    """
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return f"„{flat}“"


def grade_mc(data: dict, answers: list) -> dict:
    """Bewertet die Studentenantworten gegen mc_data.

    answers: Liste je Frage der gewählten Options-Indizes,
    z. B. [[0], [1, 3]] (Single: immer genau ein Element,
    Multi: mind. eins).

    Wirft McValidationError bei inkonsistenten Antworten.
    Rückgabe: {total, total_max, penalty_enabled, summary,
               questions: [Frage-Details inkl. Option-Statusen]}
    """
    questions = data["questions"]
    penalty = data.get("penalty_enabled", False)

    if not isinstance(answers, list) or len(answers) != len(questions):
        raise McValidationError(
            f"Antwortanzahl passt nicht ({len(answers) if isinstance(answers, list) else 0} "
            f"von {len(questions)} Fragen)."
        )

    total = 0
    total_max = 0
    q_results = []

    for qi, (q, sel) in enumerate(zip(questions, answers)):
        n_opts = len(q["options"])
        if not isinstance(sel, list) or not sel:
            raise McValidationError(f"Frage {qi + 1}: Mindestens eine Antwort nötig.")
        try:
            sel_set = {int(i) for i in sel}
        except (TypeError, ValueError):
            raise McValidationError(f"Frage {qi + 1}: Ungültige Antwort-Indizes.")
        if any(i < 0 or i >= n_opts for i in sel_set):
            raise McValidationError(f"Frage {qi + 1}: Ungültiger Options-Index.")
        if len(sel_set) != len(sel):
            raise McValidationError(f"Frage {qi + 1}: Doppelte Auswahl.")

        correct_set = {
            i for i, opt in enumerate(q["options"]) if opt["correct"]
        }
        points = q["points"]
        is_correct = sel_set == correct_set

        if is_correct:
            earned = points
        else:
            earned = -points if penalty else 0
        total += earned
        total_max += points

        options_detail = []
        for oi, opt in enumerate(q["options"]):
            selected = oi in sel_set
            correct = opt["correct"]
            if selected and correct:
                status = "tp"
            elif selected:
                status = "fp"
            elif correct:
                status = "fn"
            else:
                status = "tn"
            # Feedback-Auflösung je Frage-Typ (leeres Feld → "", dann
            # zeigt die Anzeige das System-Default „Richtig“/„Falsch“):
            # - Mehrfachauswahl: korrekt beantwortet (tp/tn) →
            #   „feedback“, falsch beantwortet (fp/fn) → „feedback_wrong“.
            # - Einzelwahl: nur die ausgewählte Option zeigt ihr Feedback.
            if q["multi_select"]:
                fb = (opt.get("feedback", "") if status in ("tp", "tn")
                      else opt.get("feedback_wrong", ""))
            else:
                fb = opt.get("feedback", "") if selected else ""
            options_detail.append({
                "index": oi,
                "text": opt["text"],
                "correct": correct,
                "selected": selected,
                "status": status,
                "feedback": fb,
            })

        q_results.append({
            "index": qi,
            "correct": is_correct,
            "points": points,
            "earned": earned,
            "multi_select": q["multi_select"],
            "selected": sorted(sel_set),
            "options": options_detail,
        })

    final_total = max(0, total)

    # Kurz-Zusammenfassung für Submission.solution (Historie/Export)
    summary = " | ".join(
        f"Frage {qi + 1}: "
        + ", ".join(_option_snippet(questions[qi]["options"][i]["text"])
                   for i in r["selected"])
        + f" ({'✓' if r['correct'] else '✗'})"
        for qi, r in enumerate(q_results)
    )

    return {
        "total": float(final_total),
        "total_max": float(total_max),
        "penalty_enabled": penalty,
        "summary": summary,
        "questions": q_results,
    }


def mc_report_summary(mc_result: dict) -> list[str]:
    """Kompakte Text-Zusammenfassung eines MC-Ergebnisses (grade_mc-Rückgabe)
    für LLM-Reports: pro Frage eine Zeile mit Status und Punkten; bei falsch
    zusätzlich die gewählten bzw. korrekten Optionen (gekürzt) und das
    hinterlegte Feedback zu den falsch beantworteten Optionen (fp/fn).

    Wird NUR von den Report-Generatoren an das LLM übergeben — das
    Feedback.comment bleibt leer, die Feedback-UI zeigt ihn nicht an."""
    lines = []
    questions = mc_result.get("questions") or []
    for r in questions:
        mark = "✓" if r["correct"] else "✗"
        line = (f"[MC] Frage {r['index'] + 1}/{len(questions)}: {mark} "
                f"({int(r['earned'])}/{int(r['points'])} P.)")
        if not r["correct"]:
            options = r.get("options") or []
            selected = [_option_snippet(o["text"]) for o in options if o["selected"]]
            correct = [_option_snippet(o["text"]) for o in options if o["correct"]]
            if selected:
                line += f" — gewählt: {', '.join(selected)}"
            if correct:
                line += f"; richtig war: {', '.join(correct)}"
            fbs = [o.get("feedback", "") for o in options
                   if o["status"] in ("fp", "fn") and o.get("feedback")]
            if fbs:
                line += "; Feedback: " + " | ".join(f[:120] for f in fbs[:4])
        lines.append(line)
    return lines


def parse_student_answers(mc_answers: Optional[str]) -> Optional[list]:
    """Strukturierte MC-Antworten aus Submission.mc_answers lesen.

    Format: "mc:v1:<json>" (z. B. mc:v1:[[0],[1,3]]).
    Returns None, wenn kein MC-Answer-Payload.
    """
    if not mc_answers or not mc_answers.startswith("mc:v1:"):
        return None
    try:
        parsed = json.loads(mc_answers[len("mc:v1:"):])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None
