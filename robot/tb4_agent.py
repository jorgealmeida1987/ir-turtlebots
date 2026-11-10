#!/usr/bin/env python3
"""
tb4_agent — corre no TurtleBot4 e liga-se ao relay (wss://.../agent).

- Só a biblioteca padrão + rclpy (os robôs não precisam de pip nem de internet para instalar).
- Liga-se para fora (funciona na eduroam, com isolamento entre clientes) e volta a ligar sozinho.
- Subscreve tópicos ROS a pedido: o relay diz que tópicos os alunos estão a pedir e o agente
  subscreve/cancela automaticamente (qualquer tópico do namespace do robô, com tipo detetado).
- Recebe comandos de velocidade e publica em <ns>/cmd_vel. Watchdog: sem comandos durante
  CMD_TIMEOUT_S (ou sem ligação ao relay) o robô para.

Configuração por variáveis de ambiente (ver tb4_agent.env.example).
"""
import array
import asyncio
import base64
import fcntl
import hashlib
import json
import math
import os
import pty
import signal
import ssl
import struct
import termios
import threading
import time
from urllib.parse import urlsplit

import rclpy
from geometry_msgs.msg import Twist, TwistStamped
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.convert import message_to_ordereddict
from rosidl_runtime_py.utilities import get_message
from sensor_msgs.msg import LaserScan

