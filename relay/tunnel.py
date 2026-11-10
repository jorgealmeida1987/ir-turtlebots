"""
Túnel TCP de administração (ex.: SSH) até um robô, através da ligação WebSocket que o agente já
mantém com o relay. Serve para entrar nos robôs que estão na eduroam (sem ligações de entrada).

  admin  --wss--> /agent?tunnel=robot1&port=22   (Authorization: Bearer <ADMIN_TOKEN>)
  relay  --texto--> agente   {"type":"tunnel_open","id":"<8 hex>","port":22}
  dados: frames binários  <id 8 bytes ascii><payload>  nos dois sentidos (relay <-> agente)
  fecho: {"type":"tunnel_close","id":..} (relay -> agente)  /  {"type":"tunnel_closed","id":..} (agente -> relay)

Desligado se ADMIN_TOKEN estiver vazio. Só os portos em TUNNEL_PORTS (omissão: 22).
"""
import hmac
import json
import logging
import os
import secrets

from aiohttp import WSMsgType, web

log = logging.getLogger("relay.tunnel")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
TUNNEL_PORTS = {int(p) for p in os.environ.get("TUNNEL_PORTS", "22").split(",") if p.strip()}
ID_LEN = 8

_tunnels: dict[str, tuple[object, web.WebSocketResponse]] = {}   # id -> (Robot, ws do admin)
_ctx: dict = {}


def init(robots_by_name: dict, events, client_ip) -> None:
    _ctx.update(robots=robots_by_name, events=events, client_ip=client_ip)


def wants_tunnel(req: web.Request) -> bool:
    return "tunnel" in req.query


async def handler(req: web.Request) -> web.StreamResponse:
    ip = _ctx["client_ip"](req)
    if not ADMIN_TOKEN:
        raise web.HTTPNotFound()
    token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), ADMIN_TOKEN.encode()):
        _ctx["events"].write("tunnel_denied", ip=ip, robot=req.query.get("tunnel"))
        raise web.HTTPForbidden(text="token inválido")
    r = _ctx["robots"].get(req.query.get("tunnel", ""))
    if r is None:
        raise web.HTTPNotFound(text="robô desconhecido")
    try:
        port = int(req.query.get("port", "22"))
    except ValueError:
        raise web.HTTPBadRequest(text="port inválido")
    if port not in TUNNEL_PORTS:
        raise web.HTTPForbidden(text=f"porto {port} não permitido")
    if r.ws is None:
        raise web.HTTPServiceUnavailable(text=f"{r.name} offline")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024, compress=False)
    await ws.prepare(req)
    tid = secrets.token_hex(ID_LEN // 2)
    _tunnels[tid] = (r, ws)
    _ctx["events"].write("tunnel_open", robot=r.name, ip=ip, port=port, id=tid)
    sent_bytes = 0
    try:
        await r.ws.send_str(json.dumps({"type": "tunnel_open", "id": tid, "port": port}))
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if r.ws is None or r.ws.closed:
                    break
                await r.ws.send_bytes(tid.encode() + msg.data)
                sent_bytes += len(msg.data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        _tunnels.pop(tid, None)
        if r.ws is not None and not r.ws.closed:
            try:
                await r.ws.send_str(json.dumps({"type": "tunnel_close", "id": tid}))
            except ConnectionError:
                pass
        _ctx["events"].write("tunnel_close", robot=r.name, ip=ip, id=tid, bytes_up=sent_bytes)
    return ws


async def on_agent_text(robot, text: str) -> bool:
    """Trata mensagens de túnel vindas do agente. Devolve True se a mensagem era de túnel."""
    if not text.startswith('{"type":"tunnel'):
        return False
    try:
        msg = json.loads(text)
    except ValueError:
        return True
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
