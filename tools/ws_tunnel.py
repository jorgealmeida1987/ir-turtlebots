#!/usr/bin/env python3
"""
ProxyCommand do ssh para entrar num robô através do relay (sem cabo, mesmo na eduroam).

  ~/.ssh/config:
    Host robot1
        User ubuntu
        IdentityFile ~/.ssh/id_ed25519
        ProxyCommand <python> <repo>/tools/ws_tunnel.py robot1

  Token: variável TB_ADMIN_TOKEN, ou ADMIN_TOKEN no .env.nomad na raiz do repositório.
  Relay: --url (omissão wss://tb.lexicode-consulting.pt/agent).
Precisa de aiohttp (pip install aiohttp).
"""
import argparse
import asyncio
import os
import sys
import threading
from pathlib import Path

import aiohttp


def load_token() -> str:
    tok = os.environ.get("TB_ADMIN_TOKEN", "")
    env = Path(__file__).resolve().parent.parent / ".env.nomad"
    if not tok and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ADMIN_TOKEN="):
                tok = line.split("=", 1)[1].split("#")[0].strip()
    if not tok:
        sys.exit("ws_tunnel: falta o ADMIN_TOKEN (TB_ADMIN_TOKEN ou .env.nomad)")
    return tok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("robot")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--url", default="wss://tb.lexicode-consulting.pt/agent")
    a = ap.parse_args()

    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[bytes] = asyncio.Queue()

    def stdin_reader():
        while True:
            chunk = os.read(0, 65536)
            loop.call_soon_threadsafe(inbox.put_nowait, chunk)
            if not chunk:
                return

    out = sys.stdout.buffer
    async with aiohttp.ClientSession() as session:
        try:
            ws = await session.ws_connect(a.url, params={"tunnel": a.robot, "port": str(a.port)},
                                          headers={"Authorization": f"Bearer {load_token()}"},
                                          heartbeat=20, max_msg_size=0, compress=0)
        except aiohttp.WSServerHandshakeError as e:
            print(f"ws_tunnel: relay recusou ({e.status} {e.message})", file=sys.stderr)
            return 1
        threading.Thread(target=stdin_reader, daemon=True).start()

        async def up():
            while True:
                chunk = await inbox.get()
                if not chunk:
                    return
                await ws.send_bytes(chunk)

        async def down():
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    out.write(msg.data)
                    out.flush()
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    return

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        await ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
