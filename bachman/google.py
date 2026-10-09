"""Google sign-in for the podcast account and authorized, read-only requests.

The owner creates an OAuth client of type "Desktop app" in his own Google Cloud project and
signs in once (`bachman google-login`). Bachman keeps the refresh token and exchanges it for
short-lived access tokens. Nothing in this module sends a write to a Google API.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# the port is never listened on: the browser fails to load the page and the user copies the address
REDIRECT = "http://127.0.0.1:8767/"
SCOPES = (
    "https://www.googleapis.com/auth/documents.readonly",  # read the agenda document
    "https://www.googleapis.com/auth/youtube.force-ssl",   # planned: set title, description and publish time
)


class GoogleError(Exception):
    """A failure that is safe to show to the chat agent (no secrets, no internals)."""


class NotSignedIn(GoogleError):
    pass


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def login_url(client_id: str, state: str, verifier: str, scopes=SCOPES) -> str:
    """The address the owner opens in a browser that is signed in to the podcast account."""
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "prompt": "consent",  # both are needed to get a refresh token
        "state": state, "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
    })


def code_from_redirect(pasted: str, state: str) -> str:
    """Take the address the browser ended up on and return the authorization code in it."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted.strip()).query)
    if query.get("error"):
        raise GoogleError(f"Google refused the sign-in: {query['error'][0][:80]}")
    if query.get("state", [""])[0] != state:
        raise GoogleError("this address does not belong to the sign-in that was just started")
    code = query.get("code", [""])[0]
    if not code:
        raise GoogleError("the address contains no code; copy the full address from the browser")
    return code


class Google:
    def __init__(self, http, conf_dir: Path, clock=time.time):
        self.http = http
        self.conf_dir = conf_dir
        self.clock = clock
        self._lock = threading.RLock()
        self._access: str | None = None
        self._expires = 0.0

    def _read(self, name: str) -> str:
        try:
            return (self.conf_dir / name).read_text().strip()
        except OSError:
            raise NotSignedIn("Google is not set up: the client or the sign-in is missing") from None

    def start_login(self) -> tuple[str, str, str]:
        """Return (address to open, state, verifier) for a new sign-in."""
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
        return login_url(self._read("client_id"), state, verifier), state, verifier

    def finish_login(self, pasted: str, state: str, verifier: str) -> list[str]:
        """Exchange the code for tokens, store the refresh token and return the granted scopes."""
        resp = self.http.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code_from_redirect(pasted, state),
            "client_id": self._read("client_id"), "client_secret": self._read("client_secret"),
            "redirect_uri": REDIRECT, "code_verifier": verifier,
        }, timeout=60)
        if resp.status_code != 200:
            raise GoogleError(f"Google rejected the code (HTTP {resp.status_code}); start the sign-in again")
        body = resp.json()
        if not body.get("refresh_token"):
            raise GoogleError("Google returned no refresh token; start the sign-in again")
        granted = sorted(str(body.get("scope", "")).split())
        self.conf_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.conf_dir / "token.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"refresh_token": body["refresh_token"], "scopes": granted}, out)
        with self._lock:
            self._access, self._expires = None, 0.0
        return granted

    def _token(self) -> str:
        with self._lock:
            if self._access and self._expires > self.clock() + 120:
                return self._access
            try:
                stored = json.loads(self._read("token.json"))
            except ValueError:
                raise NotSignedIn("the stored Google sign-in is unreadable; sign in again") from None
            resp = self.http.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": stored.get("refresh_token", ""),
                "client_id": self._read("client_id"), "client_secret": self._read("client_secret"),
            }, timeout=60)
            if resp.status_code in (400, 401):
                raise NotSignedIn("the Google sign-in expired or was revoked; sign in again")
            if resp.status_code != 200:
                raise GoogleError(f"Google answered HTTP {resp.status_code} when renewing the sign-in")
            body = resp.json()
            self._access = body["access_token"]
            self._expires = self.clock() + int(body.get("expires_in", 3600))
            return self._access

    def get_json(self, url: str, params: dict | None = None) -> dict:
        """One authorized GET. The only request this class makes to a Google API."""
        resp = self.http.get(url, params=params or {}, headers={"Authorization": f"Bearer {self._token()}"}, timeout=60)
        if resp.status_code == 403:
            raise GoogleError("Google denied the request: the account cannot open this or the API is not enabled")
        if resp.status_code == 404:
            raise GoogleError("Google did not find this document")
        if resp.status_code != 200:
            raise GoogleError(f"Google answered HTTP {resp.status_code}")
        return resp.json()
