# AICampus — User Guide

Rollen, Kursbeitritt, Aufgabentypen und Kursmaterial im Detail.
Gedacht für Professoren, Tutoren und Administratoren.

---

## 1. Rollen-Modell

Das System unterscheidet **globale Rollen** (Systemebene) und **Kurs-Rollen**
(pro Kurs):

| Globale Rolle | Beschreibung |
|---|---|
| **Admin** | Admin-Konsole: Kurse & User verwalten, globale LLM/LDAP-/Compute-Einstellungen anpassen |
| **User** | Standard-Rolle — Berechtigungen werden über die Kurs-Rolle bestimmt |

| Kurs-Rolle | Kann |
|---|---|
| **Prof** | Kurs bearbeiten (Name, Semester, Beschreibung), Mitglieder verwalten (hinzufügen, Rollen ändern, entfernen), Einladungslinks erstellen, Aufgaben erstellen/bearbeiten/löschen, Sichtbarkeit umschalten, Aufgaben per Drag-and-Drop ordnen, Einreichungen korrigieren, Feedback überschreiben, Übersichtstabelle + Excel-Export, Skript-Kapitel verwalten (anlegen, LLM-bearbeiten, freischalten, ordnen, löschen), Slide-Decks anlegen & löschen, Applet-Studio, Quellen-Bibliothek, Medienbibliothek, Kurs-Import, Kurs-Engines verwalten |
| **Tutor** | Aufgaben erstellen/bearbeiten (LLM-Aufgabe generieren), Einreichungen korrigieren, Feedback überschreiben, Übersichtstabelle + Excel-Export, Skript-Kapitel anlegen/LLM-bearbeiten/freischalten/ordnen, Slide-Decks anlegen & bearbeiten, Kurs-Forum moderieren |
| **Student** | Aufgaben sehen & lösen, sofortiges LLM-Feedback + Hints erhalten, eigene Punkte einsehen, Workspace-Umgebung nutzen (Mini-IDE), vorherige/nächste Aufgabe navigieren, freigeschaltene Skript-Kapitel & Slides lesen, Kurs-Forum nutzen, Name & Passwort selbst ändern |

> **Hinweis:** Ein globaler Admin hat uneingeschränkten Zugriff auf alle Kurse,
> auch ohne Kurs-Mitgliedschaft. Ein Prof kann alle Kurs-Rollen zuweisen —
> nur die Ernennung zu Prof darf der Admin.

## 2. Kurs-Beitritt

- **Einladungslinks:** Prof/Admin generiert einen Token mit Gültigkeitsdauer
  und optionaler Nutzungsgrenze. Copy-to-Clipboard der vollständigen
  Join-URL.
- **Manuelle Einladung:** Prof/Admin sucht den User und fügt ihn direkt zum
  Kurs hinzu (mit Rollenauswahl).
- **Join-Seite:** User gibt Token ein (via Link) und tritt dem Kurs bei.

## 3. Aufgabentypen

### 3.1 Textaufgaben

Freier Text mit **Markdown & LaTeX**-Rendering. Korrektur durch das LLM mit
konstruktivem Feedback; das Feedback kann vom Tutor/Prof überschrieben
werden.

### 3.2 Workspace-Aufgaben

Eigene, **isolierte Docker-Container pro Student** auf einem
Compute-Agent-Server (lokal oder remote, GPU optional).

- **Mini-IDE im Browser:** Datei-Tree, Code-Editor, **Terminal** (xterm.js),
  **Web-Preview** (jede Web-App, die der Student im Container startet, ist
  per Port-Liste anklickbar) — Ansichten umbrechen per View-Schalter.
- **Aufgaben-Struktur:** Die Aufgabe besteht aus Dateien (Starter-Dateien,
  `run.sh`, `test.sh`) + Umgebungsfeldern (Timeout/CPU/RAM/Internet/
  Hauptdatei). Private Dateien (Musterlösung, versteckte Tests) liegen nur
  auf dem AICampus-Server und werden **ausschließlich beim Grading** in einen
  frischen Container injiziert — nie ins Student-Volume.
- **Zugriffs-Klassen:** Dateien/Ordner sind 🔒 read-only (für alle), 👤
  privat (Agent-Server-only) oder ✏️ editierbar.
