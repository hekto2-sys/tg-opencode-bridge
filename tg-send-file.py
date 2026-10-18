"""Отправка локального файла в Telegram через релей.

Использование:
    python tg-send-file.py <путь к файлу> ["подпись"]
"""
import base64
import json
import pathlib
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).resolve().parent
cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))


def tg(payload: dict, timeout: float = 180):
    req = urllib.request.Request(
        cfg["relay_url"],
        data=json.dumps(payload).encode(),
        headers={
            "x-relay-key": cfg["relay_key"],
            "content-type": "application/json",
            "user-agent": "tg-bridge/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def send_file(path: pathlib.Path, caption: str = "") -> bool:
    payload = {
        "method": "sendDocument",
        "params": {"chat_id": cfg["allowed_user_id"], "caption": caption},
        "files": [
            {
                "field": "document",
                "name": path.name,
                "data_base64": base64.b64encode(path.read_bytes()).decode(),
            }
        ],
    }
    status, data = tg(payload)
    ok = bool(data.get("ok"))
    print("sendDocument:", status, "ok" if ok else json.dumps(data, ensure_ascii=False)[:300])
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("укажи путь к файлу")
    target = pathlib.Path(sys.argv[1])
    if not target.exists():
        raise SystemExit(f"нет файла {target}")
    send_file(target, sys.argv[2] if len(sys.argv) > 2 else "")
