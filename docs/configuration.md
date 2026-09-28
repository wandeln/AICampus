# AICampus — Konfiguration

Alle Konfigurationsoptionen und ihre Auflösung.

## Priorität (höchste → tiefste)

1. **Kurs-Settings** (Kurs → Einstellungen, Prof+)
2. **Globale Settings** (Admin-Konsole → Systemeinstellungen)
3. **`.env` / Umgebungsvariablen** (diese Doku)
4. **Defaults** (`config.py`, hardcoded Fallback)

Geregelt wird damit: LLM (Endpoint, Modell, Prompt-Overrides), LDAP und
Compute-Engines. Alles Weitere (Server, Sandbox, Import) kommt nur aus
`.env` / Defaults.

> `.env.example` im Repo-Root ist die Vorlage; in Docker-Hybrid-Modus liegt
> die `.env` im **Repo-Root** (wird via `env_file` geladen).

## Server & Datenbank

| Variable | Standard | Beschreibung |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind-Adresse (uvicorn) |
| `PORT` | `8000` | Port |
| `DEBUG` | `true` | Debug-Modus — **in Produktion `false`** |
| `DATABASE_URL` | `sqlite:///<repo>/data/aicampus.db` | SQLAlchemy-URL (SQLite oder PostgreSQL) — absoluter Pfad, im Docker-Hybrid per Compose auf `sqlite:////app/data/aicampus.db` gesetzt |
| `PREVIEW_AGENT_PORT` | `8701` | Port des Agent-Preview-Servers (nur Agent-Netz, kein Host-Publish) — Ziel des Backend-Preview-Proxy |
| `PREVIEW_BASE_DOMAIN` | *(leer = aus)* | Basis-Domain für **Preview-Subdomains** der Workspace-Web-UIs: jede App bekommt `https://<task>-<port>-<user>-<h6>.<domain>/`. Voraussetzungen: Wildcard-DNS + TLS (Wildcard-Zertifikat) + Nginx-Server-Block OHNE `X-Frame-Options`. Leer = Preview läuft im same-origin Proxy (Standard) |

## Auth / JWT

| Variable | Standard | Beschreibung |
|---|---|---|
| `SECRET_KEY` | `dev-secret-change-me-in-production` | **JWT-Signatur-Secret — in Produktion zwingend setzen** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `480` | Token-Laufzeit (8 h) |

## LLM

OpenAI-kompatibler API-Endpoint. Vertrauliche Daten (Korrektur/Grading)
gehen an das **Private**-Endpoint; für nicht-sensitive Aufgaben
(Aufgabengenerierung, Musterlösungen) kann ein zweites **Public**-Endpoint
konfiguriert werden — leer lassen = Private Endpoint wird für alles genutzt.

| Variable | Standard | Beschreibung |
|---|---|---|
| `LLM_API_URL` | `http://localhost:8001/v1` | Private Endpoint (OpenAI-kompatibel) |
| `LLM_API_KEY` | `sk-default` | API-Key (privat) |
| `LLM_MODEL` | `Qwen3-32B` | Modellname (privat) |
| `LLM_TEMPERATURE` | `0.1` | Sampling-Temperatur |
| `LLM_TIMEOUT` | `120` | Request-Timeout (Sekunden) |
| `LLM_API_URL_PUBLIC` | *(leer)* | Public Endpoint — leer = Private Endpoint |
| `LLM_API_KEY_PUBLIC` | *(leer)* | API-Key (public) |
| `LLM_MODEL_PUBLIC` | *(leer)* | Modellname (public) |

### Beispiele

**Qwen3 auf Uni-Server (vLLM):**

```bash
LLM_API_URL=http://llm-server.uni.de:8001/v1
LLM_API_KEY=sk-your-key
LLM_MODEL=Qwen3-32B
```

**Ollama (lokal):**

```bash
LLM_API_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:32b
```

**vLLM / TGI:**

```bash
LLM_API_URL=http://localhost:8000/v1
LLM_API_KEY=
LLM_MODEL=meta-llama/Llama-3.1-70B-Instruct
```

