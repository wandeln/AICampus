"""Template-Render-Smoke-Test für die Task-Seiten (Student + Tutor).

Rendert alle task_solve_*/task_detail_*-Templates mit einem realistischen
Dummy-Context und wirft bei Jinja-Fehlern. Start:  python scripts/check_task_templates.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import templates  # noqa: E402

COURSE = {"id": 1, "name": "ML-Kurs", "semester": "SS 2026", "description": "Dummy"}

TASK_WS = {
    "id": 42,
    "title": "NN auf MNIST",
    "task_type": "workspace",
    "description": "Trainiere ein kleines CNN.",
    "max_points": 10,
    "max_attempts": 3,
    "deadline": "2026-10-01T23:59:00",
    "model_solution": None,
    "is_visible": True,
    "hints_enabled": True,
    "workspace_main_file": "main.py",
    "workspace_image": "python-ml",
    "workspace_engines": ["local"],
    "workspace_timeout": 900,
    "workspace_cpu": 2.0,
    "workspace_memory": "4g",
    "workspace_disk_quota": 1.0,
    "workspace_internet": False,
    "workspace_assets_status": '{"http://compute-agent:8700": {"assets": "ready", '
                               '"task_image": "building", "image": "ready"}}',
}

TASK_TEXT = dict(TASK_WS, id=44, task_type="text", title="Text-Aufgabe")

# Kurs-Tab-Leiste (wie von _course_tab_context() gebaut)
TABS = [
    {"key": "script", "icon": "📖", "label": "Skript", "url": "/courses/1/script", "active": False},
    {"key": "slides", "icon": "📽️", "label": "Folien", "url": "/courses/1/slides", "active": False},
    {"key": "tasks", "icon": "📋", "label": "Aufgaben", "url": "/courses/1/tasks", "active": True},
    {"key": "forum", "icon": "💬", "label": "Forum", "url": "/courses/1/forum", "active": False, "badge": 0},
]


def base_ctx(task, is_tutor, tpl_type):
    return {
        "request": None,
        "page_title": task.get("title") if task else "Neu",
        "current_user": {"id": 1, "name": "Test", "role": "PROF"},
        "courses": [COURSE],
        "selected_course_id": 1,
        "is_admin": False,
        "course": COURSE,
        "task": task,
        "is_tutor": is_tutor,
        "is_student_view": False,
        "tabs": TABS,
        "compute_enabled": True,
        "tpl_type": tpl_type,
        "code_editor": True,
        "my_submissions": [{
            "id": 1, "solution": "📦 Workspace: 3 Datei(en)",
            "workspace_snapshot": task and task.get("task_type") == "workspace",
            "attempt_number": 1,
            "submitted_at": datetime(2026, 9, 1, 12, 0),
            "status": "graded", "solve_time_seconds": 600,
            "feedback_list": [{
                "id": 1, "source": "llm", "points_earned": 8,
                "comment": "Gut gemacht.", "giver_name": "LLM",
                "created_at": datetime(2026, 9, 1, 12, 1),
            }],
        }] if task else [],
        "latest_points": 8,
        "total_attempts": 1,
        "task_percentile": 55,
        "task_group_avg": 6.5,
        "prev_task": None,
        "next_task": {"id": 99, "title": "Nächste"},
        "LLM_TIMEOUT": 120,
        "preview_base": "",
        "preview_base_domain": None,
    }


def review_ctx(task):
    ctx = base_ctx(task, True, task["task_type"])
    ctx.update({
        "course_id": 1,
        "task_id": task["id"],
        "student_id": 7,
        "task_title": task["title"],
        "task_type": task["task_type"],
        "task_type_display": "Workspace-Aufgabe",
        "max_points": task["max_points"],
        "student_name": "Teststudent",
        "student_username": "testuser",
        "hints": [],
        "hints_enabled": task["hints_enabled"],
    })
    return ctx


def main():
    failures = []
    cases = [
        # (template, ctx)
        ("student/task_solve_text.html", base_ctx(TASK_TEXT, False, "text")),
        ("student/task_solve_workspace.html", base_ctx(TASK_WS, False, "workspace")),
        ("student/task_solve_workspace.html", base_ctx(TASK_WS, True, "workspace")),  # as_student
        ("tutor/task_detail_text.html", base_ctx(TASK_TEXT, True, "text")),
        ("tutor/task_detail_workspace.html", base_ctx(TASK_WS, True, "workspace")),
        ("tutor/task_detail_workspace.html", base_ctx(None, True, "workspace")),  # neue Aufgabe
        ("tutor/submission_review.html", review_ctx(TASK_WS)),
        ("tutor/submission_review.html", review_ctx(TASK_TEXT)),
    ]
    for name, ctx in cases:
        try:
            out = templates.env.get_template(name).render(**ctx)
            print(f"OK   {name}  ({len(out)} chars)")
        except Exception as e:  # noqa: BLE001
            failures.append((name, e))
            print(f"FAIL {name}: {e}")
    if failures:
        sys.exit(1)
    print("ALLE TEMPLATES OK")


if __name__ == "__main__":
    main()