- **Abgabe:** Snapshot des Containers (tar.gz) — der Tutor sieht die gesamte
  Umgebung, Laufzeit-Logs und Test-Ergebnisse.
- **Lokal mitnehmen:** Studierende können die aktuelle Umgebung inkl.
  Dateien als **docker-Paket** herunterladen und lokal ausführen.
- **Image-Specs:** Jede Aufgabe kann ein eigenes Docker-Image (z. B.
  Python-ML, C++, Webtop) verwenden — bestehende Specs aus der
  Admin-/Kurs-Registry wählen oder per LLM vorschlagen lassen
  (UI bestätigt). Engines + GPU-Zugang werden je Aufgabe gewählt.
- **Degradierter Betrieb:** Ohne erreichbare Compute-Engine bleiben die
  Aufgaben sichtbar, „Ausführen/Abgeben" ist ausgegraut
  („⚠️ Compute-Server nicht erreichbar").

### 3.3 Test- & Verifikations-Skripte (Workspace)

Die Verifikation läuft über Shell-Skripte mit festen Namen in der **Wurzel**
des Aufgaben-Dateibaums (Stubs werden beim Aufgaben-Save automatisch
angelegt — einfach editieren):

| Skript | Zugang | Läuft wann |
|---|---|---|
| `run.sh` | 🔒 | Button „▶ Ausführen" (Student-Container) |
| `test.sh` | 🔒 (**Student sieht sie!**) | Button „🧪 Test" (Student) + beim Grading |
| `.test_private.sh` | 👤 (Student sieht es nie) | nur beim Grading, nach `test.sh` |
| `.test_solution.sh` | 👤 | Button „🧪 Musterlösung testen" (Tutor): legt `.solution/` über die editierbaren Dateien und prüft sie gegen die Test-Skripte |

**Grading-Flow:** Einweg-Container (Student-Snapshot + 👤-Dateien,
`cwd=/workspace`) → `bash test.sh` → `bash .test_private.sh` (falls
hinterlegt). **Beide Outputs (stdout/stderr) fließen in den
LLM-Grading-Prompt** und sind in der Tutor-Review als Lauf-Historie
sichtbar. Der Exit-Code entscheidet: `0` = bestanden. Ohne Test-Skripte
korrigiert das LLM ohne Test-Ausgaben. Beide Skripte werden beim Grading
immer ausgeführt (das zweite wird nicht übersprungen, wenn das erste
fehlschlägt — so kommen alle Fehler ans Licht).

**Server-Probing-Muster** (Dienste starten im Container nicht
automatisch): das Skript startet den Dienst selbst, prüft per Probing,
killt dann wieder — deterministisch und idempotent, unabhängig davon,
was der Student zuletzt in seinem Workspace gemacht hat:

```bash
#!/bin/bash
# .test_private.sh — Grading-Container, cwd=/workspace
set -u
PORT=8080
FAIL=0

# 1) Studenten-Konfiguration übernehmen (falls existiert)
[ -f nginx.conf ] && cp nginx.conf /etc/nginx/nginx.conf
if [ -f site.conf ]; then
  mkdir -p /etc/nginx/sites-enabled
  cp site.conf /etc/nginx/sites-enabled/default
fi
[ -d html ] && cp -r html/* /var/www/html/ 2>/dev/null

# 2) Dienst starten
service nginx start
sleep 1

# 3) Probing
curl -sf "http://127.0.0.1:${PORT}/" -o /tmp/page.html \
  && grep -qi "welcome\|<h1" /tmp/page.html \
  && echo "OK: HTTP-Antwort mit plausiblem Inhalt" \
  || { echo "FEHLER: nginx antwortet nicht korrekt auf Port ${PORT}"; FAIL=1; }

# 4) Aufräumen
service nginx stop >/dev/null 2>&1
exit $FAIL
```

**C++** (Build + Ausgabeprobes; Multi-File über Makefile wird erkannt):

```bash
#!/bin/bash
set -u
if [ -f Makefile ]; then make -s solution; else g++ -O2 -std=c++17 -o solution main.cpp; fi \
  || { echo "FEHLER: Build fehlgeschlagen"; exit 1; }
./solution > /tmp/out.txt 2>&1 || { echo "FEHLER: Exit-Code $?"; exit 1; }
grep -q "expected-output-1" /tmp/out.txt || { echo "FEHLER: Zeile 1 fehlt"; exit 1; }
echo "OK"
```

**Assembler:**

```bash
#!/bin/bash
set -u
nasm -f elf64 main.asm -o main.o || { echo "FEHLER: NASM"; exit 1; }
ld -o solution main.o || { echo "FEHLER: Linker"; exit 1; }
./solution > /tmp/out.txt 2>&1 || { echo "FEHLER: Laufzeit"; exit 1; }
grep -q "42" /tmp/out.txt && echo "OK"
```

**Python/ML:** Tests z. B. per `python -m pytest tests/ -v` in `test.sh`
— Ordner-/Dateinamen sind frei, die Zugriffsklasse entscheidet
(private Testdaten als 👤-Dateien/Ordner, nie studentensichtbar).

**Best Practices:**

- `set -u` ja, `set -e` **nein** — alle Probes laufen lassen und den
  kumulierten Status melden (mehr Signal für LLM + Tutor).
- Probing gegen `127.0.0.1` (der Container hat sein eigenes
  Loopback-Netz; Tasks ohne Internet testen nur lokale Ports). Bei
  `internet: true` dürfen Probes auch externe Endpunkte prüfen.
- Immer stoppen/cleanen am Ende (der Grading-Container wird danach
  gelöscht, aber saubere Logs helfen beim Debugging).
- Die Ausgabe ist das wichtigste Signal: klare „OK: …" / „FEHLER: …"-
  Zeilen fließen direkt ins LLM-Feedback der Studenten.
- `test.sh` ist **studentenlesbar** (🔒) — nur öffentliche Teile
  reinsetzen (Teilmenge, vereinfachte Erwartungen); der maßgebliche
  Judge ist `.test_private.sh`.

## 4. Kurs-Material

Registerkarten je Kurs: **Skript, Slides, Aufgaben, Übersicht (Tutor+),
Medien (Prof+), Quellen (Tutor+), Forum (alle), Mitglieder (Prof+),
Einstellungen (Prof+)**.

### 4.1 Vorlesungsskript

- Besteht aus mehreren **Markdown-Kapiteln** (LaTeX, Mermaid-Diagramme).
- Jedes Kapitel einzeln **per LLM anpassbar** (Titel/Inhalt generieren oder
  überarbeiten lassen), für Studenten **einzeln freischaltbar** (Auge-Icon),
  per **Drag-and-Drop** ordnbar.
- Studenten sehen nur die freigeschalteten Kapitel in der Reihenfolge
  (Lesefluss).
- **Quellenzitate:** per `@cite:{key}` / `@citet:{key}` / `@citep:{key}` im
  Markdown — Auflösung, Nummerierung (kursweit stabil) und das
  Quellenverzeichnis rendert der Client-Renderer.

### 4.2 Vorlesungs-Slides

- **Slide-Decks** je Kurs: ein Markdown-Dokument pro Deck, Folien durch
  `---` getrennt, gerendert mit **reveal.js**.
- **Präsentationsmodus** + **PDF-Export**.
- LLM-gestützt: Deck aus Thema/Vorlage generieren, Folien überarbeiten.
- Für Studenten einzeln freischaltbar.

### 4.3 Medienbibliothek

- `data/media/course_{id}/` — Bilder (PNG/JPG/WebP/GIF, max. 5 MB,
  UUID-Namen); Upload startet automatisch bei Dateiauswahl (Vorschau +
  Progress).
- Das **Vision-LLM erzeugt Titel & Beschreibung** automatisch
  (Vision-Modell erforderlich).
- Datei kann später **ersetzt** werden (gleicher Pfad → Markdown-Referenzen
  bleiben gültig).
- **Einbindung:** `![Titel](/media/{course_id}/{datei})` — nur über
  authentifizierte Route (Kurs-Memberschaft erforderlich).
- **Verwendungs-Tracking:** Einbettungen (Skript/Slides/Aufgabe) werden
  automatisch abgeleitet (`media_usages`), doppelte Referenzen werden
  markiert.

### 4.4 Applet-Studio (Prof/Admin)

- **Interaktive HTML-Applets** für Kursmaterial generieren — per
  Beschreibung oder **mit Referenzbild** (Medium aus der Medienbibliothek
  oder hochgeladenes Foto/Zeichnung, z. B. eine Skizze aus einem Paper).
- Applets können interaktiv sein oder einfache statische Figuren (z. B. SVG).
- Manuelle Nachbearbeitung im CodeMirror-Editor; Applets sind Medien des
  Kurses und damit im Markdown einbindbar.

### 4.5 Quellen-Bibliothek (Prof/Tutor)

- **BibTeX-artige Bibliothek** je Kurs: Quellen anlegen, **BibTeX importieren**.
- Zitieren im Kurs-Material (Skript/Slides/Aufgaben) via `@cite:{key}` —
  s. 4.1.
- Beim **Kurs-Import** (4.7) kann das LLM Quellen aus den Quelldokumenten
  **extrahieren**.

## 5. Kurs-Material-Import (Zip)

Für Prof/ Admin: bestehendes Material **als Zip hochladen** (bis 500 MB;
PDF, PowerPoint, Word, Markdown, Bilder, …). Mehrstufiger **LLM-Wizard**:

1. **Dateianalyse/Staging** (mehrfache Zips mergbar)
2. **Medien-Import** (auswählen/skippen, Vorschau)
3. **Quellen-Import** (BibTeX-Dateien + optionale LLM-Quellen-Extraktion)
4. **Skript-Kapitel-Planner** (Kapitelstruktur planen, manuell anpassbar)
5. **Folien-Planner**
6. **Skript-Generierung** (Kapitel für Kapitel)
7. **Slide-Decks**

Der Job läuft asynchron mit **Pause/Resume/Cancel**, parallelen
LLM-Calls (GPU-schonend limitierbar) und Vorschau-Bildern (PDF → PNG).

## 6. Kurs-Forum

- **Kanäle pro Kurs** — alle Rollen (Student/Tutor/Prof) können in allen
  Kanälen lesen und schreiben.
- Kanäle anlegen dürfen alle Mitglieder; **umbenennen/löschen** nur der
  Ersteller oder Tutor/Prof. Nachrichten löschen: eigene für alle,
  beliebige für Tutor/Prof.
- **Ungelesen-Badge** am Forum-Tab; Lese-Status wird synchron gehalten.

## 7. Übersicht & Korrektur (Tutor/Prof)

- **Punktübersichtstabelle** je Kurs: alle Students × Aufgaben.
- **Korrektur-Workflow:** einzelne Einreichung öffnen, LLM-Feedback
  einsehen, **Feedback überschreiben**/nachbearbeiten.
- **Excel-Export** (.xlsx) des Punktestands (Blattname konfigurierbar).

## 8. Studentensicht

- Dashboard: eigene Kurse, **Punktestand** (erreicht/möglich, %).
- Kurs-Start leitet automatisch weiter: Skript (wenn sichtbar) → Aufgaben →
  Forum.
- Aufgabe lösen: Editor (je Typ), sofortiges Feedback nach Abgabe,
  **sokratische Hints** (einzeln anforderbar, konfigurierbar pro Aufgabe),
  Versuchszähler + Limit, Abgabefrist, Historie mit den eigenen
  Eingereichten.
- „Meine Einstellungen": Name & Passwort ändern (LDAP-Accounts: Name
  hier, Passwort über LDAP).

## 9. Sicherheit

- **Workspace-Container:** read-only Root-FS, `--network=none` (Default;
  Internet opt-in je Aufgabe), CPU/RAM/PID-Limits, einziger schreibbarer
  Ort `/workspace`.
- **Auth:** JWT (HS256) mit httpOnly-Cookies, SHA-256-Passwort-Hashes,
  optionales LDAP, RBAC (globale + Kurs-Rollen).
- **Compute-Agent:** bindet nur auf `127.0.0.1` bzw. nur im
  Compose-Netz; jede AICampus→Agent-Request trägt ein HMAC-Op-Token
  (60 s, scopet auf Workspace/Task). Key leer = offen (NUR Entwicklung!).
- **Medien/Avatare** nur über authentifizierte Routen — kein Static-Mount.
