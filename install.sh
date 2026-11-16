#!/usr/bin/env bash
# Installeert of werkt de Zendure mock bij in een Debian/Ubuntu LXC.
#   curl -fsSL https://raw.githubusercontent.com/cardinal808/zendure_mock_server/main/install.sh | bash
# Andere versie: ... | REF=v2026.11.1 bash
set -euo pipefail

REF="${REF:-main}"
BASE="https://raw.githubusercontent.com/cardinal808/zendure_mock_server/${REF}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Start dit script als root." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update && apt-get install -y python3
fi

mkdir -p /opt/zendure-mock
curl -fsSL -o /opt/zendure-mock/zendure_mock.py "${BASE}/zendure_mock.py"

# Service alleen plaatsen als hij nog niet bestaat, zodat eigen opties in ExecStart blijven staan
if [ ! -f /etc/systemd/system/zendure-mock.service ]; then
  curl -fsSL -o /etc/systemd/system/zendure-mock.service "${BASE}/zendure-mock.service"
fi

systemctl daemon-reload
systemctl enable zendure-mock >/dev/null 2>&1
systemctl restart zendure-mock

VERSIE=$(grep -m1 '^VERSION' /opt/zendure-mock/zendure_mock.py | cut -d'"' -f2)
IP=$(hostname -I | awk '{print $1}')
echo "Zendure mock ${VERSIE} draait: http://${IP}:8080/"