**SSH-Tunnel zu einem nur per SSH erreichbaren LLM-Server** (reboot-fester
systemd-Service, inkl. Docker/UFW-Hinweisen):
[installation.md §6](installation.md#6-llm-hinter-ssh-erreichen-llm-tunnel-service).

### In der UI

- **Globale Settings:** Admin-Konsole → Systemeinstellungen (Endpoint,
  Modell, Key + **Verbindungstest**).
- **Kurs-Override:** Kurs → Einstellungen — erlaubt z. B. ein anderes
  Modell pro Kurs.
- **Custom Grading-Prompt** je Ebene (Default-Prompts liegen in `prompts/`).

## LDAP (optional)

| Variable | Standard | Beschreibung |
|---|---|---|
| `LDAP_ENABLED` | `false` | LDAP-Auth aktivieren |
| `LDAP_SERVER` | `ldap.uni.de` | Server-URL (`ldap://…` / `ldaps://…`) |
| `LDAP_BASE_DN` | `ou=people,dc=uni,dc=de` | Such-Basis |
| `LDAP_BIND_DN` | `cn=admin,dc=uni,dc=de` | Bind-DN (Browse-Zugang) |
| `LDAP_BIND_PW` | *(leer)* | Bind-Passwort |
| `LDAP_USER_SEARCH` | `(uid={username})` | User-Suchfilter — bei Active Directory z. B. `(sAMAccountName={username})` |

Die Admin-Konsole bietet eine vollständige LDAP-Konfiguration mit
Verbindungstest. Bei aktiviertem LDAP werden neue User **automatisch
angelegt**, wenn die LDAP-Auth erfolgreich ist; die Passwortänderung
erfolgt dann über LDAP (der Name bleibt in AICampus änderbar).

## Sandbox / Code-Ausführung

| Variable | Standard | Beschreibung |
|---|---|---|
| `SANDBOX_TIMEOUT` | `15` | Wall-Clock-Timeout (Sekunden) |
| `SANDBOX_CPU_SECONDS` | `30` | CPU-Zeit-Limit (Sekunden) |
| `SANDBOX_MEMORY_MB` | `512` | Memory-Limit (MB) |

Erlaubte Python-Module: Standardbibliothek + `matplotlib` (Liste in
`config.py` → `SANDBOX_ALLOWED_MODULES`, bei Bedarf dort erweitern).

## Compute / Workspaces

| Variable | Standard | Beschreibung |
|---|---|---|
| `COMPUTE_AGENT_URL` | `http://127.0.0.1:8700` | URL der (Fallback-)Compute-Engine |
| `COMPUTE_AGENT_KEY` | *(leer)* | HMAC-Key — **muss `AGENT_KEY` des Agents entsprechen**. Leer = Auth deaktiviert (NUR Entwicklung!) |

> **Engine-Registry:** Mehrere Engines (lokal + remote) werden in der
> Admin-Konsole registriert (Name, URL, Key, GPU-Zugang). Eine explizit
> deklarierte `.env`-Engine erscheint dort als „local"-Engine. Kurse können
eigene Engines definieren (Kurs → Einstellungen), die die globalen
> ergänzen. Details: [installation.md](installation.md#4-modus-b--multi-server-compute-server--ssh-tunnel).

## Frontend

| Variable | Standard | Beschreibung |
|---|---|---|
| `CODEMIRROR_THEME` | `dracula` | CodeMirror-Theme (Code-Editor) |

## Excel-Export

| Variable | Standard | Beschreibung |
|---|---|---|
| `EXCEL_SHEET_NAME` | `Punktestand` | Blattname des Excel-Exports |

## Kurs-Material-Import (Zip)

| Variable | Standard | Beschreibung |
|---|---|---|
| `IMPORT_MAX_ZIP_BYTES` | `500 MB` | Max. Zip-Größe |
| `IMPORT_MAX_SINGLE_FILE_BYTES` | `100 MB` | Max. Größe einer einzelnen Datei |
| `IMPORT_MAX_ENTRIES` | `10000` | Max. Einträge im Zip |
| `IMPORT_MAX_EXTRACTED_BYTES` | `1 GB` | Max. entpacktes Volumen |
| `IMPORT_MIN_FREE_DISK_FACTOR` | `3.0` | Freier Disk-Platz ≥ Faktor × Zip-Größe |
| `IMPORT_LLM_CONCURRENCY` | `2` | Parallele LLM-Calls pro Import (**GPU-Schutz**) |
| `IMPORT_FILE_CONCURRENCY` | `4` | Parallele Dateianalysen |
| `IMPORT_TIMEOUT_ANALYSIS` / `MEDIA` / `PLAN` / `CONVERT` | `180` / `120` / `300` / `600` s | Feature-Timeouts (lokale Modelle können langsam sein) |
| `IMPORT_PNG_DPI` / `IMPORT_PNG_MAX_DIM` | `300` / `4096` | PDF-Figur → PNG (DPI, Dimensions-Cap) |
| `IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1` | `150` | Sicherheits-Cap Quell-Folien (1:1-Modus) |

Die restlichen `IMPORT_*`-Variablen (Chunk-Größen, Planner-Budgets) sind
Feintuning für den LLM-Wizard — Defaults sind praxistauglich.

## Docker-Compose-Spezifika

| Variable (Compose-Env) | Wo | Beschreibung |
|---|---|---|
| `COMPUTE_AGENT_KEY` | Shell-Export oder `deploy/.env` | Wird in den Agent als `AGENT_KEY` interpoliert — Compose liest `env_file` **nicht** für `${…}`-Interpolation |
| `ASSET_ROOT_HOST` | `compose.local.yml` + `compose.compute-only.yml` | Host-Pfad zum Asset-Verzeichnis (wird aus `${PWD}` gebildet — **Compose vom Repo-Root starten**) |
| `GPU_ENABLED` / `GPU_MAX_JOBS` / `IDLE_TIMEOUT` | `compose.local.yml` | Agent-GPU-/Queue-Parameter (Default `auto` / `8` / `1200 s`) |
