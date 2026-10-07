# 📚 AICampus

**KI-Plattform für MINT-Lehre — mit voller Datenhoheit.**

AICampus bündelt drei Dinge, die Sie für moderne Lehre brauchen: **KI-gestützte
Erstellung von Lehrmaterialien**, **automatisiertes Feedback für Studierende** und
**alles für den Kursalltag** — laufend auf Ihren eigenen Servern. Keine Cloud,
keine externen APIs: AICampus arbeitet mit **lokalen LLMs** (Qwen, Llama,
Mistral & Co. auf Ihren GPU-Servern). Alle Daten — Lösungen, Einreichungen,
Kursmaterial — bleiben in Ihrem Netz.

## ✨ Features

### 🔒 Datenschutz durch lokale LLMs

- **Keine Cloud, kein Datenabfluss:** AICampus spricht jeden
  **OpenAI-kompatiblen Endpoint** — betreiben Sie Ihr LLM mit vLLM, TGI oder
  Ollama auf Ihren eigenen (Uni-)GPU-Servern.
- **Zwei Endpoints, ein Prinzip:** Vertrauliche Daten (Einreichungen,
  Korrekturen) gehen nur an Ihr *privates* LLM; für nicht-sensitive Aufgaben
  (z. B. neue Aufgaben generieren) ist ein zweites, optionales *öffentliches*
  Endpoint konfigurierbar.
- LLM-Verbindung per **SSH-Tunnel** möglich (reboot-fester systemd-Service) —
  auch wenn der GPU-Server nur per SSH erreichbar ist.
- Optionale **LDAP-Anbindung** (Uni-Accounts) und JWT-Auth mit httpOnly-Cookies.

### 🎓 Automatisiertes Feedback & Hilfestellung für Studierende

- **Sofortiges Feedback zu jeder Abgabe** — statt Wartezeit auf Sprechstunde:
  - **Textaufgaben:** LLM-Korrektur mit konstruktivem, differenziertem Feedback
  - **Workspace-Aufgaben:** automatische Bewertung ganzer Code-Umgebungen —
    Studierende bekommen einen **eigenen, isolierten Container** (Mini-IDE mit
    Editor, Terminal und Web-Preview), auf Wunsch **mit GPU**
  - **Sokratische Hinweise:** das LLM führt an der Aufgabe entlang, statt die
    Lösung wegzugeben
- Versuchslimits, Abgabefristen und Punkte pro Aufgabe — automatisch.
- **Gamification:** Punkte, Fortschrittsbalken und Medaillen pro Aufgabe
  (Bronze ab 70 %, Silber ab 80 %, Gold ab 90 %, Platinum bei 100 %) — plus
  Perzentil im Kurs und Kursdurchschnitt. Sichtbarer Fortschritt, der
  Studierende motiviert; ohne zusätzliche Konfiguration.
- **Kurs-Forum** mit Kanälen für Fragen & Austausch.
- Für Sie: **Punktestands-Übersicht, Korrektur-Workflow und Excel-Export**.

### 🧠 KI-Unterstützung bei der Erstellung von Lehrmaterialien

- **Übungsaufgaben:** aus Thema + Schwierigkeitsgrad generiert das LLM
  Text-, Code- oder Workspace-Aufgaben — inkl. Vorlage, Unit-Tests und
  Musterlösung, die Sie vor der Freigabe prüfen und anpassen.
- **Vorlesungsskript:** Kapitel in Markdown mit **LaTeX und Mermaid-Diagrammen**
  per LLM erzeugen oder bestehende Kapitel überarbeiten lassen — Kapitel für
  Kapitel einzeln freischaltbar.
- **Slides:** Folien-Decks (reveal.js) LLM-gestützt erstellen, präsentieren
  und als PDF exportieren.
- **Interaktive Applets:** per Beschreibung — oder als **Referenzbild**
  (z. B. eine Skizze aus einem Paper) — generiert das LLM interaktive
  HTML-Applets für Ihr Kursmaterial.
- **Bestehendes Material importieren:** bestehende Skripte & Folien als Zip
  (PDF, PowerPoint, Word) hochladen — ein mehrstufiger LLM-Wizard wandelt es
  interaktiv in Kurskapitel, Slide-Decks, Medien und eine
  **Quellenbibliothek (BibTeX)** um.
