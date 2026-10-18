"""Живой тест: создать сессию, отправить промпт, проследить за сообщениями."""
import base64
import json
import pathlib
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

pw = json.loads(
    pathlib.Path.home().joinpath(".config/opencode/service.json").read_text(encoding="utf-8")
)["password"]
BASE = "http://127.0.0.1:45278"
AUTH = "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()


def call(method, path, body=None, timeout=30):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return resp.status, (json.loads(raw) if raw else None)


st, data = call(
    "POST",
    "/api/session",
    {
        "title": "tg-bridge test",
        "location": {"directory": r"C:\Users\User\AppData\Local\Temp\opencode"},
    },
)
sid = data["data"]["id"]
print("create:", st, "| session:", sid, "| location:", json.dumps(data["data"].get("location"), ensure_ascii=False))

st, data = call("POST", f"/api/session/{sid}/prompt", {"text": "Скажи одним словом: тест"})
print("prompt:", st, json.dumps(data, ensure_ascii=False)[:200])

print("permission list:", json.dumps(call("GET", f"/api/session/{sid}/permission")[1], ensure_ascii=False)[:300])

t0 = time.time()
last = None
while time.time() - t0 < 150:
    time.sleep(2)
    _st, data = call("GET", f"/api/session/{sid}/message?limit=8&order=desc", timeout=20)
    msgs = data["data"]
    ids = [m["id"] for m in msgs]
    if ids != last:
        tail = [
            (m["type"], m.get("finish"), m.get("time", {}).get("completed") is not None, len(m.get("content") or []))
            for m in msgs[:4]
        ]
        print(f"[{time.time() - t0:5.1f}s] tail:", tail)
        last = ids
    newest = msgs[0]
    if newest["type"] == "assistant" and newest.get("time", {}).get("completed"):
        print("--- сырое последнее сообщение (обрезано) ---")
        print(json.dumps(newest, ensure_ascii=False)[:1200])
        print("--- текст ---")
        for part in newest.get("content", []):
            if part.get("type") == "text":
                print("TEXT:", part["text"][:300])
        break
print("SESSION_ID:", sid)
