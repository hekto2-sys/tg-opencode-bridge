/**
 * tg-relay — маленький релей между компьютером и Telegram.
 *
 * Принимает:  POST /tg
 *   заголовок: x-relay-key: <RELAY_KEY>
 *   тело:      {"method": "sendMessage", "params": {...}}
 * Делает запрос в https://api.telegram.org/bot<TG_TOKEN>/<method> и возвращает ответ как есть.
 *
 * Секреты (env): TG_TOKEN, RELAY_KEY
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    if (url.pathname !== "/tg") {
      return new Response("not found", { status: 404 });
    }
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    if (request.headers.get("x-relay-key") !== env.RELAY_KEY) {
      return new Response("forbidden", { status: 403 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    const method = typeof body?.method === "string" ? body.method : "";
    if (!/^[A-Za-z]{1,40}$/.test(method)) {
      return new Response("bad method", { status: 400 });
    }

    const upstream = await fetch(
      `https://api.telegram.org/bot${env.TG_TOKEN}/${method}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body.params ?? {}),
      },
    );

    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  },
};
