"""Предохранитель: чтобы Google не видел в мосте робота.

- запросы к Gemini идут строго по одному;
- между картинками — пауза 20–40 секунд (случайная), между текстовыми запросами — 5 секунд,
  между чтениями (список чатов, моделей) — секунда;
- не больше N картинок в сутки (по московскому времени), по умолчанию 50;
- если Gemini ответил, что лимит картинок исчерпан, — мост сам встаёт на паузу и не долбит повторами.

Числа меняются в /etc/most-gemini.env без правки кода.
"""
import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


IMAGES_PER_DAY = int(_env_float("GEMINI_IMAGES_PER_DAY", 50))
IMAGE_PAUSE = (_env_float("GEMINI_IMAGE_PAUSE_MIN", 20), _env_float("GEMINI_IMAGE_PAUSE_MAX", 40))
TEXT_PAUSE = _env_float("GEMINI_TEXT_PAUSE", 5)
LIMIT_PAUSE_HOURS = _env_float("GEMINI_LIMIT_PAUSE_HOURS", 6)

# Как Gemini говорит «лимит картинок кончился» (по-русски и по-английски).
_LIMIT_RE = re.compile(
    r"(limit (resets|is reset)|once your limit|reached (your|the) (daily )?limit|create more images|"
    r"лимит[а-я]* (будет )?(сбро|обнов|восстанов|исчерп)|исчерпа[а-я]* лимит|достигли (дневного )?лимит|"
    r"смогу (создать|сделать|нарисовать) больше)",
    re.IGNORECASE,
)


class Refused(Exception):
    """Предохранитель не пустил запрос; текст — объяснение по-человечески."""


class Guard:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.state = {"day": "", "images": 0, "paused_until": 0.0, "pause_reason": "", "last_call": 0.0}
        if path.exists():
            try:
                self.state.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _today(self) -> str:
        day = datetime.now(MSK).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            self.state.update(day=day, images=0)
        return day

    def images_left(self) -> int:
        self._today()
        return max(0, IMAGES_PER_DAY - self.state["images"])

    def status(self) -> dict:
        self._today()
        paused = self.state["paused_until"] > time.time()
        return {
            "картинок сегодня": self.state["images"],
            "лимит моста в сутки": IMAGES_PER_DAY,
            "осталось сегодня": self.images_left(),
            "пауза": (
                f"до {datetime.fromtimestamp(self.state['paused_until'], MSK):%d.%m %H:%M} МСК — "
                f"{self.state['pause_reason']}" if paused else "нет"
            ),
            "пауза между картинками, сек": f"{IMAGE_PAUSE[0]:.0f}–{IMAGE_PAUSE[1]:.0f}",
        }

    def check(self, wants_images: bool) -> None:
        """Пустить ли запрос вообще (без ожидания)."""
        if self.state["paused_until"] > time.time():
            until = datetime.fromtimestamp(self.state["paused_until"], MSK)
            raise Refused(
                f"Мост на паузе до {until:%d.%m %H:%M} МСК: {self.state['pause_reason']}. "
                "Это защита аккаунта Google; снять паузу раньше можно инструментом gemini_usage(clear_pause=true), "
                "но только если владелец сам попросил."
            )
        if wants_images and self.images_left() <= 0:
            raise Refused(
                f"Сегодня уже сделано {self.state['images']} картинок — это дневной лимит моста "
                f"({IMAGES_PER_DAY}), он защищает аккаунт Google. Продолжить можно завтра (с 00:00 МСК)."
            )

    async def wait_turn(self, kind: str) -> float:
        """Подождать паузу после прошлого запроса. kind: image / text / read. Вызывать под self.lock."""
        gap = {"image": random.uniform(*IMAGE_PAUSE), "text": TEXT_PAUSE}.get(kind, 1.0)
        wait = self.state["last_call"] + gap - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        return max(0.0, wait)

    def done(self, images_made: int, reply_text: str, wanted_images: bool) -> str | None:
        """Отметить запрос. Если Gemini сказал про исчерпанный лимит — встать на паузу и вернуть пояснение."""
        self._today()
        self.state["last_call"] = time.time()
        self.state["images"] += images_made
        note = None
        if wanted_images and images_made == 0 and reply_text and _LIMIT_RE.search(reply_text):
            self.pause(LIMIT_PAUSE_HOURS, "Gemini сообщил, что лимит картинок подписки исчерпан")
            note = (
                f"Gemini сообщил об исчерпании лимита картинок. Мост встал на паузу на {LIMIT_PAUSE_HOURS:.0f} ч, "
                "повторять сейчас не нужно."
            )
        self._save()
        return note

    def pause(self, hours: float, reason: str) -> None:
        self.state["paused_until"] = time.time() + hours * 3600
        self.state["pause_reason"] = reason
        self._save()

    def clear_pause(self) -> None:
        self.state["paused_until"] = 0.0
        self.state["pause_reason"] = ""
        self._save()
