# deploy/ — Installations- & Betriebsdateien

Alle Installations- und Betriebsarten sind ausführlich in
[docs/installation.md](../docs/installation.md) dokumentiert; der
**Schnellstart** (Docker, eine Maschine) steht in der
[README](../README.md).

| Datei | Zweck |
|---|---|
| `compose.local.yml` | AICampus + Compute-Agent in einer Compose-Stack (eine Maschine) |
| `compose.dev.yml` | Overlay: Live-Code + Auto-Reload (Entwicklungsmodus) |
| `compose.compute-only.yml` | Nur Compute-Agent (reine Compute-Server) |
| `nginx.example.conf` | nginx-Produktivtemplate auf dem Host (TLS, static/media direkt vor dem Container) |
| `aicampus-compute-tunnel.service` | systemd-Unit: SSH-Tunnel zu einem Remote-Compute-Server |
