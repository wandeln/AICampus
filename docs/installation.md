# AICampus — Installation (ausführlich)

Alle Installations- und Betriebsarten im Detail: Docker (eine Maschine),
Produktiv-Betrieb mit nginx + HTTPS, Compute-Agent (lokal/remote,
Multi-Server), SSH-Tunnel, GPU, Backups.

> **Schnellstart** (Docker, eine Maschine) steht in der [README](../README.md).
> Diese Doku deckt den Produktiv-Betrieb und die restlichen Fälle ab.

## Überblick: Betriebsarten

| Modus | Wo läuft was | Für was |
|---|---|---|
| **A. Docker** | AICampus + Agent in einer `docker compose` auf EINER Maschine | Standard: MVP, kleine Kurse, kleine Aufgaben |
| **B. Multi-Server** | AICampus + lokaler Agent wie A; weitere Agenten auf Compute-Servern (als Docker-Container), verbunden per SSH-Tunnel | GPU-Trainings, Skalierung |

Der Compute-Agent spricht immer dieselbe API (`/health`, `/workspaces`,
`/assets`, `/tasks/{course}/{task}/init-build`, …). AICampus kennt die
Compute-Engines über die **Engine-Registry** in der Admin-Konsole
(Systemeinstellungen → Compute/Workspace) — dort stehen Name, URL, Key und
der GPU-Zugang (welche der von der Engine gemeldeten GPUs erlaubt sind,
per Checkbox; Default: alle). Alle Container einer Engine starten
automatisch mit den erlaubten GPUs. **Routing:** erste gesunde Engine des
Engine-Pools der Aufgabe (lokal bevorzugt); keine gesunde Engine → klarer
Fehler, kein stilles Fallback.

Workspace-Aufgaben sind **immer verfügbar** (kein Feature-Flag) — ohne
erreichbare Engine degradieren die Views sauber.

---

## 1. Voraussetzungen

