#!/usr/bin/env bash
# Instala/atualiza o tb4_agent num TurtleBot4. Correr NO ROBÔ (precisa de sudo):
#   scp -r robot ubuntu@<robô>:/tmp/tb4_agent && ssh -t ubuntu@<robô> 'bash /tmp/tb4_agent/install.sh robot1'
set -euo pipefail
ROBOT_NAME="${1:-$(. /etc/turtlebot4/setup.bash; echo "${ROBOT_NAMESPACE#/}")}"
SRC="$(cd "$(dirname "$0")" && pwd)"

sudo install -d -m 755 /opt/tb4_agent
sudo install -m 755 "$SRC/tb4_agent.py" /opt/tb4_agent/tb4_agent.py
sudo install -m 644 "$SRC/tb4_agent.service" /etc/systemd/system/tb4_agent.service

if [ ! -f /etc/tb4_agent.env ]; then
  sed "s/^ROBOT_NAME=.*/ROBOT_NAME=${ROBOT_NAME}/" "$SRC/tb4_agent.env.example" | sudo tee /etc/tb4_agent.env >/dev/null
  sudo chmod 600 /etc/tb4_agent.env
  echo ">> Criado /etc/tb4_agent.env — edita RELAY_URL e AGENT_TOKEN:  sudo nano /etc/tb4_agent.env"
fi

# o bridge antigo (servidor TCP :5001) deixa de ser usado
pkill -f turtlebot4_bridge.py 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable tb4_agent.service
sudo systemctl restart tb4_agent.service
sleep 2
systemctl --no-pager --lines=10 status tb4_agent.service || true
echo ">> Logs: journalctl -u tb4_agent -f"