- **Medienbibliothek:** Bilder hochladen, das Vision-LLM erzeugt Titel und
  Beschreibung automatisch; Verwendungen im Material werden nachverfolgt.

### 🏫 Alles für den Kursalltag

- **Rollenmodell:** Admin, Prof, Tutor, Student — mit klarer
  Berechtigungslogik (Details in der [User Guide](docs/user-guide.md)).
- **Kurse** per Einladungslink (mit Gültigkeit & Nutzungsgrenze) oder
  manueller Hinzufügung.
- 100 % Open-Source, 100 % auf Ihrer Hardware.

## 🚀 Quick Start

**Voraussetzungen:** Linux-Server (oder macOS/Windows zum Ausprobieren),
Docker + Docker Compose, ein OpenAI-kompatibles LLM-Endpoint
(z. B. vLLM/Ollama auf einem GPU-Server).

### Docker (empfohlen)

```bash
git clone <repo-url> AICampus && cd AICampus

# 1) Konfiguration: LLM-Endpoint setzen
cp .env.example .env
nano .env                       # LLM_API_URL, LLM_API_KEY, LLM_MODEL, …

# 2) Start (Web-App + Compute-Agent für Workspace-Aufgaben)
export COMPUTE_AGENT_KEY=<Wert von COMPUTE_AGENT_KEY aus der .env>
docker compose -f deploy/compose.local.yml up --build -d
```

- Web-App: <http://localhost:8000>
- **Erster Login: `admin` / `admin` — Passwort nach dem ersten Login ändern!**
- Danach in der **Admin-Konsole**: LLM-Verbindung testen, Kurs anlegen,
  Mitglieder einladen, optional Compute-Engine registrieren.

### Nativ (ohne Docker, nur Entwicklung)

```bash
cd AICampus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # LLM-Endpoint setzen
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Produktiv-Betrieb: Docker + nginx (TLS) vor dem Container —
[docs/installation.md](docs/installation.md).

> **Workspace-Aufgaben** (eigene Container pro Student, GPU) erfordern
> zusätzlich den Compute-Agent — Installation in
> [docs/installation.md](docs/installation.md). Ohne Agent degradieren
> Workspace-Aufgaben sauber (Ausführen/Abgeben ausgegraut); Text- und
> Multiple-Choice-Aufgaben bleiben voll funktionsfähig.

## 🛠️ Tech-Stack (100 % Open-Source)

| Komponente | Technologie | Lizenz |
|---|---|---|
| Backend | Python · FastAPI · SQLModel | MIT |
| Frontend | Jinja2 · HTMX · Tailwind CSS | MIT |
| Code-Editor | CodeMirror 6 | MIT |
| Markdown + LaTeX | marked · KaTeX | MIT |
| Slides | reveal.js | MIT |
| Terminal (Workspace) | xterm.js | MIT |
| Visualisierung | Plotly · Chart.js · Three.js | MIT |
| Syntax-Highlighting | highlight.js | BSD-3 |
| Drag-and-Drop | SortableJS | MIT |
| Datenbank | SQLite (→ PostgreSQL) | Public Domain |
| Auth | PyJWT (HS256) · SHA-256 · LDAP (ldap3) | MIT |
| LLM-Client | openai SDK (OpenAI-kompatibel) | Apache-2.0 |
| Compute | Docker (Container pro Student, GPU-Passthrough) | Apache-2.0 |
| Excel-Export | openpyxl | MIT |

## 📖 Dokumentation

| Dokument | Inhalt |
|---|---|
| [docs/installation.md](docs/installation.md) | Ausgeführte Installation: Docker, nginx/HTTPS, Compute-Agent, Multi-Server, SSH-Tunnel, GPU, Backups |
| [docs/configuration.md](docs/configuration.md) | Alle Konfigurationsoptionen (`.env`), LLM-Setups, LDAP, Preview-Subdomains |
| [docs/user-guide.md](docs/user-guide.md) | Rollen, Aufgabentypen, Kursmaterial, Medien, Applets, Import, Forum — für Professoren & Tutoren |
| [docs/architecture.md](docs/architecture.md) | Projektstruktur, Komponenten, Debugging & Logs |

**API-Dokumentation** (automatisch generiert, während der App läuft):

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

## 📄 License

MIT — 100 % Open-Source, freier Einsatz an Universitäten.

---

*Entwickelt für den akademischen Einsatz — keine kommerzielle Lizenz erforderlich.*
