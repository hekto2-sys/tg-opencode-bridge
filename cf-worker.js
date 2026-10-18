/**
 * tg-relay — релей между компьютерами и Telegram.
 *
 * Маршруты:
 *   POST /tg            -> бот «home»  (секреты TG_TOKEN / RELAY_KEY)
 *   POST /tg/<bot>      -> бот <bot>   (секреты TG_TOKEN_<BOT> / RELAY_KEY_<BOT>)
 *   GET  /health        -> "ok"
 *
 * Тело запроса (обычный вызов):
 *   {"method": "sendMessage", "params": {...}}
 * Тело запроса (отправка файла в Telegram):
 *   {"method": "sendDocument", "params": {...},
 *    "files": [{"field": "document", "name": "x.txt", "data_base64": "..."}]}
 * Тело запроса (скачать файл из Telegram):
 *   {"method": "downloadFile", "params": {"file_path": "voice/file_1.oga"}}
 *   -> ответ: сырые байты файла
 *
 * Заголовок: x-relay-key: <ключ этого бота>
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    const match = url.pathname.match(/^\/tg(?:\/([A-Za-z0-9_-]{1,16}))?\/?$/);
    if (!match) {
      return new Response("not found", { status: 404 });
    }
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    const name = (match[1] || "home").toUpperCase();
    const token = env[`TG_TOKEN_${name}`] || (name === "HOME" ? env.TG_TOKEN : undefined);
    const key = env[`RELAY_KEY_${name}`] || (name === "HOME" ? env.RELAY_KEY : undefined);
    if (!token || !key) {
      return new Response(`unknown bot: ${name.toLowerCase()}`, { status: 404 });
    }
    if (request.headers.get("x-relay-key") !== key) {
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

    const params = body.params ?? {};

    // Скачивание файла из Telegram (голосовые, фото, документы)
    if (method === "downloadFile") {
      const filePath = String(params.file_path || "");
      if (!filePath || filePath.includes("..")) {
        return new Response("bad file_path", { status: 400 });
      }
      const upstream = await fetch(
        `https://api.telegram.org/file/bot${token}/${filePath}`
      );
      return new Response(upstream.body, {
        status: upstream.status,
        headers: {
          "content-type":
            upstream.headers.get("content-type") || "application/octet-stream",
        },
      });
    }

    const target = `https://api.telegram.org/bot${token}/${method}`;
    const files = Array.isArray(body.files) ? body.files : [];

    let upstream;
    if (files.length > 0) {
      // multipart/form-data — для отправки файлов, картинок и т.п.
      const form = new FormData();
      for (const [field, value] of Object.entries(params)) {
        form.append(field, typeof value === "object" ? JSON.stringify(value) : String(value));
      }
      for (const file of files) {
        const bytes = Uint8Array.from(atob(file.data_base64 ?? ""), (ch) => ch.charCodeAt(0));
        form.append(file.field || "document", new Blob([bytes]), file.name || "file.bin");
      }
      upstream = await fetch(target, { method: "POST", body: form });
    } else {
      upstream = await fetch(target, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(params),
      });
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  },
};
