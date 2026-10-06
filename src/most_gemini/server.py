"""Мост Gemini: Claude работает с gemini.google.com под входом владельца (подписка Gemini Pro).

Основа — библиотека gemini-webapi (HanaokaYuzu/Gemini-API, закреплённая версия).
Данные входа уходят только в Google: этот файл сам в интернет ходит только к Google
и (по просьбе) скачивает картинку по https-ссылке, которую дал владелец/Claude.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

# --- настройки (всё из /etc/most-gemini.env) ---
STATE = Path(os.environ.get("MOST_GEMINI_STATE", "/var/lib/most-gemini"))
PUBLIC_URL = os.environ.get("MOST_GEMINI_PUBLIC_URL", "http://127.0.0.1:8150").rstrip("/")
PORT = int(os.environ.get("MOST_GEMINI_PORT", "8150"))
SECRET_DIR = STATE / "secret"
COOKIES_FILE = SECRET_DIR / "google-cookies.json"
SECRET_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("GEMINI_COOKIE_PATH", str(SECRET_DIR / "cache"))  # библиотека хранит тут продлённый вход
UPLOAD_TOKEN = os.environ.get("MOST_GEMINI_UPLOAD_TOKEN", "")
VHOD_SECRET = os.environ.get("MOST_GEMINI_VHOD_SECRET", "")
IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-flash")
IMAGE_MODEL_PRO = os.environ.get("GEMINI_IMAGE_MODEL_PRO", "gemini-pro")
GEN_TIMEOUT = float(os.environ.get("GEMINI_GEN_TIMEOUT", "420"))
RELOGIN_HINT = os.environ.get(
    "MOST_GEMINI_RELOGIN_HINT",
    "Вход в Google протух или его нет. Владельцу: обновить вход командой most-gemini-login на сервере "
    "(см. README моста).",
)

import uvicorn  # noqa: E402
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import ImageContent, TextContent, ToolAnnotations  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import FileResponse, JSONResponse, Response  # noqa: E402

from .auth import FileOAuthProvider  # noqa: E402
from .files import FileStore  # noqa: E402
from .guard import Guard, Refused  # noqa: E402
from .watermark import remove_watermark  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("most_gemini")

guard = Guard(STATE / "guard.json")
store = FileStore(STATE, PUBLIC_URL)
oauth = FileOAuthProvider(SECRET_DIR / "oauth.json")
HOST = PUBLIC_URL.split("://", 1)[1]

mcp = FastMCP(
    "most-gemini",
    instructions=(
        "Мост к Gemini (gemini.google.com) под аккаунтом владельца с подпиской Gemini Pro. "
        "Картинки Nano Banana, правка присланных картинок, чаты (новые и прошлые), выбор модели, Gems, "
        "Deep Research. Мост сам выдерживает паузы и дневной лимит — не обходи их и не повторяй запросы "
        "в цикле. Готовые файлы приходят ссылками — скачай их к владельцу (curl -o), если работаешь в "
        "Claude Code. Свою картинку на доработку: сначала отправь её на /upload (см. gemini_usage)."
    ),
    auth_server_provider=oauth,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=PUBLIC_URL + "/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[HOST, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[PUBLIC_URL, "https://claude.ai", "http://127.0.0.1:*", "http://localhost:*"],
    ),
    host="127.0.0.1",
    port=PORT,
)


# ---------------------------------------------------------------------------
# Вход в Google: клиент поднимается при первом запросе, а не при старте моста
# ---------------------------------------------------------------------------
class NoLogin(Exception):
    pass


_client = None
_client_lock = asyncio.Lock()
_login_state = {"ok": None, "checked": 0.0, "error": ""}


async def get_client(fresh: bool = False):
    global _client
    from gemini_webapi import GeminiClient

    async with _client_lock:
        if _client is not None and not fresh:
            return _client
        if _client is not None:
            try:
                await _client.close()
            except Exception:
                pass
            _client = None
        if not COOKIES_FILE.exists():
            _login_state.update(ok=False, checked=time.time(), error="нет файла входа")
            raise NoLogin(RELOGIN_HINT)
        data = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        client = GeminiClient(data.get("__Secure-1PSID"), data.get("__Secure-1PSIDTS"))
        try:
            await client.init(timeout=GEN_TIMEOUT, auto_close=False, auto_refresh=True, watchdog_timeout=90)
        except Exception as e:
            _login_state.update(ok=False, checked=time.time(), error=f"{type(e).__name__}: {e}"[:300])
            log.warning("init failed: %s", e)
            raise NoLogin(RELOGIN_HINT + f" (ответ Google: {type(e).__name__})") from e
        if _is_guest(client):
            # Google не принял куки, и библиотека тихо перешла в гостевой режим: без аккаунта и подписки.
            try:
                await client.close()
            except Exception:
                pass
            _login_state.update(ok=False, checked=time.time(), error="Google не принял вход (гостевой режим)")
            raise NoLogin(RELOGIN_HINT + " (Google не принял вход — открылся гостевой режим)")
        _client = client
        _login_state.update(ok=True, checked=time.time(), error="")
        abuse = getattr(client, "abuse_status", None)
        if abuse:
            log.warning("abuse_status: %s", abuse)
        return client


def _is_guest(client) -> bool:
    return not getattr(client, "access_token", None) or getattr(client, "_cookie_source", "") == "Guest"


def _is_auth_error(e: Exception) -> bool:
    from gemini_webapi import AuthError
    return isinstance(e, AuthError) or "unauthenticated" in str(e).lower()


async def call_gemini(kind: str, fn):
    """Один запрос к Gemini под предохранителем. fn(client) -> результат."""
    guard.check(kind == "image")
    async with guard.lock:
        guard.check(kind == "image")
        await guard.wait_turn(kind)
        client = await get_client()
        try:
            return await asyncio.wait_for(fn(client), timeout=GEN_TIMEOUT)
        except Exception as e:
            if _is_auth_error(e):
                _login_state.update(ok=False, checked=time.time(), error=str(e)[:300])
                client = await get_client(fresh=True)  # один раз перечитать вход с диска
                return await asyncio.wait_for(fn(client), timeout=GEN_TIMEOUT)
            raise
        finally:
            guard.state["last_call"] = time.time()


def _err(e: Exception) -> list:
    if isinstance(e, (Refused, NoLogin)):
        return [TextContent(type="text", text=str(e))]
    if isinstance(e, asyncio.TimeoutError):
        return [TextContent(type="text", text=f"Gemini не ответил за {GEN_TIMEOUT:.0f} с. Повторять сразу не нужно.")]
    log.exception("tool error")
    return [TextContent(type="text", text=f"Ошибка: {type(e).__name__}: {e}"[:1500])]


# ---------------------------------------------------------------------------
# Номер чата: «c_…|r_…|rc_…» (чтобы продолжить ровно с последнего ответа) или просто c_…
# ---------------------------------------------------------------------------
def _conv_id(out) -> str:
    md = [m for m in (getattr(out, "metadata", None) or [])[:3]]
    rcid = getattr(out, "rcid", None)
    if len(md) >= 2 and rcid:
        md = [md[0], md[1], rcid]
    return "|".join(x for x in md if x)


def _parse_conv(conv: str) -> list[str]:
    conv = conv.strip()
    m = re.search(r"gemini\.google\.com/(?:u/\d+/)?app/([0-9a-f]+)", conv)
    if m:
        return ["c_" + m.group(1)]
    parts = [p for p in conv.split("|") if p]
    if parts and not parts[0].startswith("c_") and re.fullmatch(r"[0-9a-f]{8,}", parts[0]):
        parts[0] = "c_" + parts[0]
    return parts


async def _chat_session(client, conv: str | None, model=None, gem=None):
    if not conv:
        return client.start_chat(model=model, gem=gem) if (model or gem) else None
    parts = _parse_conv(conv)
    if len(parts) >= 3:
        return client.start_chat(metadata=parts[:3], model=model, gem=gem)
    hist = await client.read_chat(parts[0], limit=1)  # узнать последний ответ, чтобы продолжить с него
    md = None
    if hist and hist.turns:
        for t in hist.turns:
            if t.model_output is not None:
                md = list(t.model_output.metadata[:2]) + [t.model_output.rcid]
                break
    return client.start_chat(metadata=md or [parts[0]], model=model, gem=gem)


# ---------------------------------------------------------------------------
# Сохранение того, что прислал Gemini
# ---------------------------------------------------------------------------
_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _slug(text: str) -> str:
    """Имя файла латиницей: русские буквы в ссылках ломают скачивание."""
    s = "".join(_TR.get(ch, ch) for ch in text.lower())
    s = re.sub(r"[^0-9a-z]+", "-", s).strip("-")
    return (s[:40].rstrip("-") or "gemini")


def _preview(path: Path) -> ImageContent | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((768, 768))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
        return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")
    except Exception:
        return None


async def _save_outputs(out, prompt: str, previews: bool = True) -> tuple[list[dict], list[ImageContent]]:
    from gemini_webapi.types import GeneratedImage

    files, shots = [], []
    items = list(out.images or [])
    if not items and not (out.videos or out.media):
        return files, shots
    key, d = store.new_slot()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{_slug(prompt)}"
    for i, img in enumerate(items, 1):
        name = f"{base}-{i}.png"
        try:
            saved = Path(await img.save(path=str(d), filename=name))
        except Exception as e:
            log.warning("image save failed: %s", e)
            continue
        info = {"файл": saved.name, "ссылка": store.url(key, saved.name), "путь на сервере": str(saved)}
        if isinstance(img, GeneratedImage):
            if remove_watermark(str(saved)):
                info["значок Gemini"] = "убран"
        else:
            info["откуда"] = "картинка из интернета, не сгенерирована"
        try:
            from PIL import Image
            with Image.open(saved) as im:
                info["размер"] = f"{im.size[0]}×{im.size[1]}"
        except Exception:
            pass
        files.append(info)
        if previews and (p := _preview(saved)):
            shots.append(p)
    for j, media in enumerate([*(out.videos or []), *(out.media or [])], 1):
        before = set(d.iterdir())
        try:
            await media.save(path=str(d), filename=f"{base}-media{j}")
        except Exception as e:
            log.warning("media save failed: %s", e)
        for f in sorted(set(d.iterdir()) - before):
            files.append({"файл": f.name, "ссылка": store.url(key, f.name), "путь на сервере": str(f)})
    if not any(d.iterdir()):
        shutil.rmtree(d, ignore_errors=True)
    return files, shots


async def _inputs(refs: list[str] | None) -> tuple[list[Path], Path | None]:
    if not refs:
        return [], None
    tmp = STATE / "tmp" / secrets.token_hex(6)
    paths = [await store.resolve_input(r, tmp) for r in refs]
    return paths, tmp


def _result(text: str, extra: dict, files: list[dict], shots: list) -> list:
    body = {k: v for k, v in extra.items() if v not in (None, "", [])}
    if files:
        body["файлы"] = files
    head = json.dumps(body, ensure_ascii=False, indent=1)
    parts = [TextContent(type="text", text=(text.strip() + "\n\n" if text and text.strip() else "") + head)]
    return parts + shots


RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

AspectRatio = Literal["1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "4:5", "21:9"]


# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------
@mcp.tool(annotations=RW)
async def gemini_generate_image(
    prompt: Annotated[str, Field(description="Что нарисовать или как изменить присланную картинку. Лучше подробно: предмет, фон, свет, стиль.")],
    aspect_ratio: Annotated[AspectRatio | None, Field(description="Пропорции. Для карточек маркетплейса обычно 3:4 (WB/Ozon) или 1:1.")] = None,
    files: Annotated[list[str] | None, Field(description="Картинки-образцы или картинка на доработку: номера u_… после /upload, ссылки на готовые картинки моста или https-ссылки.")] = None,
    conversation_id: Annotated[str | None, Field(description="Продолжить прошлый разговор с картинкой («сделай фон светлее»): номер из прошлого ответа.")] = None,
    pro: Annotated[bool, Field(description="True — Nano Banana Pro (медленнее, лучше текст и детали, лимит в подписке меньше).")] = False,
) -> list:
    """Нарисовать картинку в Gemini (Nano Banana) или доработать присланную. Сохраняет в полном размере,
    убирает значок Gemini, возвращает ссылки на файлы и превью. Одна картинка за вызов; мост сам выдерживает
    паузу 20–40 с между картинками и дневной лимит."""
    try:
        full = prompt.strip()
        if aspect_ratio:
            full += f"\n\nФормат изображения: {aspect_ratio} (aspect ratio {aspect_ratio})."
        if files:
            full = "Используй приложенное изображение. " + full
        if not re.search(r"(нарису|сгенерир|создай|сделай|generate|draw|create|edit|измени|замени|добавь|убери)", full, re.I):
            full = "Сгенерируй изображение: " + full
        paths, tmp = await _inputs(files)
        model = IMAGE_MODEL_PRO if pro else IMAGE_MODEL

        async def go(client):
            chat = await _chat_session(client, conversation_id, model=model)
            if chat is not None:
                return await chat.send_message(full, files=paths or None)
            return await client.generate_content(full, files=paths or None, model=model)

        t0 = time.monotonic()
        try:
            out = await call_gemini("image", go)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        saved, shots = await _save_outputs(out, prompt)
        made = sum(1 for f in saved if "откуда" not in f and f["файл"].endswith(".png"))
        note = guard.done(made, out.text, wanted_images=True)
        return _result(
            out.text if not saved else "",
            {
                "номер разговора": _conv_id(out),
                "модель": model,
                "секунд": round(time.monotonic() - t0),
                "внимание": note or ("Gemini не прислал картинку — см. текст ответа выше." if not saved else None),
                "осталось картинок сегодня": guard.images_left(),
            },
            saved, shots,
        )
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=RW)
async def gemini_chat(
    prompt: Annotated[str, Field(description="Сообщение для Gemini.")],
    conversation_id: Annotated[str | None, Field(description="Продолжить существующий чат: номер из ответа моста, из gemini_list_chats или ссылка gemini.google.com/app/…")] = None,
    model: Annotated[str | None, Field(description="Модель: gemini-pro, gemini-flash, gemini-flash-lite (список — gemini_list_models). Пусто — модель по умолчанию.")] = None,
    files: Annotated[list[str] | None, Field(description="Приложить файлы: номера u_…, ссылки на файлы моста или https-ссылки.")] = None,
    gem: Annotated[str | None, Field(description="Номер Gem (свой помощник Gemini) — из gemini_list_gems.")] = None,
    temporary: Annotated[bool, Field(description="True — временный чат, не сохранится в истории Gemini.")] = False,
    thinking: Annotated[bool, Field(description="True — расширенное обдумывание (дольше, глубже).")] = False,
) -> list:
    """Написать в Gemini: новый чат или продолжение прошлого (в том числе начатого владельцем в браузере).
    Если Gemini пришлёт картинки, видео или музыку — они сохранятся и придут ссылками."""
    try:
        paths, tmp = await _inputs(files)

        async def go(client):
            chat = await _chat_session(client, conversation_id, model=model, gem=gem)
            if chat is not None:
                return await chat.send_message(prompt, files=paths or None, extended_thinking=thinking)
            return await client.generate_content(
                prompt, files=paths or None, model=model, gem=gem, temporary=temporary, extended_thinking=thinking
            )

        try:
            out = await call_gemini("text", go)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        saved, shots = await _save_outputs(out, prompt)
        guard.done(sum(1 for f in saved if f["файл"].endswith(".png")), out.text, wanted_images=False)
        return _result(out.text, {"номер разговора": _conv_id(out), "модель": model or "по умолчанию"}, saved, shots)
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=RO)
async def gemini_list_chats(
    search: Annotated[str | None, Field(description="Искать по названию чата (без учёта регистра). Искать по словам внутри чатов Gemini не умеет.")] = None,
    limit: Annotated[int, Field(description="Сколько последних чатов просмотреть (до 200).")] = 50,
) -> list:
    """Список прошлых чатов в Gemini: название, номер, дата, закреплён ли. Номер передаётся в
    gemini_read_chat (прочитать) или gemini_chat (продолжить)."""
    try:
        async def go(client):
            await client._fetch_recent_chats(recent=max(1, min(limit, 200)))
            return client.list_chats() or []

        chats = await call_gemini("read", go)
        guard.done(0, "", False)
        rows = []
        for c in chats:
            if search and search.lower() not in (c.title or "").lower():
                continue
            rows.append({
                "номер": c.cid,
                "название": c.title,
                "изменён": datetime.fromtimestamp(c.timestamp).strftime("%d.%m.%Y %H:%M") if c.timestamp else "",
                "закреплён": bool(c.is_pinned),
                "ссылка": f"https://gemini.google.com/app/{c.cid.removeprefix('c_')}",
            })
        return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, indent=1) if rows else "Чатов не найдено.")]
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=RO)
async def gemini_read_chat(
    conversation_id: Annotated[str, Field(description="Номер чата (из gemini_list_chats) или ссылка gemini.google.com/app/…")],
    turns: Annotated[int, Field(description="Сколько последних реплик прочитать (до 100).")] = 20,
    save_images: Annotated[bool, Field(description="Сохранить картинки из чата и дать ссылки.")] = False,
) -> list:
    """Прочитать прошлый чат Gemini: реплики по порядку (сначала старые). Можно сохранить картинки из него."""
    try:
        cid = _parse_conv(conversation_id)[0]
        hist = await call_gemini("read", lambda c: c.read_chat(cid, limit=max(1, min(turns, 100))))
        guard.done(0, "", False)
        if not hist or not hist.turns:
            return [TextContent(type="text", text="Чат пуст или не найден.")]
        lines, files = [], []
        for t in reversed(hist.turns):
            who = "Владелец" if t.role == "user" else "Gemini"
            lines.append(f"### {who}\n{t.text}")
            if save_images and t.model_output is not None and t.model_output.images:
                got, _ = await _save_outputs(t.model_output, t.text or "chat", previews=False)
                files += got
        text = f"Чат {cid}\n\n" + "\n\n".join(lines)
        if files:
            text += "\n\nКартинки из чата:\n" + json.dumps(files, ensure_ascii=False, indent=1)
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def gemini_delete_chat(
    conversation_id: Annotated[str, Field(description="Номер чата.")],
    confirm: Annotated[bool, Field(description="Удалить по-настоящему. Без этого — только показать, что будет удалено.")] = False,
) -> list:
    """Удалить чат из истории Gemini. Необратимо: вызывать с confirm=true только после явного «да» владельца."""
    try:
        cid = _parse_conv(conversation_id)[0]
        if not confirm:
            hist = await call_gemini("read", lambda c: c.read_chat(cid, limit=2))
            guard.done(0, "", False)
            first = (hist.turns[-1].text[:200] if hist and hist.turns else "(пусто)")
            return [TextContent(type="text", text=f"Будет удалён чат {cid}. Начало: «{first}». "
                                                  "Удаление необратимо — спросить владельца, потом confirm=true.")]
        await call_gemini("read", lambda c: c.delete_chat(cid))
        guard.done(0, "", False)
        return [TextContent(type="text", text=f"Чат {cid} удалён.")]
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=RO)
async def gemini_list_models() -> list:
    """Какие модели доступны в подписке (имена для параметра model)."""
    try:
        client = await get_client()
        rows = [{"имя": m.model_name, "в Gemini называется": m.display_name, "описание": m.description,
                 "доступна": m.is_available} for m in (client.list_models() or [])]
        rows.append({"картинки": f"обычные — {IMAGE_MODEL}, pro=true — {IMAGE_MODEL_PRO}"})
        return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, indent=1))]
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=RO)
async def gemini_list_gems(include_hidden: bool = False) -> list:
    """Список Gems — своих и встроенных помощников Gemini. Номер передаётся в gemini_chat(gem=…)."""
    try:
        jar = await call_gemini("read", lambda c: c.fetch_gems(include_hidden=include_hidden))
        guard.done(0, "", False)
        rows = [{"номер": g.id, "название": g.name, "описание": g.description, "свой": not g.predefined}
                for g in jar.values()]
        return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, indent=1))]
    except Exception as e:
        return _err(e)


_research: dict[str, dict] = {}


@mcp.tool(annotations=RW)
async def gemini_deep_research(
    prompt: Annotated[str, Field(description="Тема исследования.")],
    model: Annotated[str | None, Field(description="Модель (по умолчанию — как в Gemini).")] = None,
) -> list:
    """Запустить Deep Research в Gemini. Идёт 5–30 минут; вызов сразу возвращает номер задачи,
    результат — через gemini_deep_research_result."""
    try:
        guard.check(False)
        task_id = "dr_" + secrets.token_hex(4)
        _research[task_id] = {"статус": "запускается", "тема": prompt, "начато": datetime.now().strftime("%H:%M")}

        async def run():
            try:
                async def start(client):
                    plan = await client.create_deep_research_plan(prompt, model=model)
                    await client.start_deep_research(plan)
                    return plan

                plan = await call_gemini("text", start)
                guard.done(0, "", False)
                _research[task_id].update(статус="идёт", план=plan.steps, срок=plan.eta_text)
                client = await get_client()
                res = await client.wait_for_deep_research(plan, poll_interval=30, timeout=3600)
                out = res.final_output
                text = out.text if out else ""
                doc = getattr(out, "deep_research_document", None) if out else None
                if doc and doc.content:
                    text = f"# {doc.title or prompt}\n\n{doc.content}"
                key, d = store.new_slot()
                f = d / f"{datetime.now():%Y%m%d-%H%M}-{_slug(prompt)}.md"
                f.write_text(text or "(пустой ответ)", encoding="utf-8")
                _research[task_id].update(статус="готово" if res.done else "не дождался", ссылка=store.url(key, f.name),
                                          номер_разговора=plan.cid)
            except Exception as e:
                _research[task_id].update(статус="ошибка", ошибка=f"{type(e).__name__}: {e}"[:500])

        asyncio.create_task(run())
        return [TextContent(type="text", text=f"Запущено. Номер задачи: {task_id}. Проверять через "
                                              "gemini_deep_research_result не чаще раза в 2–3 минуты.")]
    except Exception as e:
        return _err(e)


@mcp.tool(annotations=RO)
async def gemini_deep_research_result(task_id: str) -> list:
    """Состояние и результат Deep Research (ссылка на файл с отчётом, когда готово)."""
    info = _research.get(task_id)
    if not info:
        return [TextContent(type="text", text="Такой задачи нет (после перезапуска моста список очищается).")]
    return [TextContent(type="text", text=json.dumps(info, ensure_ascii=False, indent=1))]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def gemini_usage(
    clear_pause: Annotated[bool, Field(description="Снять паузу предохранителя. Только если владелец прямо попросил.")] = False,
) -> list:
    """Состояние моста: есть ли вход в Google, сколько картинок сегодня, пауза, лимиты подписки,
    как прислать свою картинку на доработку."""
    if clear_pause:
        guard.clear_pause()
    info = {"вход в Google": "есть" if _login_state["ok"] else ("нет" if _login_state["ok"] is False else "ещё не проверялся")}
    if _login_state["error"]:
        info["что не так со входом"] = _login_state["error"]
    info["предохранитель"] = guard.status()
    if _client is not None:
        try:
            info["лимиты подписки (данные Google)"] = _client.usage_info or _client.quotas
            if _client.abuse_status:
                info["замечания Google к аккаунту"] = _client.abuse_status
        except Exception:
            pass
    info["как прислать свою картинку"] = (
        f'curl -s -H "Authorization: Bearer $MOST_GEMINI_UPLOAD_TOKEN" -F "file=@путь/к/картинке.png" {PUBLIC_URL}/upload '
        "→ ответ {\"id\": \"u_…\"}; этот номер передать в files. Ключ MOST_GEMINI_UPLOAD_TOKEN — в .env "
        "у владельца (на сервере моста — в /etc/most-gemini.env)."
    )
    return [TextContent(type="text", text=json.dumps(info, ensure_ascii=False, indent=1, default=str))]


# ---------------------------------------------------------------------------
# Простые адреса: здоровье, приём файлов, раздача готовых файлов
# ---------------------------------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """?check=1 (раз в час от сторожа): по-настоящему спросить у Google, жив ли вход."""
    if request.query_params.get("check") == "1" and COOKIES_FILE.exists():
        try:
            if _client is None:
                await get_client()
            elif time.time() - _login_state["checked"] > 50 * 60:
                await call_gemini("read", lambda c: c._fetch_recent_chats(recent=1))
                if _is_guest(_client):
                    raise NoLogin("гостевой режим")
                st = getattr(_client, "account_status", None)
                if st is not None and getattr(st, "name", "AVAILABLE") != "AVAILABLE":
                    raise NoLogin(f"Google сообщает: {st.name}")
                _login_state.update(ok=True, checked=time.time(), error="")
        except Exception as e:
            _login_state.update(ok=False, checked=time.time(), error=f"{type(e).__name__}: {e}"[:300])
    return JSONResponse({"ok": True, "login": _login_state["ok"], "paused": guard.state["paused_until"] > time.time(),
                         "images_today": guard.state["images"]})


@mcp.custom_route("/upload", methods=["POST"])
async def upload(request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    if not UPLOAD_TOKEN or not secrets.compare_digest(auth, f"Bearer {UPLOAD_TOKEN}"):
        return JSONResponse({"error": "нужен ключ MOST_GEMINI_UPLOAD_TOKEN"}, status_code=401)
    try:
        form = await request.form(max_files=1, max_fields=2)
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            return JSONResponse({"error": "нет поля file"}, status_code=400)
        uid = store.save_upload(f.filename, await f.read())
        return JSONResponse({"id": uid})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@mcp.custom_route("/f/{key}/{name}", methods=["GET"])
async def get_file(request: Request) -> Response:
    p = store.resolve_public(request.path_params["key"], request.path_params["name"])
    if not p:
        return Response("Нет такого файла (старые удаляются сами).", status_code=404)
    return FileResponse(p, filename=p.name)


PASSWORD = os.environ.get("MOST_GEMINI_PASSWORD", "")
_GATE_KEY_FILE = SECRET_DIR / "gate.key"
_fails: dict[str, list[float]] = {}

_LOGIN_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Мост Gemini — вход</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f5f7;display:grid;place-items:center;min-height:100vh;margin:0}}
form{{background:#fff;padding:28px;border-radius:14px;box-shadow:0 4px 18px #0001;width:min(340px,90vw)}}
h1{{font-size:18px;margin:0 0 14px}}input{{width:100%;box-sizing:border-box;padding:10px;margin:6px 0 14px;font-size:16px;
border:1px solid #ccd;border-radius:8px}}button{{width:100%;padding:10px;font-size:16px;border:0;border-radius:8px;
background:#3b6ef5;color:#fff}}p{{color:#c33;margin:0 0 10px}}</style>
<form method="post" action="/login"><h1>Подключение Claude к мосту Gemini</h1>{err}
<input type="password" name="password" placeholder="Пароль моста" autofocus>
<input type="hidden" name="next" value="{nxt}"><button>Войти</button></form></html>"""


