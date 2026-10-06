"""Вход в мост для Claude (OAuth), как у моста Google.

Кто может войти, решает server.VhodGate: окно /authorize открывается только после пароля моста
(MOST_GEMINI_PASSWORD) или, на сервере автора, после единого входа, который подтверждает nginx
секретным заголовком. Здесь — только выдача и проверка пропусков.

Выданные пропуска хранятся в файле, чтобы после перезапуска моста Claude не входил заново.
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ACCESS_TTL = 30 * 24 * 3600       # пропуск на 30 дней, дальше Claude продлевает сам
REFRESH_TTL = 365 * 24 * 3600     # продление — на год
CODE_TTL = 300


class FileOAuthProvider:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"clients": {}, "codes": {}, "access": {}, "refresh": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass

    def _save(self) -> None:
        now = time.time()
        for kind in ("codes", "access", "refresh"):
            self.data[kind] = {
                k: v for k, v in self.data[kind].items()
                if not v.get("expires_at") or v["expires_at"] > now
            }
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)

    # --- клиенты (claude.ai, Claude Code) регистрируются сами ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self._save()

    # --- окно входа: сюда доходят только после единого входа ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self.data["codes"][code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        ).model_dump(mode="json")
        self._save()
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        raw = self.data["codes"].get(authorization_code)
        if not raw or raw["client_id"] != client.client_id or raw["expires_at"] < time.time():
            return None
        return AuthorizationCode.model_validate(raw)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        now = int(time.time())
        self.data["access"][access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes,
            expires_at=now + ACCESS_TTL, resource=resource, subject="owner",
        ).model_dump(mode="json")
        self.data["refresh"][refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes,
            expires_at=now + REFRESH_TTL, resource=resource, subject="owner",
        ).model_dump(mode="json")
        self._save()
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            refresh_token=refresh, scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.data["codes"].pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "код уже использован")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        raw = self.data["refresh"].get(refresh_token)
        if not raw or raw["client_id"] != client.client_id:
            return None
        if raw.get("expires_at") and raw["expires_at"] < time.time():
            return None
        return RefreshToken.model_validate(raw)

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        # Старый пропуск продления НЕ отзываем: иначе claude.ai в облаке и приложение на компьютере
        # выбивают друг друга (урок моста NotebookLM). Он сам истечёт через год.
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self.data["access"].get(token)
        if not raw or (raw.get("expires_at") and raw["expires_at"] < time.time()):
            return None
        return AccessToken.model_validate(raw)

    async def revoke_token(self, token) -> None:
        self.data["access"].pop(token.token, None)
        self.data["refresh"].pop(token.token, None)
        self._save()


__all__ = ["FileOAuthProvider", "AuthorizeError"]
