# ir-turtlebots — relay TurtleBot4 ↔ alunos (Introdução à Robótica)

Os TurtleBot4 ligam-se à **eduroam** e abrem uma ligação para um **relay** no servidor da Nomad
(`tb.lexicode-consulting.pt`). Os alunos, também na eduroam, falam com o relay por HTTPS a partir
do MATLAB. Nada precisa de portas abertas para além do 443.

```
TurtleBot4 (eduroam)                 servidor nomad (Docker)                     alunos (MATLAB)
tb4_agent (ROS 2) ── wss://…/agent ──► tbrelay (aiohttp) ◄── https://…/api/… ── TurtleBot.m
  scan/odom/battery/... ─────────────►  guarda a última msg de cada tópico ─────► tb.scan(), tb.read("odom")
  ◄──────────── cmd_vel ──────────────  controla quem fez connect por último ◄── tb.cmd(v, w)
```

| Pasta | O quê |
|---|---|
| `relay/` | relay (Python/aiohttp) + Dockerfile |
| `robot/` | `tb4_agent.py` (só stdlib + rclpy), serviço systemd, instalação, configuração eduroam |
| `matlab/` | `TurtleBot.m` — classe para os alunos |
| `deploy/nginx/tbrelay` | vhost nginx (aplicado pelo Jorge, com sudo) |
| `tools/` | `student_test.py` (teste ponta-a-ponta), `fake_sensors.py` (LiDAR falso no robô) |

## Regras do relay

- **Controla o robô quem fez `connect` por último.** O anterior passa a receber `409` nos comandos.
  Cada troca de controlo manda primeiro um STOP ao robô.
- O robô **para sozinho** se não receber comandos durante 1 s ou se perder a ligação ao relay.
- Ligações, saídas, timeouts e trocas de controlo ficam em `/data/events.jsonl` (volume Docker).
- O robô só envia os tópicos que algum aluno pediu nos últimos 5 s. Sem alunos, não há tráfego.

## API

| Método | Caminho | Corpo / query | Resposta |
|---|---|---|---|
| POST | `/api/<robot>/connect` | `{"user":"a12345","key":"<STUDENT_KEY>"}` | `{"session":…, "controller":true}` |
| GET | `/api/<robot>/<tópico>` | `?session=…&after=<seq>` | última mensagem (long-poll até 2 s se `after`) |
| POST | `/api/<robot>/cmd` | `{"session":…,"v":0.1,"w":0}` | `200` / `409` (não controlas) / `401` / `503` |
| POST | `/api/<robot>/disconnect` | `{"session":…}` | |
| GET | `/api/<robot>/status` · `/api/status` | | quem está ligado, quem controla (base do dashboard) |
| GET | `/agent` | WebSocket dos robôs (`AGENT_TOKEN`) | |

O scan mantém o formato antigo (`stamp`, `ranges`, `angles`) e acrescenta `topic` e `seq`.
Qualquer tópico do namespace do robô pode ser pedido: `scan`, `odom`, `battery_state`, `imu`, `dock_status`, …

## Deploy no servidor nomad

Regras do servidor (partilhado): só tocar em recursos `tbrelay`, usar sempre `-p tbrelay`,
**não fazer build no servidor**, portas só em `127.0.0.1`, ver `df -h /` antes de cada deploy.

Pré-requisitos no PC que faz o deploy: Docker, e o alias SSH `nomad-test`
(`nomad-consulting.pt`, porta 22555, chave autorizada, IP na allowlist, VPN desligada).

```bash
# 0) uma vez: contexto docker remoto e .env.nomad (NÃO vai para o git)
docker context create nomad-test --docker host=ssh://nomad-test
cp .env.nomad.example .env.nomad      # preencher AGENT_TOKEN (o mesmo que está nos robôs)

# 1) build LOCAL (o servidor é amd64)
docker build --platform linux/amd64 -t tbrelay:latest relay/

# 2) enviar a imagem
ssh nomad-test 'df -h /'
OLD=$(ssh nomad-test "docker image inspect tbrelay:latest --format '{{.Id}}'" 2>/dev/null || true)
docker save tbrelay:latest | gzip -1 | ssh nomad-test 'gunzip | docker load'

# 3) arrancar (o compose corre contra o docker remoto)
docker --context nomad-test compose -p tbrelay -f docker-compose.nomad.yml --env-file .env.nomad up -d
[ -n "$OLD" ] && ssh nomad-test "docker rmi $OLD" || true

# 4) verificar
ssh nomad-test 'curl -s http://127.0.0.1:10120/healthz; docker logs --tail 20 tbrelay-relay'
```

Confirmar antes que a porta `10120` está livre no servidor:
`ssh nomad-test 'docker ps --format "{{.Names}} {{.Ports}}"'`.

### nginx + HTTPS (Jorge, com sudo)

DNS: `tb.lexicode-consulting.pt  A  213.63.232.58` (sem AAAA). Comandos no topo de
[`deploy/nginx/tbrelay`](deploy/nginx/tbrelay):

```bash
sudo cp tbrelay /etc/nginx/sites-available/tbrelay
sudo ln -s /etc/nginx/sites-available/tbrelay /etc/nginx/sites-enabled/tbrelay
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d tb.lexicode-consulting.pt
```

## Robô

1. **eduroam** (UA: PEAP/MSCHAPv2, sem identidade anónima, servidores `celeborn.ua.pt;galadriel.ua.pt`,
   CA HARICA): `ssh -t <robô> "bash /tmp/tb4_agent/eduroam-setup.sh '$(date -u +%Y-%m-%dT%H:%M:%SZ)'"`.
   O Pi não tem RTC, por isso o script acerta a hora antes de autenticar. Depois de várias falhas o
   NetworkManager desiste: `sudo nmcli con up eduroam`.
2. **Agente** como serviço systemd (precisa de sudo):
   `scp -r robot <robô>:/tmp/tb4_agent && ssh -t <robô> 'bash /tmp/tb4_agent/install.sh robot1'`,
   e depois preencher `AGENT_TOKEN` em `/etc/tb4_agent.env`.
   Logs: `journalctl -u tb4_agent -f`.
   **Sem sudo** (como está hoje no robot1): `robot/run_agent_user.sh` em `~/tb4_agent/`, com
   `agent.env` (600) e `@reboot` no crontab do `ubuntu`. Logs em `~/tb4_agent/agent.log`.
   O `AGENT_TOKEN` está no `.env.nomad` local (fora do git) — o relay tem de usar o mesmo.

## Alunos (MATLAB)

```matlab
tb = TurtleBot("robot1", "a12345");          % liga-se e passa a controlar
while true
    s = tb.scan();                           % s.ranges, s.angles (como antes)
    [v, w] = controlador(s);
    tb.cmd(v, w);                            % enviar em cada iteração (watchdog de 1 s)
end
clear tb                                     % desliga; o robô para
```

## Testes

```bash
python -m venv .venv && .venv/Scripts/pip install -r relay/requirements.txt
AGENT_TOKEN=devtoken EVENT_LOG=.local/events.jsonl .venv/Scripts/python relay/relay.py
# no robô (pelo túnel): ssh -R 18080:127.0.0.1:8080 <robô> \
#   'source /etc/turtlebot4/setup.bash; cd /tmp/tb4_agent; python3 fake_sensors.py & \
#    ROBOT_NAME=robot1 RELAY_URL=ws://127.0.0.1:18080/agent AGENT_TOKEN=devtoken python3 tb4_agent.py'
.venv/Scripts/python tools/student_test.py http://127.0.0.1:8080 robot1
```
