"""Device-flow GitHub: выдаёт user_code (для тебя) и сохраняет токен со scope workflow."""
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLIENT_ID = "178c6fc778ccc68e1d6a"  # публичный client_id GitHub CLI
SCOPES = "repo,gist,read:org,delete_repo,workflow"

BASE = pathlib.Path(r"C:\1C_dev\tg-opencode-bridge")
STATE = BASE / ".device.json"
TOKEN = BASE / ".token"


def post(url, data):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json", "User-Agent": "device-flow"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    try:
        start = post("https://github.com/login/device/code", {"client_id": CLIENT_ID, "scope": SCOPES})
    except Exception as exc:
        print("REQUEST FAILED:", type(exc).__name__, exc, flush=True)
        return
    if "device_code" not in start:
        print("UNEXPECTED:", json.dumps(start)[:400], flush=True)
        return

    device_code = start["device_code"]
    user_code = start["user_code"]
    uri = start.get("verification_uri", "https://github.com/login/device")
    interval = max(int(start.get("interval", 5)), 5)
    expires = int(start.get("expires_in", 900))

    STATE.write_text(
        json.dumps({"user_code": user_code, "verification_uri": uri}, ensure_ascii=False),
        encoding="utf-8",
    )
    print("USER_CODE:", user_code, flush=True)
    print("URL:", uri, flush=True)

    deadline = time.time() + expires
    while time.time() < deadline:
        time.sleep(interval)
        try:
            res = post(
                "https://github.com/login/oauth/access_token",
                {
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except Exception as exc:
            print("poll error:", type(exc).__name__, flush=True)
            continue
        if "access_token" in res:
            TOKEN.write_text(res["access_token"], encoding="utf-8")
            print("READY: token saved | scopes =", res.get("scope"), flush=True)
            return
        err = res.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print("STOP:", err, res.get("error_description", ""), flush=True)
        return
    print("EXPIRED", flush=True)


if __name__ == "__main__":
    main()