def _gate_key() -> bytes:
    if not _GATE_KEY_FILE.exists():
        _GATE_KEY_FILE.write_bytes(secrets.token_bytes(32))
        os.chmod(_GATE_KEY_FILE, 0o600)
    return _GATE_KEY_FILE.read_bytes()


def _sign(exp: int) -> str:
    import hashlib
    import hmac
    return f"{exp}.{hmac.new(_gate_key(), str(exp).encode(), hashlib.sha256).hexdigest()}"


def _cookie_ok(headers: dict) -> bool:
    raw = headers.get(b"cookie", b"").decode()
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "mg_auth" and "." in v:
            exp = v.split(".", 1)[0]
            if exp.isdigit() and int(exp) > time.time() and secrets.compare_digest(v, _sign(int(exp))):
                return True
    return False


class VhodGate:
    """Кто может открыть окно подключения коннектора (/authorize):
    - на сервере владельца — nginx после единого входа добавляет секретный заголовок (MOST_GEMINI_VHOD_SECRET);
    - в открытой копии — своя страница с паролем моста (MOST_GEMINI_PASSWORD)."""

    def __init__(self, app):
        self.app = app
        self.janitor = None

    async def __call__(self, scope, receive, send):
        if self.janitor is None and scope["type"] in ("http", "lifespan"):
            self.janitor = asyncio.get_running_loop().create_task(_janitor())
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        headers = dict(scope["headers"])
        if path == "/login" and PASSWORD and scope["method"] == "POST":
            return await self._login(scope, receive, send)
        if path == "/authorize":
            got = headers.get(b"x-most-gemini-vhod", b"").decode()
            if VHOD_SECRET and secrets.compare_digest(got, VHOD_SECRET):
                pass
            elif PASSWORD and _cookie_ok(headers):
                pass
            elif PASSWORD:
                import html
                nxt = scope["path"] + ("?" + scope["query_string"].decode() if scope["query_string"] else "")
                page = _LOGIN_PAGE.format(err="", nxt=html.escape(nxt))
                return await Response(page, media_type="text/html")(scope, receive, send)
            else:
                return await Response("Окно подключения закрыто: не задан пароль моста.", status_code=403)(
                    scope, receive, send)
        await self.app(scope, receive, send)

    async def _login(self, scope, receive, send):
        import html
        from urllib.parse import parse_qs

        ip = dict(scope["headers"]).get(b"x-forwarded-for", b"").decode().split(",")[0].strip() or "?"
        now = time.time()
        recent = [t for t in _fails.get(ip, []) if t > now - 600]
        _fails[ip] = recent
        if len(recent) >= 8:
            return await Response("Слишком много попыток. Подождите 10 минут.", status_code=429)(scope, receive, send)
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body") or len(body) > 8192:
                break
        form = parse_qs(body.decode(errors="ignore"))
        pw = (form.get("password") or [""])[0]
        nxt = (form.get("next") or ["/"])[0]
        if not nxt.startswith("/authorize"):
            nxt = "/"
        if not secrets.compare_digest(pw.encode(), PASSWORD.encode()):
            recent.append(now)
            page = _LOGIN_PAGE.format(err="<p>Неверный пароль</p>", nxt=html.escape(nxt))
            return await Response(page, media_type="text/html", status_code=401)(scope, receive, send)
        exp = int(now + 12 * 3600)
        resp = Response(status_code=303, headers={"location": nxt})
        resp.set_cookie("mg_auth", _sign(exp), max_age=12 * 3600, httponly=True, secure=PUBLIC_URL.startswith("https"),
                        samesite="lax", path="/")
        await resp(scope, receive, send)


async def _janitor():
    while True:
        try:
            n = store.cleanup()
            if n:
                log.info("cleanup: removed %d old folders", n)
            tmp = STATE / "tmp"
            if tmp.exists():
                store_border = time.time() - 3600
                for d in tmp.iterdir():
                    if d.stat().st_mtime < store_border:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            log.warning("cleanup failed: %s", e)
        await asyncio.sleep(6 * 3600)


def main():
    if "--stdio" in sys.argv:
        # Без адреса и входа: мост запускается самим Claude Code на этом же сервере (claude mcp add … --stdio).
        mcp.run("stdio")
        return
    app = mcp.streamable_http_app()
    uvicorn.run(VhodGate(app), host="127.0.0.1", port=PORT, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                log_level="warning")


if __name__ == "__main__":
    main()
