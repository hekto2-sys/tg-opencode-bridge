"""Разбор openapi.json сервера OpenCode: нужные для моста эндпоинты и схемы."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = json.load(open(r"C:\1C_dev\tg-opencode-bridge\opencode-api.json", encoding="utf-8"))
schemas = spec["components"]["schemas"]

TARGETS = [
    ("/api/session", "post"),
    ("/api/session", "get"),
    ("/api/session/{sessionID}/prompt", "post"),
    ("/api/session/{sessionID}/message", "get"),
    ("/api/session/{sessionID}/interrupt", "post"),
    ("/api/permission/request", "get"),
    ("/api/session/{sessionID}/permission/{requestID}/reply", "post"),
    ("/api/experimental/session/{sessionID}/wait", "post"),
    ("/api/event", "get"),
    ("/api/model", "get"),
    ("/api/session/{sessionID}/command", "post"),
]


def show(sch, limit=500):
    text = json.dumps(sch, ensure_ascii=False)
    return text[:limit] + ("..." if len(text) > limit else "")


for path, method in TARGETS:
    op = spec["paths"].get(path, {}).get(method)
    print(f"--- {method.upper()} {path}")
    if not op:
        print("    NOT FOUND")
        continue
    for p in op.get("parameters", []):
        print("    param:", p.get("name"), f'({p.get("in")})', show(p.get("schema"), 150))
    rb = op.get("requestBody")
    if rb:
        sch = rb.get("content", {}).get("application/json", {}).get("schema", {})
        print("    body:", show(sch))
    for code, resp in op.get("responses", {}).items():
        sch = resp.get("content", {}).get("application/json", {}).get("schema", {})
        print(f"    resp {code}:", show(sch, 250))

print()
print("=== схемы с ключевыми словами ===")
keys = ("prompt", "message", "part", "permission", "sessioninfo", "sessioncreate")
for name in sorted(schemas):
    low = name.lower()
    if any(k in low for k in keys) and len(name) < 40:
        print(f"  {name}: {show(schemas[name], 200)}")
