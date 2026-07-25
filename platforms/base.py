"""Platform adapter contracts for authorization and future publishing."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class PublishNotImplementedError(NotImplementedError):
    pass


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    code_verifier: str


class PlatformAdapter:
    code = ""
    integration_type = "browser"

    def publish(self, video: str, title: str, tags: list[str], schedule: str | None = None) -> dict[str, Any]:
        raise PublishNotImplementedError(f"{self.code} 发布流程尚未启用")


class OAuthAdapter(PlatformAdapter):
    integration_type = "api"
    authorization_endpoint = ""
    token_endpoint = ""
    scopes: tuple[str, ...] = ()
    use_pkce = True

    def authorization_request(self, client_id: str, redirect_uri: str) -> AuthorizationRequest:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = self.authorization_params(client_id, redirect_uri, state, challenge)
        return AuthorizationRequest(f"{self.authorization_endpoint}?{urllib.parse.urlencode(query)}", state, verifier)

    def authorization_params(self, client_id: str, redirect_uri: str, state: str, challenge: str) -> dict[str, str]:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        if self.use_pkce:
            params.update({"code_challenge": challenge, "code_challenge_method": "S256"})
        return params

    def exchange_token(self, *, code: str, client_id: str, client_secret: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        payload = {
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if self.use_pkce:
            payload["code_verifier"] = code_verifier
        if client_secret:
            payload["client_secret"] = client_secret
        request = urllib.request.Request(
            self.token_endpoint,
            data=urllib.parse.urlencode(payload).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("access_token"):
            raise ValueError(result.get("error_description") or result.get("error") or "OAuth token exchange failed")
        return result


class BrowserAdapter(PlatformAdapter):
    integration_type = "browser"
    login_url = ""

    def start_authorization(self, profile_path: str, *, account_id: int = 0,
                            cookie_domains_json: str = "[]", login_cookie_names_json: str = "[]") -> dict[str, Any]:
        from browser_auth_service import launch_persistent_login

        return launch_persistent_login(
            self.code, profile_path, self.login_url, account_id=account_id,
            cookie_domains_json=cookie_domains_json,
            login_cookie_names_json=login_cookie_names_json,
        )
