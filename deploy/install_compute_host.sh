#!/usr/bin/env bash
# Einmal-Installation auf einem AICampus-Compute-Server (Ubuntu 22.04/jammy, amd64):
#   Docker Engine + NVIDIA Container Toolkit + Runtime-Konfiguration.
# Sudo-Passwort wird interaktiv vom TTY abgefragt (ssh -tt).
# Aufruf:  ssh -tt -i <key> user@host 'bash -s' < deploy/install_compute_host.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "==> [1/6] Basis-Pakete (ca. 1 Min.)"
sudo apt-get update
sudo apt-get install -y ca-certificates curl gpg

echo "==> [2/6] Docker-APT-Repo (download.docker.com)"
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu jammy stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list

echo "==> [3/6] NVIDIA Container Toolkit APT-Repo"
sudo curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
sudo curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [arch=amd64 signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo "==> [4/6] Pakete installieren (Docker + Toolkit) — kann 3–8 Min. dauern"
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit

echo "==> [5/6] Docker auf NVIDIA-Runtime umstellen + Neustart"
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

echo "==> [6/6] wandeln in docker-Gruppe (greift in neuer Session)"
sudo usermod -aG docker wandeln

echo
echo "=== VERIFICATION (via sudo — aktuelle Session hat die docker-Gruppe noch nicht) ==="
sudo docker --version
sudo docker compose version
echo "Runtimes:"; sudo docker info --format '{{ json .Runtimes }}'
echo "docker-Daemon: $(systemctl is-active docker)"
echo "INSTALL_DONE"
