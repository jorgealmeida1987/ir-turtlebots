#!/usr/bin/env bash
# Arranque do tb4_agent SEM sudo (alternativa ao serviço systemd do install.sh).
# Instalação (como ubuntu):
#   mkdir -p ~/tb4_agent && cp tb4_agent.py run_agent_user.sh ~/tb4_agent/
#   cp <env> ~/tb4_agent/agent.env && chmod 600 ~/tb4_agent/agent.env
#   (crontab -l 2>/dev/null; echo "@reboot $HOME/tb4_agent/run_agent_user.sh") | crontab -
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/agent.log"
exec 9>"$DIR/.lock"
flock -n 9 || exit 0                               # já há uma instância a correr
set -a; . "$DIR/agent.env"; set +a
source /etc/turtlebot4/setup.bash
while true; do
    [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5000000 ] && mv -f "$LOG" "$LOG.1"
    python3 -u "$DIR/tb4_agent.py" >>"$LOG" 2>&1
    echo "$(date) agente terminou ($?), a reiniciar em 5 s" >>"$LOG"
    sleep 5
done
