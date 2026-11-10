#!/usr/bin/env python3
"""
Relay TurtleBot4 <-> alunos, tudo por HTTPS (443) atrás do nginx do servidor.

Robôs (tb4_agent):   WebSocket  /agent
    robô -> relay   {"type":"hello","robot":"robot1","token":"..."}     (primeira mensagem)
                    {"topic":"scan", ...}                                (dados, reencaminhados tal como chegam)
    relay -> robô   {"type":"subs","topics":["scan","odom"]}            (tópicos que alguém está a pedir)
                    {"type":"cmd","v":0.1,"w":0.0}                      (velocidade)

Alunos (MATLAB webread/webwrite, ou qualquer cliente HTTP):
    POST /api/<robot>/connect      {"user":"a12345","key":"<chave do robô>"}  -> {"session":..,"controller":true}
    GET  /api/<robot>/<topic>?session=..&after=<seq>                       -> última mensagem desse tópico
                                    (com after=N espera até 2 s por uma mensagem com seq > N)
    POST /api/<robot>/cmd          {"session":..,"v":0.1,"w":0.0}
    POST /api/<robot>/disconnect   {"session":..}
    GET  /api/<robot>/status       -> robô online?, quem controla, quem está ligado
    GET  /api/status               -> resumo de todos os robôs (base do futuro dashboard)

Regra de controlo: quem fez /connect por último controla o robô. Todas as ligações, saídas e
mudanças de controlo ficam registadas em EVENT_LOG (JSONL).

Carga mínima: cada mensagem do robô é recebida uma vez e guardada já em bytes; todos os pedidos
dos alunos recebem esses mesmos bytes, sem nova serialização. Sem alunos a pedir um tópico, o robô
deixa de o enviar.
"""
import asyncio
import json
import logging
import os
import secrets
import time

from aiohttp import WSMsgType, web

import tunnel  # túnel SSH de administração (relay/tunnel.py)

log = logging.getLogger("relay")

# --------------------------------------------------------------------------- configuração
ROBOTS = [r.strip() for r in os.environ.get("ROBOTS", "robot1,robot2,robot3,robot4").split(",") if r.strip()]
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
STUDENT_KEY = os.environ.get("STUDENT_KEY", "")            # vazio = sem chave de turma
# chave por robô (uma por grupo): ROBOT_KEYS=robot1:abcd,robot2:efgh — tem prioridade sobre STUDENT_KEY
ROBOT_KEYS = dict(kv.split(":", 1) for kv in os.environ.get("ROBOT_KEYS", "").replace(" ", "").split(",") if ":" in kv)
SESSION_TIMEOUT = float(os.environ.get("SESSION_TIMEOUT_S", "30"))
TOPIC_IDLE = float(os.environ.get("TOPIC_IDLE_S", "5"))      # tópico deixa de ser pedido ao robô após X s sem pedidos
LONGPOLL_MAX = float(os.environ.get("LONGPOLL_MAX_S", "2"))
EVENT_LOG = os.environ.get("EVENT_LOG", "/data/events.jsonl")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") == "1"      # usar X-Real-IP do nginx

STOP = {"type": "cmd", "v": 0.0, "w": 0.0}


def now() -> float:
    return time.time()


class EventLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, event: str, **fields) -> None:
        rec = {"t": round(now(), 3), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        log.info("%s %s", event, {k: v for k, v in fields.items()})
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("não consegui escrever no log de eventos: %s", e)


EVENTS = EventLog(EVENT_LOG)


class Session:
    def __init__(self, robot: str, user: str, ip: str):
        self.id = secrets.token_urlsafe(16)
        self.robot = robot
        self.user = user
        self.ip = ip
        self.connected_at = now()
        self.last_seen = now()

    def public(self) -> dict:
        return {"user": self.user, "ip": self.ip, "connected_at": round(self.connected_at),
                "idle_s": round(now() - self.last_seen, 1)}


class Robot:
    def __init__(self, name: str):
        self.name = name
        self.ws: web.WebSocketResponse | None = None
        self.agent_ip = ""
        self.online_since = 0.0
        self.latest: dict[str, tuple[int, bytes]] = {}       # tópico -> (seq, bytes json)
        self.seq = 0
        self.cond = asyncio.Condition()
        self.wanted: dict[str, float] = {}                   # tópico -> último pedido
        self.sent_subs: frozenset[str] | None = None
        self.sessions: dict[str, Session] = {}
        self.controller: str | None = None                   # session id
        self.cmds = 0

    # ---------------------------------------------------------------- robô -> relay
    async def on_data(self, raw: str) -> None:
        # raw começa por '{"topic":"xxx",' (garantido pelo agente) -> extrair sem parse completo
        prefix = '{"topic":"'
        if not raw.startswith(prefix):
            return
        end = raw.find('"', len(prefix))
        if end < 0:
            return
        topic = raw[len(prefix):end]
        self.seq += 1
        # injeta "seq" no início do objeto, uma única vez por mensagem
        body = ('{"seq":%d,' % self.seq + raw[1:]).encode()
        self.latest[topic] = (self.seq, body)
        async with self.cond:
            self.cond.notify_all()

    async def send(self, msg: dict) -> None:
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(msg))
            except ConnectionError:
                pass

    async def update_subs(self) -> None:
        cutoff = now() - TOPIC_IDLE
        topics = frozenset(t for t, ts in self.wanted.items() if ts >= cutoff)
        if topics != self.sent_subs and self.ws is not None:
            await self.send({"type": "subs", "topics": sorted(topics)})
            self.sent_subs = topics

    # ---------------------------------------------------------------- sessões / controlo
    def controller_session(self) -> Session | None:
        return self.sessions.get(self.controller) if self.controller else None

    async def set_controller(self, sid: str | None, reason: str) -> None:
        if sid == self.controller:
            return
        old = self.controller_session()
        self.controller = sid
        new = self.controller_session()
        if old is not None or self.controller is None:
            await self.send(STOP)       # mudou de mãos -> robô para antes de obedecer ao novo
        EVENTS.write("control", robot=self.name, reason=reason,
                     from_user=old.user if old else None, to_user=new.user if new else None,
                     to_ip=new.ip if new else None)

    async def drop_session(self, sid: str, reason: str) -> None:
        s = self.sessions.get(sid)
        if s is None:
            return
        EVENTS.write("disconnect", robot=self.name, user=s.user, ip=s.ip, reason=reason,
                     duration_s=round(now() - s.connected_at))
        if self.controller == sid:
            await self.set_controller(None, f"controller {reason}")   # antes do pop, para registar from_user
        self.sessions.pop(sid, None)

    def status(self) -> dict:
        c = self.controller_session()
        return {
            "robot": self.name,
            "online": self.ws is not None,
            "online_since": round(self.online_since) if self.ws is not None else None,
            "controller": c.user if c else None,
            "users": [s.public() for s in self.sessions.values()],
            "topics_available": sorted(self.latest),
            "topics_streaming": sorted(self.sent_subs or ()),
        }


ROBOTS_BY_NAME = {n: Robot(n) for n in ROBOTS}


# --------------------------------------------------------------------------- helpers HTTP
def client_ip(req: web.Request) -> str:
    if TRUST_PROXY:
        return req.headers.get("X-Real-IP") or req.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
            or (req.remote or "")
    return req.remote or ""


def get_robot(req: web.Request) -> Robot:
    r = ROBOTS_BY_NAME.get(req.match_info["robot"])
    if r is None:
        raise web.HTTPNotFound(text=json.dumps({"error": "robô desconhecido", "robots": ROBOTS}),
                               content_type="application/json")
    return r


async def body_json(req: web.Request) -> dict:
    # JSON (webwrite com MediaType application/json), formulário (webwrite por omissão) ou query string
    text = await req.text()
    data = None
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    elif text:
        data = dict(await req.post())
    if not isinstance(data, dict):
        data = {}
    return {**req.query, **data}


def err(status: int, msg: str, **extra) -> web.Response:
    return web.json_response({"error": msg, **extra}, status=status)


def get_session(r: Robot, sid: str | None) -> Session | None:
    s = r.sessions.get(sid or "")
    if s is not None:
        s.last_seen = now()
    return s


