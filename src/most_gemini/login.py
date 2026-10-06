"""Вход моста в Google. Три способа:

1. most-gemini-login               — открывает чистое окно Chrome на рабочем столе сервера, владелец входит
                                     в Google сам, программа забирает две куки и закрывает окно.
2. most-gemini-login --json ФАЙЛ   — куки из выгрузки браузера (расширение Cookie-Editor → Export → JSON),
                                     или «-» — прочитать выгрузку из ввода.
3. most-gemini-login --paste       — спросит два значения (__Secure-1PSID и __Secure-1PSIDTS) скрытым вводом.

Куки кладутся в файл, доступный только мосту, мост перезапускается и проверяет вход.
Пароль от Google программа не видит и не сохраняет. Окно браузера одноразовое: профиль удаляется,
второго «продлевателя» входа не остаётся (иначе браузер и мост перебивают друг друга).
"""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("MOST_GEMINI_STATE", "/var/lib/most-gemini"))
SERVICE_USER = os.environ.get("MOST_GEMINI_USER", "gmost")
SERVICE = os.environ.get("MOST_GEMINI_SERVICE", "most-gemini")
PORT = os.environ.get("MOST_GEMINI_PORT", "8150")
WANTED = ("__Secure-1PSID", "__Secure-1PSIDTS")
START = "https://accounts.google.com/ServiceLogin?continue=https://gemini.google.com/app"


def say(text: str) -> None:
    print(text, flush=True)


def from_browser() -> dict:
    from playwright.sync_api import sync_playwright

    say("Сейчас откроется окно Chrome. Войдите в Google (аккаунт с подпиской Gemini).")
    say("Когда откроется Gemini, окно закроется само. На всё — 15 минут.\n")
    got = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--lang=ru-RU", "--start-maximized"],
            ignore_default_args=["--enable-automation"],
        )
        ctx = browser.new_context(locale="ru-RU", no_viewport=True)
        page = ctx.new_page()
        page.goto(START)
        deadline = time.time() + 900

        def jar():
            return {c["name"]: c["value"] for c in ctx.cookies("https://gemini.google.com")
                    if c["domain"].endswith("google.com")}

        while time.time() < deadline:
            try:
                url = page.url
            except Exception:
                break
            if url.startswith("https://gemini.google.com/") and jar().get("__Secure-1PSID"):
                time.sleep(5)  # дать Google дописать свежие куки
                c = jar()
                got = {k: c[k] for k in WANTED if k in c}
                break
            time.sleep(2)
        browser.close()
    return got


def from_json(src: str) -> dict:
    raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict) and "cookies" in data:  # формат storage_state (Playwright)
        data = data["cookies"]
    if isinstance(data, dict):
        return {k: data[k] for k in WANTED if data.get(k)}
    got = {}
    for c in data:  # формат Cookie-Editor: список {name, value, domain, ...}
        if c.get("name") in WANTED and str(c.get("domain", "")).endswith("google.com") and c.get("value"):
            got[c["name"]] = c["value"]
    return got


def from_paste() -> dict:
    say("Значения берутся в браузере: gemini.google.com → F12 → Application (Приложение) → Cookies → "
        "https://gemini.google.com. Символы при вводе не видны — так и должно быть.")
    got = {}
    for name in WANTED:
        v = getpass.getpass(f"{name}: ").strip()
        if v:
            got[name] = v
    return got


def main() -> int:
    ap = argparse.ArgumentParser(description="Вход моста Gemini в Google")
    ap.add_argument("--json", metavar="ФАЙЛ", help="выгрузка кук из браузера (JSON) или «-» для ввода")
    ap.add_argument("--paste", action="store_true", help="ввести две куки вручную")
    a = ap.parse_args()

    got = from_json(a.json) if a.json else from_paste() if a.paste else from_browser()
    if not got.get("__Secure-1PSID"):
        say("Вход не получен (нет куки __Secure-1PSID). Ничего не изменено.")
        return 1

    secret = STATE / "secret"
    secret.mkdir(parents=True, exist_ok=True)
    f = secret / "google-cookies.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(got), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(f)
    shutil.rmtree(secret / "cache", ignore_errors=True)  # старый продлённый вход больше не нужен
    try:
        for path in (secret, f):
            shutil.chown(path, SERVICE_USER, SERVICE_USER)
    except (LookupError, PermissionError):
        pass
    subprocess.run(["systemctl", "restart", SERVICE], check=False)
    time.sleep(4)
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health?check=1", timeout=120) as r:
            ok = json.loads(r.read()).get("login")
    except Exception:
        ok = None
    say("Готово: мост вошёл в Gemini." if ok else "Куки сохранены, но мост пока не подтвердил вход — скажите Claude.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
