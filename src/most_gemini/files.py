"""Где лежат картинки и как они попадают к Claude и обратно.

- Готовые картинки: <STATE>/files/<случайный ключ>/<имя>. Ссылка вида
  <адрес моста>/f/<ключ>/<имя> — ключ на 128 бит, угадать нельзя; Claude Code
  скачивает по ней картинку к владельцу на компьютер.
- Своя картинка на доработку: Claude Code отправляет файл на /upload с ключом UPLOAD_TOKEN
  (лежит в .env на компьютере владельца), в ответ — номер вида u_xxx, его и передают мосту.
  Либо просто https-ссылка на картинку.
- Старше GEMINI_FILES_KEEP_DAYS (по умолчанию 14) — удаляются сами.
"""
import ipaddress
import os
import re
import secrets
import shutil
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_UPLOAD = 25 * 1024 * 1024
KEEP_DAYS = float(os.environ.get("GEMINI_FILES_KEEP_DAYS", "14"))
_SAFE = re.compile(r"[^A-Za-z0-9А-Яа-яЁё._-]+")
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".pdf", ".txt", ".md", ".csv",
                ".mp4", ".mov", ".mp3", ".wav", ".m4a", ".docx", ".xlsx"}


class FileStore:
    def __init__(self, root: Path, public_base: str):
        self.out = root / "files"
        self.inbox = root / "uploads"
        self.out.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.public_base = public_base.rstrip("/")

    # --- готовые картинки ---
    def new_slot(self) -> tuple[str, Path]:
        key = secrets.token_urlsafe(16)
        d = self.out / key
        d.mkdir()
        return key, d

    def url(self, key: str, name: str) -> str:
        return f"{self.public_base}/f/{key}/{name}"

    def resolve_public(self, key: str, name: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", key) or "/" in name or name.startswith("."):
            return None
        p = (self.out / key / name).resolve()
        if self.out.resolve() not in p.parents or not p.is_file():
            return None
        return p

    # --- присланные файлы ---
    def save_upload(self, filename: str, data: bytes) -> str:
        ext = Path(filename or "").suffix.lower()
        if ext not in _ALLOWED_EXT:
            raise ValueError(f"такой файл не принимаю: {ext or 'без расширения'}")
        if len(data) > MAX_UPLOAD:
            raise ValueError("файл больше 25 МБ")
        uid = "u_" + secrets.token_hex(12)
        name = _SAFE.sub("_", Path(filename).name)[-80:] or ("file" + ext)
        d = self.inbox / uid
        d.mkdir()
        (d / name).write_bytes(data)
        return uid

    async def resolve_input(self, ref: str, tmp_dir: Path) -> Path:
        """u_xxx, ссылка на нашу готовую картинку или https-ссылка → путь к файлу на сервере."""
        ref = ref.strip()
        if re.fullmatch(r"u_[0-9a-f]{24}", ref):
            d = self.inbox / ref
            files = list(d.iterdir()) if d.is_dir() else []
            if not files:
                raise ValueError(f"файл {ref} не найден (старые удаляются через {KEEP_DAYS:.0f} дн.)")
            return files[0]
        if ref.startswith(self.public_base + "/f/"):
            key, _, name = ref[len(self.public_base) + 3:].partition("/")
            p = self.resolve_public(key, name)
            if not p:
                raise ValueError(f"картинка {ref} не найдена (старые удаляются через {KEEP_DAYS:.0f} дн.)")
            return p
        if ref.startswith("https://"):
            return await self._download(ref, tmp_dir)
        raise ValueError(
            f"не понимаю, что за файл: {ref!r}. Передайте номер u_… (после отправки на /upload), "
            "ссылку на готовую картинку моста или https-ссылку."
        )

    async def _download(self, url: str, tmp_dir: Path) -> Path:
        host = urlparse(url).hostname or ""
        for info in socket.getaddrinfo(host, 443):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("ссылки на внутренние адреса не принимаю")
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
            r = await http.get(url)
            r.raise_for_status()
            if len(r.content) > MAX_UPLOAD:
                raise ValueError("файл по ссылке больше 25 МБ")
        ctype = r.headers.get("content-type", "").split(";")[0]
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
               "application/pdf": ".pdf"}.get(ctype) or Path(urlparse(url).path).suffix.lower() or ".bin"
        if ext not in _ALLOWED_EXT:
            raise ValueError(f"по ссылке не картинка и не документ ({ctype})")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        p = tmp_dir / ("link" + secrets.token_hex(4) + ext)
        p.write_bytes(r.content)
        return p

    def cleanup(self) -> int:
        border = time.time() - KEEP_DAYS * 86400
        n = 0
        for base in (self.out, self.inbox):
            for d in base.iterdir():
                try:
                    if d.stat().st_mtime < border:
                        shutil.rmtree(d, ignore_errors=True)
                        n += 1
                except FileNotFoundError:
                    pass
        return n