- Linux (Debian/Ubuntu empfohlen) mit **Docker + Docker Compose v2**.
- Ein **OpenAI-kompatibles LLM-Endpoint** ([Konfiguration](configuration.md#llm)).
- Für Workspace-Aufgaben: Docker auf dem Host des Agents (bei Modus A ist
  das derselbe Host); für GPU: `nvidia-container-toolkit` auf dem Host.
- (Optional) LDAP-Server für Uni-Accounts.

## 2. Docker-Installation (AICampus + Agent, eine Maschine)

```bash
# Voraussetzungen: Docker, .env im Repo-Root
cd AICampus
export COMPUTE_AGENT_KEY=<identisch mit COMPUTE_AGENT_KEY in der .env>
docker compose -f deploy/compose.local.yml up --build -d

docker compose -f deploy/compose.local.yml logs -f aicampus        # Web-App
docker compose -f deploy/compose.local.yml logs -f compute-agent   # Agent
```

Wichtige Punkte:

- AICampus: `http://<host>:8000` (Port in `compose.local.yml` änderbar;
  Default bindet auf `127.0.0.1` — für LAN-Zugriff ohne TLS auf
  `"8000:8000"` ändern, Produktiv-Betrieb s. Abschnitt 3).
- Agent: nur im Compose-Netz (`http://compute-agent:8700`) + optional
  `127.0.0.1:8700` auf dem Host. In der Engine-Registry für den
  AICampus-Container `http://compute-agent:8700` eintragen (die
  `.env`-Fallback-URL `http://127.0.0.1:8700` ist **im
  Container-Modus nicht erreichbar**).
- `AGENT_KEY` wird per **Shell-Export oder `deploy/.env`** übergeben —
  Compose-Interpolation liest `env_file` **nicht**.
- Der Agent verwaltet den **Host-Docker-Daemon** (docker.sock-Mount):
  Workspace-Container, Volumes und Images leben auf dem Host.
- `data/` wird als Volume gemountet (`/app/data`) — DB, Uploads,
  Workspaces, Submissions.
- **GPU:** `nvidia-container-toolkit` auf dem Host installieren. Der Agent
  meldet die verfügbaren GPUs per Health (`gpus: [0, 1, …]`); der
  GPU-Zugang wird je Engine in der Engine-UI gewählt (Checkboxen;
  Default alle, „keine" = nur CPU).

### Entwicklungsmodus (Live-Code + Auto-Reload)

`deploy/compose.dev.yml` ist ein Overlay, das das Repo live in den
AICampus-Container bindet und uvicorn mit `--reload` startet:

```bash
docker compose -f deploy/compose.local.yml -f deploy/compose.dev.yml up -d aicampus
```

- Python-Änderungen → App startet automatisch neu (watchfiles);
  Templates/CSS/JS greifen sofort, ohne Rebuild oder Neustart
  (`AICAMPUS_DEV=1` lässt das statische `?v=` aus der Mtime kommen).
- `--build` fällt komplett weg — nur nach `requirements.txt`-Änderungen
  oder beim allerersten Setup: `… up -d --build aicampus`.
- `compute-agent` bleibt unberührt (Agent-Änderungen wie üblich mit
  `--build compute-agent`).
- Zurück zum Normalbetrieb: `docker compose -f deploy/compose.local.yml up -d aicampus`
  (der Container wird ohne Overlay neu angelegt).

### Dev-Start ohne Docker (optional)

Nur für Entwicklung ohne Docker (z. B. macOS/Windows):

```bash
cd AICampus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # LLM-Endpoint, Secrets setzen
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Ohne Compute-Agent degradieren Workspace-Aufgaben sauber (s. README);
Text- und Multiple-Choice-Aufgaben sind voll funktionsfähig.

## 3. Produktiv-Betrieb: nginx + HTTPS (vor dem Container)

Für den Produktiv-Betrieb steht **nginx auf dem Host** vor dem
AICampus-Container (`127.0.0.1:8000`). nginx übernimmt dabei:

- **TLS-Terminierung** (Let's Encrypt via certbot) + HTTP→HTTPS-Redirect
- **`/static/` direkt von nginx** — performanter als uvicorn; die App
  hängt einen Content-Hash (`?v=`) an die Asset-URLs, daher ist
  1-Jahre-Cache sicher (bei Änderung ändert sich der Hash → Browser
  holt automatisch die neue Version)
- **`/media/` + `/avatars/` direkt von nginx** (UUID-Dateinamen) —
  umgeht den Python/DB-Weg; ohne nginx würden dutzende Bild-Requests
  pro Medien-Suche den App-Connection-Pool fluten
- **Preview-Subdomains** (`*.DOMAIN`) für die Workspace-Web-UIs:
  bewusst ohne `X-Frame-Options` (iframe), `proxy_buffering off` +
  lange Timeouts (SSE/WebSockets)

### 3.1 Site einrichten

Das Template liegt in [`deploy/nginx.example.conf`](../deploy/nginx.example.conf)
— dort die Platzhalter `DOMAIN` und `REPO_PATH` ersetzen. `REPO_PATH` ist
der Pfad zum AICampus-Repo **auf dem Host** (das Verzeichnis mit
`static/` und `data/`) — nginx serviert `static/` aus dem Repo, nicht aus
dem Image, daher bei Updates per `git pull` automatisch aktuell.

```bash
sudo cp deploy/nginx.example.conf /etc/nginx/sites-available/aicampus
sudo nano /etc/nginx/sites-available/aicampus   # DOMAIN + REPO_PATH ersetzen
sudo ln -s /etc/nginx/sites-available/aicampus /etc/nginx/sites-enabled/
sudo nginx -t
```

### 3.2 Zertifikat ausstellen

**Ohne Preview-Subdomains** (einfachste Variante, HTTP-01 — certbot
legt die Challenge automatisch über die nginx-Site ab):

```bash
sudo certbot --nginx -d aicampus.uni.example.edu
```

**Mit Preview-Subdomains** ist ein **Wildcard-Zertifikat** nötig
(`*.DOMAIN`) — das geht nur via **DNS-01**, s. Abschnitt 3.3.

Danach — egal welcher Weg:

```bash
# Zertifikats-Pfad prüfen (certbot zeigt ihn an; weicht er von
# /etc/letsencrypt/live/DOMAIN/ ab, in der Site anpassen) und:
sudo nginx -s reload
```

Certbot erneuert Zertifikate automatisch (systemd-Timer
`certbot.timer`; `systemctl list-timers | grep certbot` prüfen) —
**außer** sie wurden per `--manual` ausgestellt, s. 3.3.

### 3.3 Preview-Subdomains: Wildcard-DNS + Zertifikat via DNS-01

**Was das ist:** Jede Workspace-Web-UI (Jupyter, Studenten-Web-Apps,
noVNC) kann auf einer eigenen Subdomain laufen:
`https://<task>-<port>-<user>-<h6>.<Basis-Domain>/`. Das Label enthält
einen aus dem `SECRET_KEY` abgeleiteten 6-Zeichen-Tag (`<h6>`) —
beliebige Subdomains sind dadurch nicht erratbar. Die Auth bleibt der
access_token-Cookie (er wird per 60-s-Einmal-Ticket auf die Subdomain
gesetzt). Aktiviert wird die Funktion mit `PREVIEW_BASE_DOMAIN` im
`.env` (Details: [configuration.md](configuration.md#server--datenbank));
ohne sie laufen die Previews same-origin über `/preview/…`, und ihr
braucht **kein** Wildcard-Zertifikat — dieser Abschnitt betrifft nur
Betrieb mit Preview-Subdomains.

**Warum Wildcard:** Das Label ist pro Aufgabe/Port/Nutzer dynamisch —
ein eigenes Zertifikat pro Preview-App ist unmöglich (Dynamik +
Let's-Encrypt-Rate-Limits). Ein einzelnes `*.DOMAIN`-Zertifikat deckt
alle ab. Achtung: `*.DOMAIN` deckt die Basis-Domain **selbst nicht** ab
→ das Zertifikat braucht beide Namen (`-d DOMAIN -d *.DOMAIN`).

**Warum DNS-01:** Let's Encrypt stellt Wildcard-Zertifikate nur über
die **DNS-01-Challenge** aus — eine HTTP-Challenge kann die Kontrolle
über *alle* Subdomains nicht beweisen. DNS-01 bedeutet: certbot lässt
einen TXT-Record `_acme-challenge.DOMAIN` mit vorgegebenem Wert in
eurer DNS-Zone anlegen (für `DOMAIN` und `*.DOMAIN` ist es derselbe
Record).

**Voraussetzung (DNS-Zone):**

```dns
# Wildcard-Record, damit die Preview-Subdomains auf den Server auflösen:
*.aicampus.uni.example.edu.   IN   A   <Server-IP>
```

**Variante A (empfohlen): DNS-Plugin — automatische Erneuerung.**
certbot setzt den TXT-Record selbst über eine DNS-API. Für eigenen
BIND/PowerDNS (typisch Uni) ist das `dns-rfc2136`-Plugin (DNS-Update
mit TSIG-Key):

```bash
sudo apt install python3-certbot-dns-rfc2136   # oder: pip install certbot-dns-rfc2136
```

```ini
# /etc/letsencrypt/dns-rfc2136.ini
[main]
authenticator = dns-rfc2136
dns_rfc2136_server = <DNS-Server-IP>:53
dns_rfc2136_tcp = true
dns_rfc2136_reg_type = TSIG
dns_rfc2136_tsig_keyname = aicampus
dns_rfc2136_tsig_algorithm = hmac-sha256
dns_rfc2136_tsig_key = <TSIG-Key>
```

```bash
sudo certbot certonly --config /etc/letsencrypt/dns-rfc2136.ini \
    -d aicampus.uni.example.edu -d *.aicampus.uni.example.edu
```

Der TSIG-Key darf im DNS (BIND: `update-policy` in der Zone) idealer
Weise nur `_acme-challenge.DOMAIN` aktualisieren. Bei Provider-DNS
(Delegierung) gibt es äquivalente Plugins (`dns-cloudflare`,
`dns-google`, `dns-ovh`, …) — gleiche Vorgehensweise. `certbot renew`
(Timer) erneuert dann alles ohne Zutun.

**Variante B: manuell — ohne automatische Erneuerung.**
Ohne DNS-API: certbot fragt interaktiv den TXT-Wert ab, ihr legt den
Record selbst in der Zone an und bestätigt:

```bash
sudo certbot certonly --manual --preferred-challenges dns \
    -d aicampus.uni.example.edu -d *.aicampus.uni.example.edu
```

⚠️ Ein so ausgestelltes Zertifikat wird **nicht automatisch
erneuert** — alle ~2 Monate muss der Befehl erneut manuell laufen
(alternativ: auf Variante A umsteigen).

## 4. Modus B — Multi-Server (Compute-Server + SSH-Tunnel)

### 4.1 Compute-Server vorbereiten (einmalig pro Server)

Für reine Compute-Server ohne AICampus-Installation gibt es
`deploy/compose.compute-only.yml` — der Agent läuft als Container und
verwaltet dabei den **Host-Docker-Daemon** (docker.sock-Mount):

```bash
# Auf dem Compute-Server (beliebiges Linux):
# 1) Docker installieren (GPU: + nvidia-container-toolkit)
# 2) Repo per Git holen (sparse — nur der Agent-Code, kein Backend):
git clone --filter=blob:none --no-checkout https://github.com/wandeln/AICampus.git
cd AICampus && git sparse-checkout set compute_agent deploy && git checkout main
# 3) Key hinterlegen — exakt derselbe Key, der in der Engine-Registry
#    des AICampus-Servers für diese Engine eingetragen ist:
sudo sh -c 'echo "AGENT_KEY=<Key>" > deploy/.env && chmod 600 deploy/.env'
# 4) Starten:
docker compose -f deploy/compose.compute-only.yml up -d --build
curl -s http://127.0.0.1:8700/health   # ohne Token: 401 = Auth aktiv (gut)
```

**GPU-Server:** Zusätzlich zum nvidia-container-toolkit auf dem Host
(`sudo nvidia-ctk runtime configure --runtime=docker`) mit GPU-Overlay
starten, damit der Agent die GPUs in der Engine-UI meldet (GPU-Checkboxen
+ Label statt „alle/keine"-Fallback):

```bash
docker compose -f deploy/compose.compute-only.yml \
               -f deploy/compose.compute-only.gpu.yml up -d --build
```

Ohne `AGENT_KEY` verweigert der Agent-Container den Start (bewusste
Schutzsperre). GPU-/Queue-Parameter (`GPU_ENABLED`, `GPU_MAX_JOBS`, …)
können in `deploy/.env` gesetzt oder direkt in der Compose-Datei
angepasst werden.

**Update nach Code-Änderungen:** `bash deploy/compute-agent-update.sh`
— das Skript holt neue Commits und baut das Agent-Image nur neu, wenn
sich `compute_agent/` tatsächlich geändert hat (Backend-Updates bleiben
wirkungslos). Der Agent meldet seine Version (Content-Hash des
Agent-Quellcodes) über `/health`; die Engine-UI (Admin-Konsole,
Kurs-Settings, Task-Editor) zeigt pro Engine einen grünen
Versions-Badge, solange der Agent aktuell ist, sonst eine gelbe
„veraltet“-Warnung.

Der Agent bindet auf `127.0.0.1:8700` — das ist das Ziel des
SSH-Tunnels in 4.2.

### 4.2 SSH-Tunnel auf dem AICampus-Server

```bash
# Key + Publikey auf dem Compute-Server (User aicampus)
sudo ssh-keygen -t ed25519 -f /etc/aicampus/compute_ssh_key -N ""
sudo ssh-copy-id -i /etc/aicampus/compute_ssh_key.pub aicampus@compute-server-1

# Unit: COMPUTE_HOST ersetzen, ggf. lokalen Port (8701) bei mehreren Servern
sudo cp deploy/aicampus-compute-tunnel.service /etc/systemd/system/
sudo nano /etc/systemd/system/aicampus-compute-tunnel.service   # COMPUTE_HOST → Hostname
sudo systemctl daemon-reload && sudo systemctl enable --now aicampus-compute-tunnel

# Test: Agent hinter dem Tunnel
curl -s http://127.0.0.1:8701/health
```

Mehrere Compute-Server: eine Tunnel-Unit pro Server (lokale Ports 8701,
8702, …).

**Wichtig bei Docker-Deployment** (AICampus läuft in einem Container,
z. B. WSL): Der Container erreicht den Tunnel nicht über `127.0.0.1` —
den Tunnel stattdessen an `0.0.0.0:8701` binden (Unit anpassen) und bei
aktivem UFW die Docker-Subnetze freigeben (wie für den LLM-Tunnel 8001
in Abschnitt 5):

```bash
sudo ufw allow from 172.17.0.0/16 to any port 8701 proto tcp
sudo ufw allow from 172.18.0.0/16 to any port 8701 proto tcp
```

Die Registry-URL für diese Engine lautet dann
`http://host.docker.internal:8701` (zusätzlich `extra_hosts:
host.docker.internal:host-gateway` in der Compose-Datei, steht bereits
in `deploy/compose.local.yml`).

### 4.3 Registry in der Admin-Konsole

Admin → Systemeinstellungen → **Compute/Workspace**:

```
Name: local       URL: http://127.0.0.1:8700   [🧪 Test]
Name: compute-1   URL: http://127.0.0.1:8701   Key: …     [🧪 Test]
Name: compute-2   URL: http://127.0.0.1:8702   Key: …     [🧪 Test]
```

Je Engine wird der GPU-Zugang in der Engine-UI gewählt (Checkboxen für die
von der Engine gemeldeten GPUs; Default alle, „keine" = nur CPU).
Speichern ist automatisch (Auto-Save). Der Statusbereich zeigt die Health
aller Engines (30-s-Cache).

### 4.4 Betriebsprüfung

- **Tunnel down?** Agent im Status rot; Workspace-Aufgaben bleiben sichtbar,
  „Ausführen/Abgeben" ausgegraut („⚠️ Compute-Server nicht erreichbar").
  Der Tunnel stellt sich selbst wieder her (`Restart=always` +
  `ServerAliveInterval`), AICampus braucht nicht neu gestartet zu werden.
- **Agent neugestartet?** Lauffähige Workspaces (Volumes) bleiben erhalten;
  laufende Jobs sind nach dem Neustart „killed" (UI zeigt das an).
- **Image-Spec geändert?** Neuer Content-Hash → neues Tag → beim nächsten
  Installieren werden nur die geänderten Schichten neu gebaut.

## 5. LLM hinter SSH erreichen (LLM-Tunnel-Service)

Wenn der LLM-Server in einem anderen Netz liegt oder per Firewall nicht
direkt erreichbar ist (z. B. vLLM auf einem Uni-GPU-Server, nur SSH
offen), kann man einen SSH-Local-Port-Forward als **systemd-Service**
einrichten — reboot-fest mit Auto-Restart:

```bash
# 1) SSH-Key erzeugen (falls noch nicht vorhanden) und Public-Key auf den LLM-Server kopieren
ssh-keygen -t ed25519 -C "aicampus-llm-tunnel"
ssh-copy-id user@llm-server   # alternativ: Public-Key manuell nach ~/.ssh/authorized_keys

# 2) systemd-Service anlegen (er setzt Key-Auth voraus, kein Passwort-Prompt)
sudo tee /etc/systemd/system/aicampus-llm-tunnel.service > /dev/null << 'EOF'
[Unit]
Description=AICampus LLM SSH-Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wandel
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 0.0.0.0:8001:localhost:8001 user@llm-server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 3) Aktivieren
sudo systemctl daemon-reload
sudo systemctl enable --now aicampus-llm-tunnel

# 4) Prüfen: Port muss lauschen
ss -tln | grep 8001
```

Dann im `.env` einfach den lokalen Tunnel-Port als Endpoint angeben:

```bash
LLM_API_URL=http://localhost:8001/v1
```

**Wichtig bei Docker-Deployment** (AICampus läuft in einem Container):

1. Der Container erreicht den Host nicht über `localhost`, sondern über
   die Docker-Host-IP. Am einfachsten per `extra_hosts` in der
   Compose-Datei (steht bereits in `deploy/compose.local.yml`):
   ```yaml
   services:
     aicampus:
       extra_hosts: ["host.docker.internal:host-gateway"]
   ```
   und `LLM_API_URL=http://host.docker.internal:8001/v1` im `.env`.
   **Wichtig:** Der Tunnel muss an die **Bridge-IP** binden
   (`-L 172.18.0.1:8001:localhost:8001`), damit er aus dem Compose-Netz
   erreichbar ist (Standard-Netz `172.18.0.0/16`, in
   `docker network inspect` prüfen).
2. Falls auf dem Host **UFW** aktiv ist: Docker-Container-Traffic auf
   nicht-published Ports wird per Default gedroppt. Freigabe für die
   Docker-Subnetze:
   ```bash
   sudo ufw allow from 172.17.0.0/16 to any port 8001 proto tcp
   sudo ufw allow from 172.18.0.0/16 to any port 8001 proto tcp
   ```

**Fehlersuche:** `journalctl -u aicampus-llm-tunnel -n 50` — ein
`Permission denied (publickey)`-Fehler deutet auf ein Key-Problem (z. B.
überschriebene `authorized_keys` auf dem LLM-Server), sonst liegt es am
SSH-Server/Netz. In der AICampus-Admin-Konsole lässt sich die Verbindung
jederzeit per „LLM testen" prüfen.

## 6. Backups & Datenorte

| Daten | Ort |
|---|---|
| DB + Uploads + Task-Dateien + Snapshots | `<repo>/data/` (Volume) |
| Assets (Daten, Tests, Task-Skripte) | immer `<repo>/data/compute-assets/` (lokal und remote compute-only — Bind-Mount, da der Docker-Daemon Host-Pfade braucht) |
| Task-Images (init.sh-Builds) | Docker-Images auf dem Host des jeweiligen Agents |
| Student-Volumes | Docker-Volumes `aicampus-ws-*` auf dem Host des jeweiligen Agents |

> **Backup-Praxis:** `data/` sichern (konsistent stoppen oder
> `sqlite3 data/aicampus.db ".backup …"`); Docker-Volumes/Images der
> laufenden Workspaces zusätzlich, falls Fortschritt erhalten bleiben soll.

## 7. Sicherheit (Kurzfassung)

- Agent bindet nur auf `127.0.0.1` bzw. nur im Compose-Netz (Docker).
  Einziger externer Zugangsweg: SSH-Tunnel mit Key-Auth.
- Jede AICampus→Agent-Request trägt ein HMAC-Op-Token (60 s, scopet auf
  Workspace/Task). Key leer = offen (NUR Entwicklung!).
- Workspace-Container: read-only Root-FS, `--network=none` (Default;
  `internet: true` in der Spec = opt-in Bridge), CPU/RAM/PID-Limits,
  einziger schreibbarer Ort `/workspace`.
- Private Dateien (`.solution/`, `.tests/`) liegen nur auf der
  AICampus-Disk und werden ausschließlich beim Grading in einen frischen
  Container injiziert — nie ins Student-Volume.
- Medien/Avatare werden in Produktion direkt von nginx gesendet
  (Abschnitt 3) — die „Verstecktheit" kommt über die unguessable
  UUID-Dateinamen.
- **In Produktion:** `SECRET_KEY` setzen (JWT), Admin-Passwort ändern,
  `DEBUG=false`, nginx mit TLS davor (Abschnitt 3).
