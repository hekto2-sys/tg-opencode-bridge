"""Быстрая проверка релея: getMe + тестовое сообщение."""
import json
import pathlib
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))


def call(method, **params):
    body = json.dumps({"method": method, "params": params}).encode()
    req = urllib.request.Request(
        cfg["relay_url"],
        data=body,
        headers={"x-relay-key": cfg["relay_key"], "content-type": "application/json", "user-agent": "tg-bridge/1.0"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


me = call("getMe")
print("getMe ok:", me.get("ok"), "|", json.dumps(me.get("result", {}), ensure_ascii=False)[:200])

sent = call(
    "sendMessage",
    chat_id=cfg["allowed_user_id"],
    text="🟢 Релей Telegram работает.\nЭто сообщение прошло путь: твой ПК → Cloudflare → Telegram.",
)
print("sendMessage ok:", sent.get("ok"))