# --------------------------------------------------------------------------- configuração
ROBOT = os.environ.get("ROBOT_NAME", "robot1")
RELAY_URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8080/agent")
TOKEN = os.environ.get("AGENT_TOKEN", "")
NS = os.environ.get("ROS_NS", f"/{ROBOT}").rstrip("/")
CMD_TIMEOUT = float(os.environ.get("CMD_TIMEOUT_S", "1.0"))
MAX_V = float(os.environ.get("MAX_V", "0.3"))           # m/s   (Create3 ~0.306)
MAX_W = float(os.environ.get("MAX_W", "1.9"))           # rad/s
TOPIC_MAX_HZ = float(os.environ.get("TOPIC_MAX_HZ", "10"))
SCAN_MAX_HZ = float(os.environ.get("SCAN_MAX_HZ", "20"))
STAMPED = os.environ.get("CMD_VEL_STAMPED", "1") == "1"
BLOCKED = {t for t in os.environ.get("BLOCKED_TOPICS", "cmd_vel,cmd_vel_unstamped,robot_description").split(",") if t}
MAX_MSG_BYTES = int(os.environ.get("MAX_MSG_BYTES", str(1024 * 1024)))
# túnel de administração (SSH pelo relay): portos locais que o relay pode abrir; vazio = desligado
TUNNEL_PORTS = {int(p) for p in os.environ.get("TUNNEL_PORTS", "22").split(",") if p.strip()}
TUNNEL_ID_LEN = 8
# consola web (shell num pseudo-terminal, aberta pelo relay a pedido da página de admin)
CONSOLE = os.environ.get("CONSOLE", "1") == "1"
CONSOLE_SHELL = os.environ.get("CONSOLE_SHELL", "/bin/bash")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- WebSocket mínimo (RFC 6455)
class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.r, self.w = reader, writer
        self.wlock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str, timeout: float = 10.0) -> "WebSocket":
        u = urlsplit(url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        ctx = ssl.create_default_context() if secure else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, port, ssl=ctx, server_hostname=u.hostname if secure else None,
                                    limit=2 ** 22), timeout)
        sock = writer.get_extra_info("socket")
        if sock is not None:
            import socket
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"User-Agent: tb4_agent/{ROBOT}\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        lines = head.decode(errors="replace").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"handshake falhou: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if l)}
        expect = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expect:
            raise ConnectionError("handshake: Sec-WebSocket-Accept inválido")
        return cls(reader, writer)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        # XOR rápido com inteiros grandes (evita ciclo byte-a-byte em Python)
        m = (mask * (n // 4 + 1))[:n]
        masked = (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big") if n else b""
        async with self.wlock:
            self.w.write(header + mask + masked)
            await self.w.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(0x1, text.encode())

    async def send_binary(self, data: bytes) -> None:
        await self._send_frame(0x2, data)

    async def recv_text(self) -> str:
        return (await self.recv())[1].decode(errors="replace")

    async def recv(self) -> tuple[int, bytes]:
        """Próxima mensagem completa: (0x1 texto | 0x2 binário, bytes)."""
        buf, first_op = b"", None
        while True:
            try:
                b1, b2 = await self.r.readexactly(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", await self.r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", await self.r.readexactly(8))[0]
                mask = await self.r.readexactly(4) if b2 & 0x80 else None
                data = await self.r.readexactly(n) if n else b""
            except (asyncio.IncompleteReadError, ConnectionError) as e:
                raise WSClosed(str(e))
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            op, fin = b1 & 0x0F, b1 & 0x80
            if op == 0x9:                       # ping -> pong
                await self._send_frame(0xA, data)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                raise WSClosed(f"fechado pelo servidor ({code} {data[2:].decode(errors='replace')})")
            if op in (0x1, 0x2):
                first_op, buf = op, data
            elif op == 0x0:
                buf += data
            if fin and first_op is not None:
                return first_op, buf

    def close(self) -> None:
        try:
            self.w.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- conversão de mensagens
def _jsonable(o):
    if isinstance(o, (bytes, bytearray)):
        return list(o)
    if isinstance(o, array.array):
        return o.tolist()
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def encode(topic: str, msg) -> str:
    if isinstance(msg, LaserScan):
        # formato antigo (stamp/ranges/angles) + metadados; 3 casas decimais = mm
        n = len(msg.ranges)
        d = {"topic": topic,
             "stamp": msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9,
             "ranges": [round(r, 3) if math.isfinite(r) else r for r in msg.ranges],
             "angles": [round(msg.angle_min + i * msg.angle_increment, 4) for i in range(n)],
             "range_min": msg.range_min, "range_max": msg.range_max}
    else:
        d = {"topic": topic, **message_to_ordereddict(msg)}
    return json.dumps(d, default=_jsonable, separators=(",", ":"))


# --------------------------------------------------------------------------- nó ROS
class Agent(Node):
    def __init__(self, outbox):
        super().__init__("tb4_agent")
        self.outbox = outbox                       # callable(topic, text) thread-safe
        self.lock = threading.Lock()
        self.wanted: set[str] = set()
        self.subs: dict[str, object] = {}
        self.last_sent: dict[str, float] = {}
        self.missing_logged: set[str] = set()
        self.cmd_pub = self.create_publisher(TwistStamped if STAMPED else Twist, f"{NS}/cmd_vel", 10)
        self.last_cmd = 0.0
        self.moving = False
        self.create_timer(0.5, self.sync_subscriptions)
        self.create_timer(0.1, self.watchdog)
        log(f"agente {ROBOT}: ns={NS} relay={RELAY_URL} cmd_vel={'TwistStamped' if STAMPED else 'Twist'}")

    # chamado a partir da thread asyncio
    def set_wanted(self, topics) -> None:
        with self.lock:
            self.wanted = {str(t).strip("/") for t in topics if str(t).strip("/") and str(t).strip("/") not in BLOCKED}

    def sync_subscriptions(self) -> None:
        with self.lock:
            wanted = set(self.wanted)
        for t in list(self.subs):
            if t not in wanted:
                self.destroy_subscription(self.subs.pop(t))
                log(f"deixei de subscrever {t}")
        todo = wanted - set(self.subs)
        if not todo:
            return
        types = dict(self.get_topic_names_and_types())
        for t in todo:
            full = f"{NS}/{t.strip('/')}"
            if full not in types:
                if t not in self.missing_logged:
                    log(f"tópico {full} não existe (ainda)")
                    self.missing_logged.add(t)
                continue
            try:
                cls = get_message(types[full][0])
            except Exception as e:
                log(f"tipo desconhecido para {full}: {e}")
                continue
            self.subs[t] = self.create_subscription(cls, full, lambda m, t=t: self.on_msg(t, m),
                                                    qos_profile_sensor_data)
            self.missing_logged.discard(t)
            log(f"subscrevi {full} [{types[full][0]}]")

    def on_msg(self, topic: str, msg) -> None:
        hz = SCAN_MAX_HZ if isinstance(msg, LaserScan) else TOPIC_MAX_HZ
        t = time.monotonic()
        if hz > 0 and t - self.last_sent.get(topic, 0.0) < 1.0 / hz:
            return
        self.last_sent[topic] = t
        try:
            text = encode(topic, msg)
        except Exception as e:
            log(f"erro a converter {topic}: {e}")
            return
        if len(text) <= MAX_MSG_BYTES:
            self.outbox(topic, text)

    # comandos (thread asyncio)
    def command(self, v: float, w: float) -> None:
        v = max(-MAX_V, min(MAX_V, v))
        w = max(-MAX_W, min(MAX_W, w))
        self._publish(v, w)
        self.last_cmd = time.monotonic()
        self.moving = v != 0.0 or w != 0.0

    def stop(self, why: str = "") -> None:
        self._publish(0.0, 0.0)
        if self.moving and why:
            log(f"STOP ({why})")
        self.moving = False

    def watchdog(self) -> None:
        if self.moving and time.monotonic() - self.last_cmd > CMD_TIMEOUT:
            self.stop(f"sem comandos há {CMD_TIMEOUT}s")

    def _publish(self, v: float, w: float) -> None:
        if STAMPED:
            m = TwistStamped()
            m.header.stamp = self.get_clock().now().to_msg()
            m.twist.linear.x, m.twist.angular.z = float(v), float(w)
        else:
            m = Twist()
            m.linear.x, m.angular.z = float(v), float(w)
        self.cmd_pub.publish(m)


# --------------------------------------------------------------------------- ligação ao relay
class Link:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.pending: dict[str, str] = {}              # tópico -> última mensagem (latest-wins)
        self.wake = asyncio.Event()
        self.node: Agent | None = None
        self.connected = False
        self.dropped = 0
        self.tunnels: dict[str, dict] = {}

    def outbox(self, topic: str, text: str) -> None:   # chamado pela thread ROS
        if self.connected:
            self.loop.call_soon_threadsafe(self._queue, topic, text)

    def _queue(self, topic: str, text: str) -> None:
        if topic in self.pending:
            self.dropped += 1
        self.pending[topic] = text
        self.wake.set()

    async def sender(self, ws: WebSocket) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            batch, self.pending = self.pending, {}
            for text in batch.values():
                await ws.send_text(text)

    async def receiver(self, ws: WebSocket) -> None:
        while True:
            op, data = await ws.recv()
            if op == 0x2:                                   # dados de túnel: <id><payload>
                self.tunnel_data(data[:TUNNEL_ID_LEN].decode(errors="replace"), data[TUNNEL_ID_LEN:])
                continue
            msg = json.loads(data)
            kind = msg.get("type")
            if kind == "cmd":
                self.node.command(float(msg.get("v", 0.0)), float(msg.get("w", 0.0)))
            elif kind == "subs":
                self.node.set_wanted(msg.get("topics", []))
                log(f"relay pede: {msg.get('topics')}")
            elif kind == "tunnel_open":
                if msg.get("kind") == "pty":
                    self.console_open(ws, str(msg.get("id")), int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                else:
                    self.tunnel_open(ws, str(msg.get("id")), int(msg.get("port", 0)))
            elif kind == "tunnel_resize":
                self.console_resize(str(msg.get("id")), int(msg.get("cols", 80)), int(msg.get("rows", 24)))
            elif kind == "tunnel_close":
                self.tunnel_close(str(msg.get("id")))

    # ---------------------------------------------------------------- consola (shell num PTY)
    def console_open(self, ws: WebSocket, tid: str, cols: int, rows: int) -> None:
        if not CONSOLE or len(tid) != TUNNEL_ID_LEN or tid in self.tunnels:
            log(f"consola {tid} recusada")
            asyncio.create_task(ws.send_text(json.dumps({"type": "tunnel_closed", "id": tid})))
            return
        home = os.path.expanduser("~")
        env = {"HOME": home, "USER": os.environ.get("USER", "ubuntu"), "LOGNAME": os.environ.get("USER", "ubuntu"),
               "SHELL": CONSOLE_SHELL, "TERM": "xterm-256color", "LANG": os.environ.get("LANG", "C.UTF-8"),
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        pid, fd = pty.fork()
        if pid == 0:                                    # filho: só exec (o pai tem threads)
            try:
                os.chdir(home)
                os.execve(CONSOLE_SHELL, [CONSOLE_SHELL, "-l"], env)
            finally:
                os._exit(127)
        os.set_blocking(fd, False)
        t = {"writer": None, "pending": [], "pty": fd, "pid": pid, "task": None}
        self.tunnels[tid] = t
        self.console_resize(tid, cols, rows)
        loop = asyncio.get_running_loop()

        def readable():
            try:
                data = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                data = b""
            if data:
                asyncio.create_task(ws.send_binary(tid.encode() + data))
            else:                                       # shell terminou
                self.tunnel_close(tid)
                asyncio.create_task(ws.send_text(json.dumps({"type": "tunnel_closed", "id": tid})))

        loop.add_reader(fd, readable)
        log(f"consola {tid} aberta (pid {pid}, {cols}x{rows})")

    def console_resize(self, tid: str, cols: int, rows: int) -> None:
        t = self.tunnels.get(tid)
        if t and "pty" in t:
            try:
                fcntl.ioctl(t["pty"], termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))
            except OSError:
                pass

    # ---------------------------------------------------------------- túnel (SSH pelo relay)
    def tunnel_open(self, ws: WebSocket, tid: str, port: int) -> None:
        if port not in TUNNEL_PORTS or len(tid) != TUNNEL_ID_LEN or tid in self.tunnels:
            log(f"túnel {tid} para o porto {port} recusado")
            asyncio.create_task(ws.send_text(json.dumps({"type": "tunnel_closed", "id": tid})))
            return
        t = {"writer": None, "pending": []}
        self.tunnels[tid] = t
        t["task"] = asyncio.create_task(self.tunnel_run(ws, tid, port, t))

    async def tunnel_run(self, ws: WebSocket, tid: str, port: int, t: dict) -> None:
        log(f"túnel {tid} aberto -> 127.0.0.1:{port}")
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            t["writer"] = writer
            for chunk in t.pop("pending"):
                writer.write(chunk)
            await writer.drain()
            while True:
                chunk = await reader.read(32768)
                if not chunk:
                    break
                await ws.send_binary(tid.encode() + chunk)
        except Exception as e:
            log(f"túnel {tid}: {type(e).__name__}: {e}")
        finally:
            if self.tunnels.pop(tid, None) is not None:
                try:
                    await ws.send_text(json.dumps({"type": "tunnel_closed", "id": tid}))
                except Exception:
                    pass
            if writer is not None:
                writer.close()
            log(f"túnel {tid} fechado")

    def tunnel_data(self, tid: str, payload: bytes) -> None:
        t = self.tunnels.get(tid)
        if t is None:
            return
        if "pty" in t:
            try:
                os.write(t["pty"], payload)
            except BlockingIOError:
                asyncio.get_running_loop().call_later(0.01, self.tunnel_data, tid, payload)
            except OSError:
                pass
            return
        if t["writer"] is None:
            t["pending"].append(payload)
        else:
            t["writer"].write(payload)

    def tunnel_close(self, tid: str) -> None:
        t = self.tunnels.pop(tid, None)
        if t is None:
            return
        if "pty" in t:
            try:
                asyncio.get_running_loop().remove_reader(t["pty"])
            except Exception:
                pass
            try:
                os.close(t["pty"])
            except OSError:
                pass
            try:
                os.killpg(t["pid"], signal.SIGHUP)       # a shell é líder de sessão (pty.fork)
            except OSError:
                pass
            threading.Thread(target=os.waitpid, args=(t["pid"], 0), daemon=True).start()   # sem zombies
            log(f"consola {tid} fechada")
            return
        if t["writer"] is not None:
            t["writer"].close()
        if t["task"] is not None:
            t["task"].cancel()

    def close_all_tunnels(self) -> None:
        for tid in list(self.tunnels):
            self.tunnel_close(tid)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            ws = None
            try:
                ws = await WebSocket.connect(RELAY_URL)
                await ws.send_text(json.dumps({"type": "hello", "robot": ROBOT, "token": TOKEN}))
                self.connected, backoff = True, 1.0
                log("ligado ao relay")
                tasks = [asyncio.create_task(self.sender(ws)), asyncio.create_task(self.receiver(ws))]
                done, rest = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for t in rest:
                    t.cancel()
                for t in done:
                    t.result()
            except Exception as e:
                log(f"ligação ao relay: {type(e).__name__}: {e}")
            finally:
                self.connected = False
                self.pending.clear()
                self.close_all_tunnels()
                if ws:
                    ws.close()
                if self.node:
                    self.node.set_wanted([])
                    self.node.stop("ligação ao relay perdida")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def start(self) -> None:
        def main():
            asyncio.set_event_loop(self.loop)
            self.wake = asyncio.Event()
            self.loop.run_until_complete(self.run())
        threading.Thread(target=main, daemon=True, name="relay-link").start()


def main():
    rclpy.init()
    link = Link()
    node = Agent(link.outbox)
    link.node = node
    link.start()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.stop("a terminar")          # pode falhar se o ROS já fechou (SIGTERM)
        except Exception:
            pass
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
