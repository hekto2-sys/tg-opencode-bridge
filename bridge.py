"""Проверка связки: GitHub Actions -> Telegram + туннель до OpenCode на ПК."""
import base64
import json
import os
import urllib.error
import urllib.request

TG_TOKEN = os.environ["TG_TOKEN"]
TG_USER_ID = int(os.environ["TG_USER_ID"])
OPC_URL = os.environ["OPC_URL"].rstrip("/")
OPC_AUTH = base64.b64encode(
    f'{os.environ["OPC_USER"]}:{os.environ["OPC_PASS"]}'.encode()
).decode()


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def tg(method, **params):
    _status, text = http(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json.dumps(params).encode(),
        {"Content-Type": "application/json"},
    )
    return json.loads(text)


def check_telegram():
    try:
        me = tg("getMe")
    except Exception as exc:
        print("Telegram: FAILED with", type(exc).__name__)
        return False
    if not me.get("ok"):
        print("Telegram: FAILED", me.get("description"))
        return False
    bot = me["result"]
    print(f"Telegram: OK, bot=@{bot.get('username')} id={bot.get('id')}")
    return True


def check_tunnel():
    try:
        status, text = http(
            OPC_URL + "/api/info", headers={"Authorization": "Basic " + OPC_AUTH}
        )
    except urllib.error.HTTPError as exc:
        print("OpenCode tunnel: HTTP", exc.code)
        return False
    except Exception as exc:
        print("OpenCode tunnel: error", type(exc).__name__)
        return False
    data = json.loads(text)
    print("OpenCode tunnel: OK, status", status, "| fields:", ",".join(sorted(data)[:6]))
    return True


def main():
    tg_ok = check_telegram()
    tunnel_ok = check_tunnel()
    lines = [
        "Первый шаг: связь проверена.",
        "",
        "GitHub -> Telegram: " + ("работает" if tg_ok else "НЕ работает"),
        "GitHub -> сервер на ПК: " + ("работает" if tunnel_ok else "НЕ работает"),
        "",
        "Это тестовое сообщение от бота. Следующим шагом подключу сюда OpenCode,",
        "и ты сможешь писать мне задачи прямо из Telegram.",
    ]
    result = tg("sendMessage", chat_id=TG_USER_ID, text="\n".join(lines))
    print("sendMessage:", "OK" if result.get("ok") else result.get("description"))
    if not (tg_ok and tunnel_ok):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
