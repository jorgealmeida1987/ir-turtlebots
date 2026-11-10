"""
Túneis de administração até um robô, através da ligação WebSocket que o agente já mantém com o
relay (a eduroam não aceita ligações de entrada).

Dois tipos:
  - "tcp": ligação a 127.0.0.1:<porto> no robô (SSH). Cliente: tools/ws_tunnel.py.
  - "pty": shell (bash -l) num pseudo-terminal no robô. Cliente: consola web (xterm.js).

Entradas:
  - /agent?tunnel=<robot>&port=22             (Authorization: Bearer <ADMIN_TOKEN>)   -> tcp
  - /agent?tunnel=<robot>&kind=pty&cols=..&rows=..  (idem)                            -> pty
  - console_handler(req, robot, user)  -> para a página de admin, DEPOIS de autenticar o admin.

Protocolo com o browser/cliente (WebSocket):
  - frames BINÁRIOS = bytes do terminal / do socket, nos dois sentidos;
  - frames de TEXTO (só pty) = JSON de controlo: {"type":"resize","cols":120,"rows":32}.

Protocolo relay <-> agente (na ligação /agent do robô):
  relay -> agente  {"type":"tunnel_open","id":"<8 hex>","kind":"tcp","port":22}
                   {"type":"tunnel_open","id":..,"kind":"pty","cols":80,"rows":24}
                   {"type":"tunnel_resize","id":..,"cols":..,"rows":..}
                   {"type":"tunnel_close","id":..}
  agente -> relay  {"type":"tunnel_closed","id":..}
  dados: frames binários <id 8 bytes ascii><payload>, nos dois sentidos.

Desligado se ADMIN_TOKEN estiver vazio (exceto console_handler, que confia na autenticação de quem
o chama). Todas as aberturas, recusas e fechos ficam no log de eventos.
"""
import hmac
import json
import logging
import os
import secrets
import time

from aiohttp import WSMsgType, web

log = logging.getLogger("relay.tunnel")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
TUNNEL_PORTS = {int(p) for p in os.environ.get("TUNNEL_PORTS", "22").split(",") if p.strip()}
ID_LEN = 8

_tunnels: dict[str, tuple[object, web.WebSocketResponse]] = {}   # id -> (Robot, ws do cliente)
_info: dict[str, dict] = {}                                       # id -> metadados (para o dashboard)
_ctx: dict = {}


def init(robots_by_name: dict, events, client_ip) -> None:
    _ctx.update(robots=robots_by_name, events=events, client_ip=client_ip)


def wants_tunnel(req: web.Request) -> bool:
    return "tunnel" in req.query


def active() -> list[dict]:
    """Túneis/consolas abertos agora (para mostrar no dashboard)."""
    return [dict(i, id=tid, age_s=round(time.time() - i["since"])) for tid, i in _info.items()]


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def handler(req: web.Request) -> web.StreamResponse:
    """Entrada por token (CLI / ssh): /agent?tunnel=<robot>[&kind=pty]."""
    ip = _ctx["client_ip"](req)
    if not ADMIN_TOKEN:
        raise web.HTTPNotFound()
    token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), ADMIN_TOKEN.encode()):
        _ctx["events"].write("tunnel_denied", ip=ip, robot=req.query.get("tunnel"))
        raise web.HTTPForbidden(text="token inválido")
    robot = req.query.get("tunnel", "")
    if req.query.get("kind") == "pty":
        return await console_handler(req, robot, user="admin-token",
                                     cols=_int(req.query.get("cols"), 80), rows=_int(req.query.get("rows"), 24))
    port = _int(req.query.get("port"), 22)
    if port not in TUNNEL_PORTS:
        raise web.HTTPForbidden(text=f"porto {port} não permitido")
    return await _bridge(req, robot, {"kind": "tcp", "port": port}, user="admin-token")


async def console_handler(req: web.Request, robot: str, user: str,
                          cols: int = 80, rows: int = 24) -> web.StreamResponse:
    """Consola web (shell no robô). CHAMAR SÓ DEPOIS DE AUTENTICAR O ADMIN. `user` fica no log."""
    return await _bridge(req, robot, {"kind": "pty", "cols": cols, "rows": rows}, user=user)


async def _bridge(req: web.Request, robot: str, open_args: dict, user: str) -> web.StreamResponse:
    ip = _ctx["client_ip"](req)
    r = _ctx["robots"].get(robot)
    if r is None:
        raise web.HTTPNotFound(text="robô desconhecido")
    if r.ws is None:
        raise web.HTTPServiceUnavailable(text=f"{r.name} offline")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024, compress=False)
    await ws.prepare(req)
    tid = secrets.token_hex(ID_LEN // 2)
    kind = open_args["kind"]
    _tunnels[tid] = (r, ws)
    _info[tid] = {"robot": r.name, "kind": kind, "user": user, "ip": ip, "since": time.time()}
    _ctx["events"].write("tunnel_open", robot=r.name, kind=kind, user=user, ip=ip, id=tid,
                         **({"port": open_args["port"]} if kind == "tcp" else {}))
    sent_bytes = 0
    try:
        await r.ws.send_str(json.dumps({"type": "tunnel_open", "id": tid, **open_args}))
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if r.ws is None or r.ws.closed:
                    break
                await r.ws.send_bytes(tid.encode() + msg.data)
                sent_bytes += len(msg.data)
            elif msg.type == WSMsgType.TEXT and kind == "pty":
                try:
                    ctl = json.loads(msg.data)
                except ValueError:
                    continue
                if ctl.get("type") == "resize" and r.ws is not None:
                    await r.ws.send_str(json.dumps({"type": "tunnel_resize", "id": tid,
                                                    "cols": _int(ctl.get("cols"), 80),
                                                    "rows": _int(ctl.get("rows"), 24)}))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        _tunnels.pop(tid, None)
        info = _info.pop(tid, {})
        if r.ws is not None and not r.ws.closed:
            try:
                await r.ws.send_str(json.dumps({"type": "tunnel_close", "id": tid}))
            except ConnectionError:
                pass
        _ctx["events"].write("tunnel_close", robot=r.name, kind=kind, user=user, ip=ip, id=tid,
                             bytes_up=sent_bytes, duration_s=round(time.time() - info.get("since", time.time())))
    return ws


async def on_agent_text(robot, text: str) -> bool:
    """Trata mensagens de túnel vindas do agente. Devolve True se a mensagem era de túnel."""
    if not text.startswith('{"type"'):                 # dados dos tópicos começam por {"topic"
        return False
    try:
        msg = json.loads(text)
    except ValueError:
        return True
    if not str(msg.get("type", "")).startswith("tunnel"):
        return False
    entry = _tunnels.get(str(msg.get("id")))
    if entry and entry[0] is robot and msg.get("type") == "tunnel_closed":
        await entry[1].close()
    return True


async def on_agent_binary(robot, data: bytes) -> None:
    entry = _tunnels.get(data[:ID_LEN].decode(errors="replace"))
    if entry is None or entry[0] is not robot or entry[1].closed:
        return
    try:
        await entry[1].send_bytes(data[ID_LEN:])
    except ConnectionError:
        pass


async def on_agent_gone(robot) -> None:
    for tid, (r, ws) in list(_tunnels.items()):
        if r is robot:
            await ws.close()
