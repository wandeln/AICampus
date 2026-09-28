# AICampus — Architektur & Debugging

Komponentenüberblick, Projektstruktur und Fehlersuche.

## Komponenten

1. **AICampus-App** (FastAPI): Web-UI (Jinja2 + HTMX), REST-API,
   Grading-Orchestrierung, LLM-Client, Sandbox, Medien/Import.
   Ein-Prozess-Architektur (`main.py` → Router aus `api/`), SQLite/PostgreSQL.
2. **Compute-Agent** (`compute_agent/`): FastAPI-Service auf einem
   (Compute-)Server, der über den **Docker-Daemon des Hosts** die
   Workspace-Container der Students verwaltet (Lebenszyklus, Terminal-PTY,
   Run-Runs, Preview-Relay, Image-Spec-Builds, GPU-Passthrough,
   Idle-Reaper, Disk-Quota-Watchdog).
3. **Kommunikation App ↔ Agent:** HTTP + HMAC-Op-Tokens (60 s, scopet);
   Health-Polling (30-s-Cache) für die Engine-Registry.

## Projektstruktur

```
AICampus/
├── main.py                  # FastAPI App: Lifespan (DB-Setup, Admin-Bootstrap,
│                            #   Migrationen), Web-Routes (Dashboard, Kurs-Tabs),
│                            #   Preview-Subdomain-Middleware
├── config.py                # Zentrale Konfiguration (alle Env-Vars, s. docs/configuration.md)
├── .env.example             # Konfigurations-Vorlage
├── requirements.txt
├── migrate_*.py             # Einmal-Migrationen (Altschema → Neu)
│
├── database/
│   ├── base.py              # DB-Engine + Session + create_db_and_tables
│   └── models.py            # SQLModel-Tabellen (User, Course, Task, CourseMedia,
│                            #   CourseReference, Forum, ImageSpec, GlobalSettings, …)
│
├── api/                     # FastAPI-Router (REST)
│   ├── auth.py              # Login/Logout/Register
│   ├── admin.py             # Admin-Konsole: Kurse, User, globale Settings (LLM/LDAP/Compute)
│   ├── course_members.py    # Kurs-Mitglieder + Einladungen (Prof/Admin)
│   ├── tutor.py             # Aufgaben CRUD + LLM-Generierung + Korrektur + Workspace-Engine
│   ├── student.py           # Aufgaben + Einreichung + Feedback + Workspace-API
│   ├── script.py            # Skript-Kapitel: CRUD/Freischalten/Reihenfolge/LLM
│   ├── script_questions.py  # Aufgaben-Referenzen auf Skript-Kapitel
│   ├── slides.py            # Slide-Theme (Design/Logo) + LLM-Deck-Generierung
│   ├── materials.py         # Skript (Legacy-Einzel-Skript) + Slide-Decks (Markdown)
│   ├── media.py             # Medienbibliothek: Upload/Ersatz/LLM-Beschreibung/Applet-Studio
│   ├── references.py        # Quellen-Bibliothek (BibTeX) + Import
│   ├── importer.py          # Kurs-Material-Import (Zip-LLM-Wizard, 202-Jobs)
│   ├── image_specs.py       # Image-Specs (Workspace-Images) verwalten
│   ├── forum.py             # Kurs-Forum: Kanäle + Nachrichten
│   └── user_settings.py     # Eigene Einstellungen (Name/Passwort)
│
├── services/                # Business-Logik (ohne HTTP)
│   ├── auth_service.py      # JWT + LDAP-Auth + RBAC (require_course_access, …)
│   ├── llm_service.py       # OpenAI-kompatibler LLM-Client (alle Generierungs-/Grading-Calls)
│   ├── grading_service.py   # Grading-Orchestrierung (Text/Code/Workspace)
│   ├── sandbox_runner.py    # Sichere serverseitige Code-Ausführung (Code-Aufgaben)
│   ├── compute_client.py    # HTTP-Client zum Compute-Agent (HMAC-Tokens, Health)
│   ├── workspace_service.py # Workspace-Lebenszyklus (Start/Stop/Snapshot/Runs)
│   ├── workspace_presets.py # Validierung des LLM-Workspace-Generierungs-Outputs
│   ├── image_spec_service.py# Image-Specs (Registry, Auflösung, Status)
│   ├── import_service.py    # Import-Wizard-Job-Runner (stages, Pause/Resume/Cancel)
│   ├── media_service.py     # Medien-Speicher + MediaUsage-Reconciliation
│   ├── references_service.py# Quellen (BibTeX-Parsing, Zitate)
│   ├── bibtex.py            # BibTeX-Utilities
│   ├── slides_service.py    # Slide-Decks (Folien-Split, PDF-Export)
│   ├── content_edits.py     # Strukturierte LLM-Content-Edits (Kapitel/Folien)
│   ├── export_service.py    # Excel-Export (.xlsx)
│   ├── preview_proxy.py     # Backend-Proxy/Preview-Subdomain-Middleware (Agent-Preview)
│   └── settings_resolver.py # Settings-Auflösung (Kurs → global → .env → Default)
│
├── compute_agent/           # Compute-Agent (eigener Service, s. deployment-Doku)
│   ├── main.py              # Agent-FastAPI-App (Workspaces, Assets, Init-Builds, WS-Terminal)
│   ├── auth.py              # HMAC-Op-Token (Sign + Verify)
│   ├── docker_ops.py        # Docker-Daemon-Operationen (Container, Volumes, Images)
│   ├── spec.py              # Slim-Spec-Parsing & Validierung (Workspace-Container)
│   ├── image_spec.py        # Image-Spec-Validierung (reine Dockerfiles, Content-Hash)
│   ├── runs.py              # Run-Jobs (docker exec, Logs, Kill)
│   ├── terminal.py          # Terminal-Sessionen (PTY um docker exec, Resize, Orphan-Schutz)
│   ├── preview.py / preview_pipe.py # Preview-Relay (Port-Forwarding auf Agent-Preview-Port)
│   ├── reaper.py            # Idle-Reaper (inaktive Workspaces stoppen)
│   └── relay/               # Relay-Binary (docker cp → Container, Port-Forward)
│
├── templates/               # Jinja2 (Master-Layout base.html)
│   ├── login.html / dashboard.html / join.html / user_settings.html
│   ├── admin/dashboard.html           # Admin-Konsole (Kurse, User, Systemeinstellungen)
│   ├── course/                  # Kurs-Tab-Seiten (Tab-Leiste + Rollensichtbarkeit)
│   │   ├── base.html / _tabs.html / _course_header.html
│   │   ├── script.html / slides.html / slides_edit.html / slides_present.html
│   │   ├── applet_studio.html   # Applet-Studio (LLM + CodeMirror)
│   │   ├── media.html / references.html / forum.html
│   │   ├── tasks_student.html / tasks_tutor.html / overview.html
│   │   ├── members.html / settings.html
│   ├── tutor/
│   │   ├── task_detail_base.html / _text / _code / _workspace   # Aufgaben-Editor
│   │   └── submission_review.html   # Einzelne Einreichung bewerten
│   └── student/
│       ├── task_solve_base.html / _text / _code / _workspace    # Lösungsseiten
│
├── static/
│   ├── css/                 # Custom Styles (main.css, workspace.css, …)
│   ├── js/
│   │   ├── markdown-renderer.js   # Markdown + KaTeX + Zitate (Client-Rendering)
│   │   ├── workspace.js           # Workspace-UI (Datei-Tree, Editor, Views)
│   │   ├── codemirror6-setup.js / codemirror6-shim.js
│   │   ├── slides.js / image-specs-ui.js
│   └── vendor/              # Minified Vendor-Libs (gitignored): htmx, tailwind,
│                            #   codemirror, katex, reveal, xterm, plotly, chart, three
│
├── prompts/                 # LLM-Prompt-Templates (je Feature)
│   ├── grading_prompt.py    # LLM-Korrektur
│   ├── creation_prompt.py   # Aufgaben-Generierung (Text)
│   ├── code_task_prompt.py  # Code-Aufgaben (Vorlage/Tests/Lösung)
│   ├── workspace_task_prompt.py / image_spec_prompt.py  # Workspace + Image-Specs
│   ├── script_prompt.py / script_question_prompt.py     # Skript-Kapitel
│   ├── slides_prompt.py     # Slide-Decks
│   ├── applet_prompt.py     # Applet-Studio
│   ├── import_prompt.py     # Import-Wizard (Analyse/Planner/Generierung)
│   ├── hint_prompt.py       # Sokratische Hints
│   └── report_prompt.py / markdown_manual.py
│
├── deploy/                  # Deployment-Artefakte
│   ├── compose.local.yml    # Docker-Hybrid (App + Agent, eine Maschine)
│   ├── compose.dev.yml      # Overlay: Live-Code + Auto-Reload
│   ├── compose.compute-only.yml  # Agent-only-Container (reine Compute-Server)
│   ├── nginx.example.conf        # nginx-Produktivtemplate (TLS vor dem Container)
│   └── aicampus-compute-tunnel.service  # SSH-Tunnel zu Remote-Compute-Servern
│
├── scripts/                 # Maintenance-/Tooling-Skripte (Migrationen, Relay-Build)
├── docs/                    # Diese Dokumentation (+ interne Plan-Docs)
└── data/                    # Laufzeitdaten (gitignored): aicampus.db, media/, avatars/,
                             #   workspaces/, submissions/, imports/, compute-assets/
```

