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
| `aicampus-llm-tunnel.service` | systemd-Unit: LLM-Tunnel 8001 → venusberg:8001 (qwen3.6-moe) |
| `aicampus-llm-tunnel.venusberg-qwen.service` | systemd-Unit: LLM-Tunnel 8002 → venusberg:8000 (Qwen3.8-27B-FP8) |
| `aicampus-llm-tunnel.vci-gpu1.service` | systemd-Unit: LLM-Tunnel 8003 → vci-gpu1:8003 (qwen3.8-27B) |
