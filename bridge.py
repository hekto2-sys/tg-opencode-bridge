# -*- coding: utf-8 -*-
"""Мост Telegram <-> OpenCode.

Живёт на ПК рядом с OpenCode:
  * забирает сообщения из Telegram через релей (Cloudflare Worker);
  * передаёт их в локальный сервер OpenCode (127.0.0.1);
  * возвращает ответы и запросы прав обратно в Telegram.

Конфиг: config.json рядом со скриптом.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "bridge.log"
LOCK_PORT = 47811

TG_LIMIT = 3800          # длина куска сообщения (лимит Telegram — 4096)
VOICE_TTS_LIMIT = 700    # сколько символов ответа озвучивать голосом
POLL_INTERVAL = 2.5      # как часто проверять ответ агента
STATUS_EVERY = 15        # как часто обновлять сообщение "работаю"
PROMPT_TIMEOUT = 3600    # максимум ожидания ответа, с

log_handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
if sys.stdout is not None:  # при запуске без окна (pythonw) stdout отсутствует
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=log_handlers,
)
log = logging.getLogger("bridge")


# ---------------------------------------------------------------- http

_TLS12 = None


def _tls12_context():
    """Запасной вариант: некоторые провайдеры рубят TLS 1.3 — тогда идём на 1.2."""
    global _TLS12
    if _TLS12 is None:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        _TLS12 = ctx
    return _TLS12


def http_json(url, *, method="GET", body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {
        "Content-Type": "application/json",
        "User-Agent": "tg-bridge/1.0",  # без него Cloudflare отвечает 403
        **(headers or {}),
    }
    modes = [None, "tls12"] if url.startswith("https") else [None]
    last_exc = None
    for mode in modes:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            kwargs = {"timeout": timeout}
            if mode == "tls12":
                kwargs["context"] = _tls12_context()
            with urllib.request.urlopen(req, **kwargs) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except ssl.SSLError as exc:
            last_exc = exc
            log.warning("TLS-ошибка (%s): %s", mode or "по умолчанию", exc)
            continue
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw[:400]}
            return exc.code, parsed
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            break
    raise RuntimeError(f"http error: {last_exc}")


# ---------------------------------------------------------------- telegram (через релей)

class Tg:
    def __init__(self, url: str, key: str):
        self.url = url
        self.key = key

    def call(self, method: str, http_timeout: float = 45, **params):
        for attempt in range(3):
            try:
                _st, data = http_json(
                    self.url,
                    method="POST",
                    body={"method": method, "params": params},
                    headers={"x-relay-key": self.key},
                    timeout=http_timeout,
                )
                if data and data.get("ok"):
                    return data.get("result")
                desc = (data or {}).get("description", "?")
                if desc != "message is not modified":
                    log.warning("telegram %s -> %s", method, desc)
                return None
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram %s: ошибка %s", method, exc)
                time.sleep(2 + attempt * 3)
        return None

    def send(self, chat_id: int, text: str, **extra):
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        payload.update(extra)
        return self.call("sendMessage", **payload)

    def send_long(self, chat_id: int, text: str):
        text = text.strip() or "(пусто)"
        while text:
            if len(text) <= TG_LIMIT:
                self.send(chat_id, text)
                return
            cut = text.rfind("\n", 0, TG_LIMIT)
            if cut < TG_LIMIT // 2:
                cut = TG_LIMIT
            self.send(chat_id, text[:cut])
            text = text[cut:].lstrip("\n")

    def download(self, file_path: str, http_timeout: float = 300) -> bytes:
        """Скачивает файл из Telegram — через релей (сам api.telegram.org заблокирован)."""
        return self.post_raw({"method": "downloadFile", "params": {"file_path": file_path}}, http_timeout)

    def post_raw(self, payload: dict, http_timeout: float = 300) -> bytes:
        """Отправляет произвольный запрос в релей и возвращает сырой ответ."""
        body = json.dumps(payload).encode()
        modes = [None, "tls12"] if self.url.startswith("https") else [None]
        last_exc = None
        for mode in modes:
            req = urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "tg-bridge/1.0",
                    "x-relay-key": self.key,
                },
            )
            try:
                kwargs = {"timeout": http_timeout}
                if mode == "tls12":
                    kwargs["context"] = _tls12_context()
                with urllib.request.urlopen(req, **kwargs) as resp:
                    return resp.read()
            except ssl.SSLError as exc:
                last_exc = exc
                log.warning("TLS-ошибка (%s): %s", mode or "по умолчанию", exc)
                continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                break
        raise RuntimeError(f"post error: {last_exc}")

    def send_voice(self, chat_id: int, path: Path, caption: str = "") -> bool:
        """Отправляет голосовое сообщение (ogg/opus) через релей."""
        payload = {
            "method": "sendVoice",
            "params": {"chat_id": chat_id, **({"caption": caption[:1000]} if caption else {})},
            "files": [
                {
                    "field": "voice",
                    "name": path.name,
                    "data_base64": base64.b64encode(path.read_bytes()).decode(),
                }
            ],
        }
        try:
            data = json.loads(self.post_raw(payload).decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            log.warning("sendVoice: %s", exc)
            return False
        if not data.get("ok"):
            log.warning("sendVoice -> %s", json.dumps(data, ensure_ascii=False)[:200])
        return bool(data.get("ok"))


# ---------------------------------------------------------------- opencode

class Opc:
    def __init__(self, base: str, password: str = ""):
        self.base = base.rstrip("/")
        self.auth = (
            "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
            if password
            else None
        )

    def call(self, method: str, path: str, body=None, timeout: float = 60):
        headers = {"Authorization": self.auth} if self.auth else {}
        status, data = http_json(
            self.base + path, method=method, body=body, headers=headers, timeout=timeout
        )
        if status >= 400:
            raise RuntimeError(
                f"{method} {path} -> {status}: {json.dumps(data, ensure_ascii=False)[:300]}"
            )
        return data

    def create_session(self, directory: str, title: str) -> str:
        data = self.call("POST", "/api/session", {"title": title, "location": {"directory": directory}})
        return data["data"]["id"]

    def session(self, sid: str) -> dict:
        return self.call("GET", f"/api/session/{sid}")["data"]

    def prompt(self, sid: str, text: str):
        return self.call("POST", f"/api/session/{sid}/prompt", {"text": text})

    def messages(self, sid: str, limit: int = 10) -> list:
        data = self.call("GET", f"/api/session/{sid}/message?limit={limit}&order=desc")
        return data.get("data", [])

    def permissions(self, sid: str) -> list:
        data = None
        try:
            data = self.call("GET", f"/api/session/{sid}/permission")
        except Exception:  # noqa: BLE001
            pass
        items = None
        if isinstance(data, dict):
            inner = data.get("data", data)
            if isinstance(inner, list):
                items = inner
            elif isinstance(inner, dict):
                for key in ("data", "requests", "permissions"):
                    if isinstance(inner.get(key), list):
                        items = inner[key]
                        break
        if items is None:
            data = self.call("GET", "/api/permission/request")
            items = data.get("data", [])
        return [x for x in items if isinstance(x, dict) and x.get("sessionID") in (None, sid, "")]

    def reply_permission(self, sid: str, request_id: str, decision: str):
        return self.call(
            "POST", f"/api/session/{sid}/permission/{request_id}/reply", {"decision": decision}
        )

    def interrupt(self, sid: str):
        return self.call("POST", f"/api/session/{sid}/interrupt")


# ---------------------------------------------------------------- утилиты

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def chunks_text(msgs: list) -> str:
    """Собирает текст ответа из новых сообщений агента (по порядку)."""
    parts = []
    for msg in reversed(msgs):
        if msg.get("type") != "assistant":
            continue
        for part in msg.get("content", []):
            if part.get("type") == "text" and (part.get("text") or "").strip():
                parts.append(part["text"].strip())
    return "\n\n".join(parts)


def tool_summary(msg: dict) -> str:
    names = []
    for part in msg.get("content", []):
        if part.get("type") == "tool" and part.get("name"):
            names.append(part["name"])
    return ", ".join(names[:6])


def fmt_seconds(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} с"
    return f"{sec // 60} мин {sec % 60} с"


# ---------------------------------------------------------------- распознавание речи

_STT_MODEL = None
_STT_NAME = ""
_STT_LOCK = threading.Lock()


def get_stt(model_name: str):
    """Модель распознавания речи грузится один раз и остаётся в памяти."""
    global _STT_MODEL, _STT_NAME
    with _STT_LOCK:
        if _STT_MODEL is None:
            from faster_whisper import WhisperModel  # тяжёлый импорт — только под голосовые

            log.info("загружаю модель распознавания «%s» (первый раз качает ~%s МБ)", model_name, "500")
            _STT_MODEL = WhisperModel(model_name, device="cpu", compute_type="int8")
            _STT_NAME = model_name
            log.info("модель распознавания «%s» готова", model_name)
        return _STT_MODEL


def transcribe(data: bytes, model_name: str = "small", language: str = "ru") -> str:
    model = get_stt(model_name)
    tmp = tempfile.NamedTemporaryFile(suffix=".oga", delete=False)
    tmp.write(data)
    tmp.close()
    try:
        segments, info = model.transcribe(
            tmp.name, language=language or None, vad_filter=True, beam_size=1
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info("распознано %.1f с аудио -> %s символов", info.duration or 0, len(text))
        return text
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ---------------------------------------------------------------- озвучка ответов

def clean_for_speech(text: str, limit: int) -> str:
    """Готовит ответ к озвучке: без разметки, кода и лишних пустых строк."""
    text = re.sub(r"```.*?```", " Код пропущен. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]*)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}[#>\-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "… Дальше — текстом."
    return text


def _as_list(value) -> list:
    """Ресемплер в разных версиях PyAV отдаёт то кадр, то список кадров."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def tts_ogg(text: str, voice_hint: str = "", rate: int = 0) -> Path:
    """Озвучивает текст голосом Windows и отдаёт ogg/opus — формат голосовых Telegram."""
    import av
    import win32com.client

    sapi = win32com.client.Dispatch("SAPI.SpVoice")
    voices = sapi.GetVoices()
    hint = (voice_hint or "").lower()
    chosen = None
    for i in range(voices.Count):  # сначала ищем голос по подсказке из конфига
        if hint and hint in voices.Item(i).GetDescription().lower():
            chosen = voices.Item(i)
            break
    if chosen is None and not hint:  # иначе — первый русский
        for i in range(voices.Count):
            low = voices.Item(i).GetDescription().lower()
            if "russian" in low or "irina" in low:
                chosen = voices.Item(i)
                break
    if chosen is not None:
        sapi.Voice = chosen
    sapi.Rate = max(-10, min(10, rate))

    wav_path = Path(tempfile.gettempdir()) / f"tg-tts-{time.time_ns()}.wav"
    ogg_path = wav_path.with_suffix(".ogg")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(str(wav_path), 3, False)  # 3 = создать файл для записи
    sapi.AudioOutputStream = stream
    sapi.Speak(text)
    stream.Close()

    with av.open(str(wav_path)) as src, av.open(str(ogg_path), "w", format="ogg") as dst:
        codec = "libopus" if "libopus" in getattr(av, "codecs_available", set()) else "opus"
        out = dst.add_stream(codec, rate=48000)
        out.bit_rate = 32000
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        for frame in src.decode(audio=0):
            for ready in _as_list(resampler.resample(frame)):
                ready.pts = None
                for packet in out.encode(ready):
                    dst.mux(packet)
        for ready in _as_list(resampler.resample(None)):
            ready.pts = None
            for packet in out.encode(ready):
                dst.mux(packet)
        for packet in out.encode(None):
            dst.mux(packet)
    try:
        wav_path.unlink()
    except OSError:
        pass
    log.info("озвучка готова: %s байт", ogg_path.stat().st_size)
    return ogg_path


