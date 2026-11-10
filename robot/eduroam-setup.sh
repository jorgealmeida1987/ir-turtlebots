#!/usr/bin/env bash
# Liga o TurtleBot4 à eduroam (WPA2-Enterprise, PEAP/MSCHAPv2) via NetworkManager.
# Correr NO ROBÔ, de forma interativa (pede a password, que não fica no histórico):
#   ssh -t ubuntu@<robô> 'bash /tmp/tb4_agent/eduroam-setup.sh'
#
# Antes de correr, confirma com os serviços de informática da tua instituição:
#   - IDENTITY:   conta a usar (idealmente uma conta de dispositivo por robô, não uma pessoal)
#   - ANON_ID:    identidade anónima (ex.: anonymous@dominio.pt)
#   - DOMAIN:     sufixo do servidor RADIUS para validar o certificado (ex.: radius.dominio.pt)
#   - CA_CERT:    certificado da CA do RADIUS (por omissão, o bundle do sistema)
set -euo pipefail

# Opcional: 1.º argumento = hora UTC atual (ex.: vinda do PC), para o Pi sem RTC validar certificados.
if [ -n "${1:-}" ]; then
  sudo date -u -s "$1" >/dev/null && echo "Hora acertada: $(date -u)"
fi

# Valores por omissão da Universidade de Aveiro (perfil eduroam CAT 1015): PEAP/MSCHAPv2, SEM identidade
# anónima, servidores RADIUS celeborn.ua.pt / galadriel.ua.pt, CA HARICA TLS RSA Root CA 2021.
read -rp "Identidade eduroam (ex.: utilizador@ua.pt): " IDENTITY
read -rp "Identidade anónima [vazio = nenhuma, como na UA]: " ANON_ID
DEFAULT_SERVERS="celeborn.ua.pt;galadriel.ua.pt"
[ "${IDENTITY#*@}" = "ua.pt" ] || DEFAULT_SERVERS=""
read -rp "Servidores RADIUS (domain-match) [${DEFAULT_SERVERS}]: " DOMAIN
DOMAIN="${DOMAIN:-$DEFAULT_SERVERS}"
read -rp "Certificado CA [/etc/ssl/certs/HARICA_TLS_RSA_Root_CA_2021.pem]: " CA_CERT
CA_CERT="${CA_CERT:-/etc/ssl/certs/HARICA_TLS_RSA_Root_CA_2021.pem}"
IFS= read -rsp "Password: " PASS; echo          # IFS= mantém espaços no início/fim da password

sudo nmcli connection delete eduroam >/dev/null 2>&1 || true
sudo nmcli connection add type wifi ifname wlan0 con-name eduroam ssid eduroam \
  wifi-sec.key-mgmt wpa-eap \
  802-1x.eap peap 802-1x.phase2-auth mschapv2 \
  802-1x.identity "$IDENTITY" 802-1x.anonymous-identity "$ANON_ID" \
  802-1x.ca-cert "$CA_CERT" 802-1x.domain-match "$DOMAIN" \
  802-1x.password "$PASS" \
  connection.autoconnect yes connection.autoconnect-priority 10 ipv4.method auto
unset PASS

# O Pi não tem RTC: sem hora certa o TLS (eduroam e relay) falha. Forçar sincronização.
sudo nmcli connection up eduroam
sleep 5
ip -4 addr show wlan0 | grep inet || echo "!! sem IP na wlan0"
sudo chronyc -a makestep >/dev/null 2>&1 || true
timedatectl | grep -E "Local time|synchronized"
