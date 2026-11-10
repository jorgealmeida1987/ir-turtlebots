#!/usr/bin/env python3
"""
Teste da consola (pty) pelo relay: abre uma shell no robô, muda o tamanho, corre comandos e fecha.
Uso: python tools/console_test.py [robot] [--url ws://127.0.0.1:8080/agent]
Token: TB_ADMIN_TOKEN ou ADMIN_TOKEN no .env.nomad (como o ws_tunnel.py).
"""
import argparse
import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from ws_tunnel import load_token  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("robot", nargs="?", default="robot1")
    ap.add_argument("--url", default="wss://tb.lexicode-consulting.pt/agent")
    a = ap.parse_args()
    out = b""
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(a.url, params={"tunnel": a.robot, "kind": "pty", "cols": "100", "rows": "30"},
                                headers={"Authorization": f"Bearer {load_token()}"}, max_msg_size=0)

        async def read_for(seconds):
            nonlocal out
            try:
                while True:
                    m = await asyncio.wait_for(ws.receive(), timeout=seconds)
                    if m.type == aiohttp.WSMsgType.BINARY:
                        out += m.data
                    elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        return False
            except asyncio.TimeoutError:
                return True

        await read_for(2)                                         # prompt
        await ws.send_str(json.dumps({"type": "resize", "cols": 132, "rows": 40}))
        await ws.send_bytes(b"echo CONSOLA-OK $(whoami)@$(hostname); stty size; echo $TERM\n")
        await read_for(2)
        await ws.send_bytes(b"exit\n")
        closed_by_robot = not await read_for(3)
        await ws.close()

    text = out.decode(errors="replace")
    print(text[-400:])
    ok = "CONSOLA-OK ubuntu@turtlebot4" in text and "40 132" in text and "xterm-256color" in text
    print("\nRESULTADO:", "OK" if ok else "FALHOU", "| fecho pelo robô após exit:", closed_by_robot)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