def single_instance() -> socket.socket | None:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return s
    except OSError:
        return None


# ---------------------------------------------------------------- мост

HELP = (
    "Я — твой агент OpenCode на компьютере. Пиши задачу текстом или наговаривай голосом —\n"
    "голосовые я расшифрую сам и, если хочешь, отвечу тоже голосом.\n\n"
    "Команды:\n"
    "/new — начать новую сессию (чистый контекст)\n"
    "/status — что сейчас происходит\n"
    "/stop — прервать текущую задачу\n"
    "/work <папка> — новая сессия в указанной папке\n"
    "/voice on|off|auto — отвечать голосом (сейчас: auto — на голосовые)\n"
    "/help — эта справка\n\n"
    "Когда я спрашиваю разрешение на действие — пришлю кнопки «Разрешить / Всегда / Отклонить»."
)


class Bridge:
    def __init__(self, cfg: dict, tg: Tg, opc: Opc):
        self.cfg = cfg
        self.tg = tg
        self.opc = opc
        self.allowed = int(cfg["allowed_user_id"])
        self.state = load_json(STATE_PATH, {"update_id": None, "session_id": None})
        self.lock = threading.RLock()
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.seen_perms: set = set()
        self.perm_session: dict = {}
        self.stop_flag = False

    # ---------- состояние

    def save_state(self):
        with self.lock:
            STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def ensure_session(self) -> str:
        with self.lock:
            sid = self.state.get("session_id")
        if sid:
            try:
                self.opc.session(sid)
                return sid
            except Exception:  # noqa: BLE001
                log.warning("сессия %s больше недоступна", sid)
        return self.new_session(self.cfg["workdir"])

    def new_session(self, directory: str) -> str:
        sid = self.opc.create_session(directory, self.cfg.get("title", "Telegram"))
        with self.lock:
            self.state["session_id"] = sid
        self.save_state()
        log.info("создана сессия %s (%s)", sid, directory)
        return sid

    # ---------- приём обновлений

    def init_offset(self):
        """На первом запуске пропускаем всё, что бот получил до нас."""
        with self.lock:
            if self.state.get("update_id") is not None:
                return
        updates = self.tg.call("getUpdates", http_timeout=20, offset=-1, timeout=0) or []
        last = updates[-1]["update_id"] + 1 if updates else 0
        with self.lock:
            self.state["update_id"] = last
        self.save_state()
        log.info("стартовый offset=%s (пропускаю старые сообщения)", last)

    def poll_loop(self):
        self.init_offset()
        while not self.stop_flag:
            try:
                offset = self.state.get("update_id", 0)
                updates = self.tg.call(
                    "getUpdates",
                    http_timeout=35,
                    offset=offset,
                    timeout=20,
                    allowed_updates=["message", "callback_query"],
                )
                for upd in updates or []:
                    with self.lock:
                        self.state["update_id"] = upd["update_id"] + 1
                    try:
                        self.handle_update(upd)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("ошибка обработки обновления: %s", exc)
                if updates:
                    self.save_state()
            except Exception as exc:  # noqa: BLE001
                log.warning("цикл опроса: %s", exc)
                time.sleep(3)

    def handle_update(self, upd: dict):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if cq.get("from", {}).get("id") == self.allowed:
                self.handle_callback(cq)
            return

        msg = upd.get("message") or upd.get("edited_message") or {}
        sender = (msg.get("from") or {}).get("id")
        if sender != self.allowed:
            log.info("сообщение от чужого id=%s проигнорировано", sender)
            return
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        text = (msg.get("text") or "").strip()
        if not text:
            voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
            if voice and voice.get("file_id"):
                self.q.put({"kind": "voice", "file_id": voice["file_id"], "chat_id": chat_id})
                if self.busy:
                    self.tg.send(chat_id, "⏳ Принял, поставлю в очередь после текущей задачи.")
                return
            if msg.get("photo") or msg.get("document"):
                self.tg.send(chat_id, "Картинки и файлы пока не понимаю 🙂 Напиши текстом или голосом.")
                return
            self.tg.send(chat_id, "Пока я умею текст и голосовые сообщения 🙂")
            return
        if text.startswith("/"):
            self.handle_command(text, chat_id)
            return
        self.q.put({"kind": "text", "text": text, "chat_id": chat_id})
        if self.busy:
            self.tg.send(chat_id, "⏳ Принял, поставлю в очередь после текущей задачи.")

    # ---------- команды

    def handle_command(self, text: str, chat_id: int):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lstrip("/").split("@")[0].lower()
        arg = arg.strip()

        if cmd in ("start", "help"):
            self.tg.send(chat_id, HELP)
        elif cmd == "new":
            sid = self.new_session(self.cfg["workdir"])
            self.tg.send(chat_id, f"Начал новую сессию.\nПапка: {self.cfg['workdir']}\nID: {sid}")
        elif cmd == "work":
            if not arg:
                self.tg.send(chat_id, "Напиши так: /work C:\\путь\\к\\папке")
                return
            try:
                sid = self.new_session(arg)
                self.tg.send(chat_id, f"Готово, работаю в:\n{arg}\nID: {sid}")
            except Exception as exc:  # noqa: BLE001
                self.tg.send(chat_id, f"Не получилось открыть папку:\n{exc}")
        elif cmd == "voice":
            arg = arg.lower()
            if arg in ("on", "always", "вкл", "да"):
                mode = "on"
            elif arg in ("off", "never", "выкл", "нет"):
                mode = "off"
            elif arg in ("auto", ""):
                mode = "auto"
            else:
                self.tg.send(chat_id, "Напиши так: /voice on | off | auto")
                return
            with self.lock:
                self.state["voice_reply"] = mode
            self.save_state()
            self.tg.send(
                chat_id,
                f"Голосовые ответы: {mode}\n"
                "(auto — отвечаю голосом на голосовые, on — всегда, off — только текстом)",
            )
        elif cmd == "status":
            self.tg.send(chat_id, self.status_text())
        elif cmd == "stop":
            sid = self.ensure_session()
            try:
                self.opc.interrupt(sid)
                self.tg.send(chat_id, "⛔️ Попросил остановиться.")
            except Exception as exc:  # noqa: BLE001
                self.tg.send(chat_id, f"Не получилось прервать: {exc}")
        else:
            self.tg.send(chat_id, "Не знаю такую команду. /help — список.")

    def status_text(self) -> str:
        sid = self.state.get("session_id")
        lines = [f"Сессия: {sid or 'ещё нет'}"]
        if sid:
            try:
                info = self.opc.session(sid)
                loc = (info.get("location") or {}).get("directory")
                lines.append(f"Папка: {loc}")
                lines.append(f"Заголовок: {info.get('title')}")
            except Exception:  # noqa: BLE001
                lines.append("Сервер не отвечает по этой сессии")
        lines.append("Сейчас: " + ("работаю над задачей" if self.busy else "свободен"))
        lines.append(f"Очередь: {self.q.qsize()}")
        return "\n".join(lines)

    # ---------- права доступа

    def check_permissions(self, sid: str, chat_id: int):
        if not sid:
            return
        try:
            reqs = self.opc.permissions(sid)
        except Exception as exc:  # noqa: BLE001
            log.warning("не смог получить запросы прав: %s", exc)
            return
        for req in reqs:
            rid = req.get("id")
            with self.lock:
                if not rid or rid in self.seen_perms:
                    continue
                self.seen_perms.add(rid)
                self.perm_session[rid] = req.get("sessionID") or sid
            text = ["🔐 Нужно разрешение"]
            if req.get("action"):
                text.append(f"Действие: {req['action']}")
            res = req.get("resources") or []
            if res:
                text.append("Объект: " + ", ".join(str(x) for x in res[:4])[:300])
            if req.get("message"):
                text.append(str(req["message"])[:300])
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Разрешить", "callback_data": f"perm:{rid}:once"},
                        {"text": "🔁 Всегда", "callback_data": f"perm:{rid}:always"},
                        {"text": "⛔️ Отклонить", "callback_data": f"perm:{rid}:reject"},
                    ]
                ]
            }
            log.info("запрос прав %s: %s", rid, req.get("action"))
            self.tg.send(chat_id, "\n".join(text), reply_markup=keyboard)

    def handle_callback(self, cq: dict):
        data = cq.get("data") or ""
        self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        if not data.startswith("perm:"):
            return
        _, rid, decision = data.split(":", 2)
        sid = self.perm_session.get(rid) or self.state.get("session_id")
        try:
            self.opc.reply_permission(sid, rid, decision)
            mark = {"once": "✅ Разрешено", "always": "🔁 Разрешено всегда", "reject": "⛔️ Отклонено"}[decision]
        except Exception as exc:  # noqa: BLE001
            mark = f"⚠️ Не получилось: {str(exc)[:120]}"
        msg = cq.get("message") or {}
        if msg.get("message_id"):
            self.tg.call(
                "editMessageText",
                chat_id=msg["chat"]["id"],
                message_id=msg["message_id"],
                text=f"{msg.get('text', '')}\n\n{mark}",
            )
        log.info("решение по правам %s: %s", rid, decision)

    # ---------- выполнение задачи

    def worker_loop(self):
        while not self.stop_flag:
            try:
                item = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if item.get("kind") == "voice":
                    self.run_voice(item)
                else:
                    self.run_prompt(item["text"], item["chat_id"])
            except Exception as exc:  # noqa: BLE001
                log.exception("ошибка выполнения задачи: %s", exc)
                self.tg.send(item["chat_id"], f"⚠️ Ошибка: {str(exc)[:400]}")

    def run_voice(self, item: dict):
        """Голосовое сообщение: скачать через релей -> расшифровать -> выполнить как задачу."""
        chat_id = item["chat_id"]
        status_id = None
        try:
            self.busy = True
            status = self.tg.send(chat_id, "🎤 Слушаю…")
            status_id = (status or {}).get("message_id")
            self.tg.call("sendChatAction", chat_id=chat_id, action="typing")
            info = self.tg.call("getFile", file_id=item["file_id"]) or {}
            path = info.get("file_path")
            if not path:
                raise RuntimeError("Telegram не отдал файл голосового")
            data = self.tg.download(path)
            log.info("получено голосовое: %s байт", len(data))
            text = transcribe(
                data,
                self.cfg.get("stt_model", "small"),
                self.cfg.get("stt_language", "ru"),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог распознать голос: %s", exc)
            if status_id:
                self.tg.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=status_id,
                    text=f"⚠️ Не смог разобрать голос: {str(exc)[:200]}",
                )
            return
        finally:
            self.busy = False

        if status_id:
            self.tg.call("deleteMessage", chat_id=chat_id, message_id=status_id)
        if not text:
            self.tg.send(chat_id, "🎤 В голосовом не разобрал слов. Попробуй ещё раз или напиши текстом.")
            return
        self.tg.send(chat_id, f"🎤 «{text}»")
        self.run_prompt(text, chat_id, by_voice=True)

    def voice_mode(self) -> str:
        return self.state.get("voice_reply") or self.cfg.get("voice_reply", "auto")

    def want_voice(self, by_voice: bool) -> bool:
        mode = self.voice_mode()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return by_voice

    def speak_answer(self, chat_id: int, answer: str):
        """Озвучивает ответ и присылает его голосовым сообщением."""
        try:
            spoken = clean_for_speech(answer, VOICE_TTS_LIMIT)
            if not spoken:
                return
            self.tg.call("sendChatAction", chat_id=chat_id, action="record_voice")
            path = tts_ogg(
                spoken,
                str(self.cfg.get("tts_voice", "")),
                int(self.cfg.get("tts_rate", 0)),
            )
            ok = self.tg.send_voice(chat_id, path)
            log.info("голосовой ответ %s", "отправлен" if ok else "не ушёл")
            try:
                path.unlink()
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог озвучить ответ: %s", exc)

    def run_prompt(self, text: str, chat_id: int, by_voice: bool = False):
        self.busy = True
        sid = self.ensure_session()
        try:
            base_ids = {m["id"] for m in self.opc.messages(sid, 8)}
            status = self.tg.send(chat_id, "⏳ Принял задачу, работаю…")
            status_id = (status or {}).get("message_id")
            self.tg.call("sendChatAction", chat_id=chat_id, action="typing")
            self.opc.prompt(sid, text)

            t0 = time.time()
            last_status = 0.0
            done_streak = 0
            msgs: list = []
            while time.time() - t0 < PROMPT_TIMEOUT:
                time.sleep(POLL_INTERVAL)
                self.check_permissions(sid, chat_id)
                self.tg.call("sendChatAction", chat_id=chat_id, action="typing")
                try:
                    msgs = [m for m in self.opc.messages(sid, 12) if m["id"] not in base_ids]
                except Exception as exc:  # noqa: BLE001
                    log.warning("не смог прочитать сообщения: %s", exc)
                    msgs = []
                if time.time() - last_status >= STATUS_EVERY:
                    last_status = time.time()
                    if status_id:
                        note = f"⏳ Работаю {fmt_seconds(time.time() - t0)}…"
                        tools = tool_summary(msgs[0]) if msgs else ""
                        if tools:
                            note += f"\nинструменты: {tools}"
                        self.tg.call(
                            "editMessageText", chat_id=chat_id, message_id=status_id, text=note
                        )
                if self.finished(msgs):
                    done_streak += 1
                    if done_streak >= 2:
                        break
                else:
                    done_streak = 0

            answer = chunks_text(msgs)
            if not answer:
                tools = tool_summary(msgs[0]) if msgs else ""
                answer = "Выполнил, но текстового ответа нет." + (f"\nИнструменты: {tools}" if tools else "")
            took = fmt_seconds(time.time() - t0)
            if status_id:
                self.tg.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=status_id,
                    text=f"✅ Готово за {took}",
                )
            self.tg.send_long(chat_id, answer)
            log.info("задача выполнена за %s, ответ %s символов", took, len(answer))
            if self.want_voice(by_voice):
                self.speak_answer(chat_id, answer)
        finally:
            self.busy = False

    @staticmethod
    def finished(msgs: list) -> bool:
        if not msgs:
            return False
        last = msgs[0]
        if last.get("type") == "idle":
            return True
        if last.get("type") != "assistant":
            return False
        return bool((last.get("time") or {}).get("completed"))

    # ---------- запуск

    def run(self):
        threading.Thread(target=self.poll_loop, name="poll", daemon=True).start()
        threading.Thread(target=self.worker_loop, name="worker", daemon=True).start()
        log.info("мост запущен (разрешён id=%s)", self.allowed)
        while not self.stop_flag:
            time.sleep(5)


def read_password(opencode_cfg: dict) -> str:
    """Пароль сервера OpenCode: из конфига, из service.json или никакой (если сервер без пароля)."""
    pw = opencode_cfg.get("password")
    if pw:
        return pw
    path = opencode_cfg.get("service_json")
    if path and Path(path).exists():
        return load_json(Path(path), {}).get("password", "") or ""
    return ""


def main():
    guard = single_instance()
    if guard is None:
        log.error("мост уже запущен (порт %s занят) — выхожу", LOCK_PORT)
        return
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        raise SystemExit(f"нет конфига {CONFIG_PATH}")
    tg = Tg(cfg["relay_url"], cfg["relay_key"])
    opc = Opc(cfg["opencode"]["base"], read_password(cfg["opencode"]))
    Bridge(cfg, tg, opc).run()


if __name__ == "__main__":
    main()