# --------------------------------------------------------------------------- rotas: robô
async def agent_ws(req: web.Request) -> web.WebSocketResponse:
    if tunnel.wants_tunnel(req):                       # /agent?tunnel=robotN (admin, ver tunnel.py)
        return await tunnel.handler(req)
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=4 * 1024 * 1024, compress=False)
    await ws.prepare(req)
    ip = client_ip(req)
    try:
        hello = json.loads((await ws.receive(timeout=10)).data)
        r = ROBOTS_BY_NAME[hello["robot"]]
        if hello.get("type") != "hello" or (AGENT_TOKEN and hello.get("token") != AGENT_TOKEN):
            raise PermissionError
    except Exception:
        log.warning("agente rejeitado (%s)", ip)
        await ws.close(code=4001, message=b"unauthorized")
        return ws

    if r.ws is not None:
        await r.ws.close(code=4000, message=b"replaced")
    r.ws, r.agent_ip, r.online_since, r.sent_subs = ws, ip, now(), None
    EVENTS.write("robot_online", robot=r.name, ip=ip)
    await r.update_subs()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if not await tunnel.on_agent_text(r, msg.data):
                    await r.on_data(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await tunnel.on_agent_binary(r, msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        if r.ws is ws:
            await tunnel.on_agent_gone(r)
            r.ws = None
            r.latest.clear()
            EVENTS.write("robot_offline", robot=r.name, ip=ip)
            async with r.cond:
                r.cond.notify_all()
    return ws


# --------------------------------------------------------------------------- rotas: alunos
async def connect(req: web.Request) -> web.Response:
    r = get_robot(req)
    data = await body_json(req)
    user = str(data.get("user", "")).strip()[:64]
    if not user:
        return err(400, "falta o campo 'user' (ex.: número de aluno)")
    key = ROBOT_KEYS.get(r.name, STUDENT_KEY)
    if key and str(data.get("key", "")).strip().lower() != key.lower():
        return err(403, f"chave inválida para o {r.name}")
    s = Session(r.name, user, client_ip(req))
    r.sessions[s.id] = s
    EVENTS.write("connect", robot=r.name, user=user, ip=s.ip)
    await r.set_controller(s.id, "last to connect")
    return web.json_response({"session": s.id, "robot": r.name, "controller": True,
                              "online": r.ws is not None})


async def disconnect(req: web.Request) -> web.Response:
    r = get_robot(req)
    data = await body_json(req)
    await r.drop_session(str(data.get("session", "")), "left")
    return web.json_response({"ok": True})


async def cmd(req: web.Request) -> web.Response:
    r = get_robot(req)
    data = await body_json(req)
    s = get_session(r, data.get("session"))
    if s is None:
        return err(401, "sessão inválida ou expirada: faz connect outra vez")
    if r.controller != s.id:
        c = r.controller_session()
        return err(409, "não és tu quem controla o robô", controller=c.user if c else None)
    try:
        v, w = float(data.get("v", 0.0)), float(data.get("w", 0.0))
    except (TypeError, ValueError):
        return err(400, "v e w têm de ser números")
    if r.ws is None:
        return err(503, "robô offline")
    r.cmds += 1
    await r.send({"type": "cmd", "v": v, "w": w})
    return web.json_response({"ok": True})


async def topic(req: web.Request) -> web.Response:
    r = get_robot(req)
    name = req.match_info["topic"]
    s = get_session(r, req.query.get("session"))
    if s is None:
        return err(401, "sessão inválida ou expirada: faz connect outra vez")
    r.wanted[name] = now()
    if r.sent_subs is None or name not in r.sent_subs:
        await r.update_subs()

    try:
        after = int(req.query.get("after", "-1"))
    except ValueError:
        after = -1
    item = r.latest.get(name)
    if (item is None or item[0] <= after) and r.ws is not None:
        try:
            async with r.cond:
                await asyncio.wait_for(
                    r.cond.wait_for(lambda: (x := r.latest.get(name)) is not None and x[0] > after or r.ws is None),
                    timeout=LONGPOLL_MAX)
        except asyncio.TimeoutError:
            pass
        item = r.latest.get(name)
    if item is None:
        return err(503 if r.ws is None else 504,
                   "robô offline" if r.ws is None else f"ainda sem dados de '{name}'")
    return web.Response(body=item[1], content_type="application/json")


async def robot_status(req: web.Request) -> web.Response:
    return web.json_response(get_robot(req).status())


async def all_status(_req: web.Request) -> web.Response:
    return web.json_response({"robots": [r.status() for r in ROBOTS_BY_NAME.values()]})


async def healthz(_req: web.Request) -> web.Response:
    return web.Response(text="ok")


# --------------------------------------------------------------------------- manutenção
async def housekeeping(_app: web.Application):
    async def loop():
        while True:
            await asyncio.sleep(1)
            t = now()
            for r in ROBOTS_BY_NAME.values():
                for sid, s in list(r.sessions.items()):
                    if t - s.last_seen > SESSION_TIMEOUT:
                        await r.drop_session(sid, "timeout")
                await r.update_subs()
    task = asyncio.create_task(loop())
    yield
    task.cancel()


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app.cleanup_ctx.append(housekeeping)
    tunnel.init(ROBOTS_BY_NAME, EVENTS, client_ip)
    app.add_routes([
        web.get("/healthz", healthz),
        web.get("/agent", agent_ws),
        web.get("/api/status", all_status),
        web.get("/api/{robot}/status", robot_status),
        web.post("/api/{robot}/connect", connect),
        web.post("/api/{robot}/disconnect", disconnect),
        web.post("/api/{robot}/cmd", cmd),
        web.get("/api/{robot}/{topic}", topic),
    ])
    return app


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if not AGENT_TOKEN:
        log.warning("AGENT_TOKEN vazio: qualquer pessoa pode fazer-se passar por um robô!")
    web.run_app(make_app(), host=HOST, port=PORT, access_log=None)
