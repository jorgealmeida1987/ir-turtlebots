#!/usr/bin/env python3
"""
Teste ponta-a-ponta do relay do lado do aluno (o mesmo fluxo que matlab/TurtleBot.m).
Uso: python tools/student_test.py [BASE_URL] [robot] [chave]
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
ROBOT = sys.argv[2] if len(sys.argv) > 2 else "robot1"
KEY = sys.argv[3] if len(sys.argv) > 3 else ""


def call(method, path, body=None, query=None):
    url = f"{BASE}/api/{ROBOT}/{path}"
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw[:200].decode(errors="replace")}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    return cond


ok = True
st, a = call("POST", "connect", {"user": "aluno_A", "key": KEY})
ok &= check("A connect", st == 200 and a["controller"], a)
sa = a["session"]

# scans com long-poll (como readline): devem chegar sempre novos, seq a subir
seq, t0, sizes, n = -1, time.time(), [], 10
for _ in range(n):
    st, s = call("GET", "scan", query={"session": sa, "after": seq})
    if st != 200:
        ok &= check("scan", False, (st, s))
        break
    assert s["seq"] > seq
    seq = s["seq"]
    sizes.append(len(json.dumps(s)))
dt = time.time() - t0
ok &= check("scan formato antigo", all(k in s for k in ("stamp", "ranges", "angles")) and len(s["ranges"]) == len(s["angles"]),
            f"{len(s['ranges'])} pontos, {n / dt:.1f} scans/s, ~{sum(sizes) / len(sizes) / 1024:.0f} kB/scan")
ok &= check("inf preservado", any(r == float("inf") for r in s["ranges"]))

st, b = call("GET", "battery_state", query={"session": sa})
ok &= check("outro tópico (battery_state)", st == 200 and abs(b.get("percentage", 0) - 0.87) < 1e-3, f"percentage={b.get('percentage')}")

st, r = call("POST", "cmd", {"session": sa, "v": 0.05, "w": 0.2})
ok &= check("A cmd", st == 200, r)

# B liga-se depois -> passa a controlar; A perde o controlo
st, bb = call("POST", "connect", {"user": "aluno_B", "key": KEY})
sb = bb["session"]
st, r = call("POST", "cmd", {"session": sa, "v": 0.1, "w": 0})
ok &= check("A perdeu o controlo (409)", st == 409 and r.get("controller") == "aluno_B", r)
st, r = call("POST", "cmd", {"session": sb, "v": 0.1, "w": 0})
ok &= check("B controla", st == 200, r)

st, status = call("GET", "status")
ok &= check("status", status["controller"] == "aluno_B" and len(status["users"]) == 2,
            f"online={status['online']} streaming={status['topics_streaming']}")

st, r = call("POST", "cmd", {"session": "invalida", "v": 0, "w": 0})
ok &= check("sessão inválida (401)", st == 401)

call("POST", "disconnect", {"session": sb})
call("POST", "disconnect", {"session": sa})
st, status = call("GET", "status")
ok &= check("disconnect", status["controller"] is None and not status["users"])

print("\nRESULTADO:", "TUDO OK" if ok else "HÁ FALHAS")
sys.exit(0 if ok else 1)
