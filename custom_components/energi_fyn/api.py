"""Async client for the Energi Fyn app API (reverse-engineered from the iOS app)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from html import unescape
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urljoin, urlencode, urlparse

import aiohttp

AUTH_BASE = "https://accounts.forsyningslogin.dk"
PROFILE_BASE = "https://efprofileservice.azurewebsites.net/api"
API_BASE = "https://webapi.energifyn.dk/api/v4"

APP_VERSION = "3.4.9"
CLIENT_ID = "ef-app"
# Embedded in the public app binary; identifies the app, not the user.
CLIENT_SECRET = "e13fefb9f454418988c37e0bd05e3555"
REDIRECT_URI = "energifyn://authorize"
SCOPE = "openid profile customer offline_access"

APP_HEADERS = {
    "Accept": "application/json",
    "Client-Identity": f"APP_EF/{APP_VERSION}/production",
    "X-App-Version": APP_VERSION,
    "User-Agent": "EnergiFyn/1 CFNetwork/3860.700.2 Darwin/25.6.0",
}
BROWSER_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.6.1 Mobile/15E148 Safari/604.1"
)


class EnergiFynError(Exception):
    """Base error."""


class AuthError(EnergiFynError):
    """Bad credentials or revoked refresh token."""


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(96)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _jwt_exp(token: str) -> float:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])


class EnergiFynClient:
    """Login, token refresh and consumption queries."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        # Called with the new refresh token whenever the server rotates it.
        self._token_callback = token_callback
        # Refresh tokens rotate, so two concurrent refreshes would invalidate each other.
        self._lock = asyncio.Lock()
        self.refresh_token: str | None = None
        # Which customer's API token to fetch; defaults to the first on the account.
        self.customer_id: str | None = None
        self._access_token = ""
        self._o2_token: str | None = None
        self._o2_exp = 0.0

    # ---- authentication -------------------------------------------------

    async def login(self, username: str, password: str) -> str:
        """Run the OIDC code+PKCE flow headlessly. Returns the refresh token."""
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(8)
        query = urlencode(
            {
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "response_type": "code",
                "state": state,
                "scope": SCOPE,
            }
        )
        # Own cookie jar: the login is cookie-based and must not leak into HA's session.
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(
            cookie_jar=jar, headers={"User-Agent": BROWSER_UA}
        ) as web:
            page_url = await self._follow(web, f"{AUTH_BASE}/connect/authorize?{query}")
            async with web.get(page_url) as resp:
                html = await resp.text()
            token = re.search(
                r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html
            )
            return_url = re.search(r'name="ReturnUrl"[^>]*value="([^"]*)"', html)
            if not token:
                raise EnergiFynError("login form not found")
            async with web.post(
                page_url,
                data={
                    "ReturnUrl": unescape(return_url.group(1)) if return_url else "",
                    "Username": username,
                    "Password": password,
                    "button": "login",
                    "__RequestVerificationToken": token.group(1),
                },
            ) as resp:
                body = await resp.text()
            refresh = re.search(r'http-equiv="refresh"\s+content="0;url=([^"]+)"', body)
            if not refresh:
                raise AuthError("invalid username or password")
            callback = urljoin(AUTH_BASE, unescape(refresh.group(1)))
            code = await self._code_from_callback(web, callback, state)

        data = await self._token_request(
            {
                "grant_type": "authorization_code",
                "scope": SCOPE,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            }
        )
        self.refresh_token = data["refresh_token"]
        self._access_token = data["access_token"]
        return self.refresh_token

    @staticmethod
    async def _follow(web: aiohttp.ClientSession, url: str) -> str:
        """GET following redirects; return the final URL."""
        async with web.get(url) as resp:
            return str(resp.url)

    @staticmethod
    async def _code_from_callback(
        web: aiohttp.ClientSession, url: str, state: str
    ) -> str:
        async with web.get(url, allow_redirects=False) as resp:
            location = resp.headers.get("Location", "")
        if not location.startswith(REDIRECT_URI):
            raise EnergiFynError("login callback did not redirect to the app")
        params = parse_qs(urlparse(location).query)
        if params.get("state", [""])[0] != state:
            raise EnergiFynError("OIDC state mismatch")
        return params["code"][0]

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        form = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **form}
        async with self._session.post(f"{AUTH_BASE}/connect/token", data=form) as resp:
            if resp.status in (400, 401):
                raise AuthError(await resp.text())
            resp.raise_for_status()
            return await resp.json()

    async def _bearer(self) -> str:
        """Return a valid 30-day API token, refreshing via the OIDC refresh token."""
        async with self._lock:
            if self._o2_token and time.time() < self._o2_exp - 3600:
                return self._o2_token
            if not self.refresh_token:
                raise AuthError("not logged in")
            data = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
            )
            new_refresh = data.get("refresh_token", self.refresh_token)
            if new_refresh != self.refresh_token:
                self.refresh_token = new_refresh
                if self._token_callback:
                    self._token_callback(new_refresh)
            self._access_token = data["access_token"]
            customer = self.customer_id or (await self.customers())[0]["customerNumber"]
            async with self._session.get(
                f"{PROFILE_BASE}/customers/{customer}/o2-token",
                headers={
                    **APP_HEADERS,
                    "Authorization": f"Bearer {self._access_token}",
                },
            ) as resp:
                resp.raise_for_status()
                self._o2_token = (await resp.json()).strip()
            self._o2_exp = _jwt_exp(self._o2_token)
            return self._o2_token

    # ---- API calls ------------------------------------------------------

    async def customers(self) -> list[dict[str, Any]]:
        async with self._session.get(
            f"{PROFILE_BASE}/customers",
            headers={**APP_HEADERS, "Authorization": f"Bearer {self._access_token}"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _api(self, method: str, path: str, **kw: Any) -> Any:
        headers = {**APP_HEADERS, "Authorization": f"Bearer {await self._bearer()}"}
        async with self._session.request(
            method, f"{API_BASE}{path}", headers=headers, **kw
        ) as resp:
            if resp.status == 401:
                raise AuthError("API token rejected")
            resp.raise_for_status()
            return await resp.json()

    @staticmethod
    def meter_id(meter: dict[str, Any]) -> str:
        """The consumption endpoint addresses a meter as '<number>-<installationId>'."""
        return f"{meter['number']}-{meter['installationId']}"

    async def estates(self, customer_id: str) -> list[dict[str, Any]]:
        return await self._api("GET", f"/customers/{customer_id}/estate")

    async def products(self, customer_id: str) -> list[dict[str, Any]]:
        return await self._api("GET", f"/customers/{customer_id}/products")

    async def consumption(
        self,
        customer_id: str,
        estate_id: int | str,
        meter_number: str,
        start: datetime,
        end: datetime,
        hourly: bool,
    ) -> dict[str, Any]:
        """Hourly (one day) or daily (one month) consumption in kWh."""
        body = {
            "startDate": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "endDate": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "aggregateFactor": "Hourly" if hourly else "Daily",
            "displayType": "Daily" if hourly else "Monthly",
        }
        return await self._api(
            "POST",
            f"/customers/{customer_id}/estates/{estate_id}/meters/{meter_number}"
            "/consumption/detail",
            json=body,
        )