## Debugging

### Logs

| Komponente | Log-Quelle |
|---|---|
| App + Agent (Docker) | `docker compose -f deploy/compose.local.yml logs -f <aicampus\|compute-agent>` |
| LLM-Tunnel | `journalctl -u aicampus-llm-tunnel -n 50` |
| Compute-Tunnel | `journalctl -u aicampus-compute-tunnel -n 50` |

> Bei Problemen immer zuerst die letzten Zeilen der zugehörigen Log-Datei
> prüfen — FastAPI/uvicorn loggen Errors + Request-Trace.

### API & Health-Endpunkte

- **Swagger UI:** `http://localhost:8000/docs` — interaktive REST-API.
- **ReDoc:** `http://localhost:8000/redoc`.
- **Agent-Health:** `GET /health` auf `127.0.0.1:8700` (HMAC-Token
  erforderlich, wenn `AGENT_KEY` gesetzt — `{"ok": true, "docker": true,
  "gpus": […]}`). In der UI: Admin-Konsole → Compute/Workspace (Status +
  🧪 Test-Button je Engine).
- **LLM-Verbindung:** Admin-Konsole → Systemeinstellungen → „LLM testen".

### Typische Symptome

| Symptom | Ursache / erster Check |
|---|---|
| Workspace-Aufgaben „⚠️ Compute-Server nicht erreichbar" | Agent down? Tunnel down? Engine-Registry (URL/Key)? `curl http://127.0.0.1:8700/health` |
| LLM-Calls Timeouts | `LLM_TIMEOUT` erhöhen (lokale Modelle), LLM-Server-Last, Tunnel-Logs |
| Agent startet nicht (Docker) | `AGENT_KEY` fehlt (Compose-Interpolation liest kein `env_file` → Shell-Export oder `deploy/.env`) |
| `host.docker.internal` nicht erreichbar | Tunnel bindet an falsche IP (muss Bridge-IP des Compose-Netzes sein, s. [installation.md §6](installation.md#6-llm-hinter-ssh-erreichen-llm-tunnel-service)); UFW-Freigabe |
| Empty-State bei Task-Generierung (Workspace) | Keine Engine registriert / keine installierten Image-Specs auf der Engine |
| DB-Konsistenz | Migrationen laufen beim Start (`migrate_schema`, …); `migrate_*.py`-Skripte im Repo-Root sind Einmal-Migrationen aus älteren Versionen |

### Datenbank (Entwicklung)

```bash
sqlite3 data/aicampus.db ".tables"
sqlite3 data/aicampus.db "select id, username, role from users limit 10;"
```

> Vor dem Bearbeiten von Daten App stoppen; für Konsistenz-Backup:
> `sqlite3 data/aicampus.db ".backup backup.db"`.
