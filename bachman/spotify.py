"""Read-only client for the unofficial Spotify for Creators API.

Authenticates with the session cookies of the podcast login and runs persisted GraphQL
queries. There is deliberately no way to send a mutation or a REST write from this module.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import string
import threading
import time
from pathlib import Path

from . import ops as ops_mod

CLIENT_ID = "05a1371ee5194c27860b3ff3ff3979d2"  # public client id of the web app
REDIRECT = "https://podcasters.spotify.com"
AUTH_URL = "https://accounts.spotify.com/oauth2/v2/auth"
TOKEN_URL = "https://accounts.spotify.com/api/token"
GRAPHQL = "https://creators-graph.spotify.com/v2/graph-pq"


class SpotifyError(Exception):
    """A failure that is safe to show to the chat agent (no secrets, no internals)."""


class LoginExpired(SpotifyError):
    pass


def _rand(n: int) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(n))


class Spotify:
    def __init__(self, http, conf_dir: Path, ops_path: Path, clock=time.time):
        self.http = http
        self.conf_dir = conf_dir
        self.ops_path = ops_path
        self.clock = clock
        self._lock = threading.RLock()
        self._bearer: str | None = None
        self._bearer_expires = 0.0
        # after a rejected login nothing is retried until the cookie files change,
        # so a dead session cannot turn into a stream of login attempts
        self._poisoned_mtime: float | None = None
        self._ops = ops_mod.load(ops_path)

    def _conf(self, name: str) -> str:
        return (self.conf_dir / name).read_text().strip()

    @property
    def show_uri(self) -> str:
        return "spotify:show:" + self._conf("show_id")

    def _cookie_mtime(self) -> float:
        return max((self.conf_dir / n).stat().st_mtime for n in ("sp_dc", "sp_key"))

    def _token(self) -> str:
        with self._lock:
            if self._bearer and self._bearer_expires > self.clock() + 120:
                return self._bearer
            if self._poisoned_mtime is not None and self._poisoned_mtime == self._cookie_mtime():
                raise LoginExpired("Spotify login expired: the cookies must be entered again")
            state, verifier = _rand(32), _rand(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            page = self.http.get(
                AUTH_URL,
                params={
                    "response_type": "code", "client_id": CLIENT_ID,
                    "scope": "streaming ugc-image-upload user-read-email user-read-private",
                    "redirect_uri": REDIRECT, "code_challenge": challenge, "code_challenge_method": "S256",
                    "state": state, "response_mode": "web_message", "prompt": "none",
                },
                cookies={"sp_dc": self._conf("sp_dc"), "sp_key": self._conf("sp_key")},
                timeout=60,
            )
            page.raise_for_status()
            code = re.search(r"""["']?code["']?\s*:\s*["']([^"']+)["']""", page.text)
            got_state = re.search(r"""["']?state["']?\s*:\s*["']([^"']+)["']""", page.text)
            if "login_required" in page.text or not code or not got_state or got_state.group(1) != state:
                self._poisoned_mtime = self._cookie_mtime()
                raise LoginExpired("Spotify login expired: the cookies must be entered again")
            resp = self.http.post(
                TOKEN_URL,
                data={"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code.group(1),
                      "redirect_uri": REDIRECT, "code_verifier": verifier},
                timeout=60,
            )
            resp.raise_for_status()
            body = resp.json()
            self._bearer = body["access_token"]
            self._bearer_expires = self.clock() + int(body.get("expires_in", 3600))
            self._poisoned_mtime = None
            return self._bearer

    def _post(self, op: str, variables: dict):
        known = self._ops.get(op)
        if known is None:
            return None
        if known["type"] != "query":
            raise SpotifyError(f"refused: {op} is not a read operation")
        return self.http.post(
            GRAPHQL,
            headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json",
                     "Content-Type": "application/json", "x-creator-client": "public-website"},
            json={"variables": variables, "operationName": op,
                  "extensions": {"persistedQuery": {"version": 1, "sha256Hash": known["hash"]}}},
            timeout=60,
        )

    @staticmethod
    def _stale(resp) -> bool:
        """True when the server no longer knows the hash we sent."""
        if resp is None or resp.status_code == 400:
            return True
        try:
            errors = resp.json().get("errors") or []
        except ValueError:
            return False
        return any("PersistedQuery" in str(e.get("message", "")) for e in errors)

    def query(self, op: str, variables: dict) -> dict:
        """Run one persisted query and return its `data`. Field-level errors are tolerated as long
        as data came back, because the episode list reports analytics errors for unpublished episodes."""
        with self._lock:
            resp = self._post(op, variables)
            if self._stale(resp):
                self._ops = ops_mod.refresh(self.http, self.ops_path)
                resp = self._post(op, variables)
        if resp is None:
            raise SpotifyError(f"Spotify no longer offers the operation {op}")
        if resp.status_code == 401:
            self._bearer = None
            raise SpotifyError("Spotify rejected the session token")
        if resp.status_code != 200:
            raise SpotifyError(f"Spotify answered HTTP {resp.status_code} for {op}")
        data = resp.json().get("data")
        if not data:
            raise SpotifyError(f"Spotify returned no data for {op}")
        return data

    # --- the reads the tools need ---------------------------------------------------------------
    def episodes(self, page_size: int = 15) -> list[dict]:
        data = self.query("WebGetIndexedEpisodeList", {
            "showUri": self.show_uri, "pageSize": page_size, "currentPage": 1, "includeMembershipTiers": False,
        })
        items = ((data.get("showByShowUri") or {}).get("episodesV2") or {}).get("items") or []
        out = []
        for it in items:
            published = (it.get("publishedOn") or {}).get("seconds")
            created = (it.get("createdOn") or {}).get("seconds")
            out.append({
                "id": str(it.get("episodeId")),
                "uri": it.get("uri"),
                "title": (it.get("title") or "").strip(),
                "published": int(published) if published else None,
                "created": int(created) if created else None,
                "video": "VIDEO" in str(it.get("contentType")),
                "minutes": round(((it.get("asset") or {}).get("lengthMs") or 0) / 60000, 1),
            })
        return out

    def transcript_length(self, uri: str) -> int:
        data = self.query("getEpisodeTranscriptAvailability", {"episodeUri": uri})
        node = (((data.get("episodeByUri") or {}).get("transcript") or {}).get("transcript") or {})
        return int(node.get("transcriptTextLength") or 0)

    def transcript(self, uri: str) -> str:
        data = self.query("getEpisodeTranscript", {"episodeUri": uri})
        node = (((data.get("episodeByUri") or {}).get("transcript") or {}).get("transcript") or {})
        return node.get("transcriptText") or ""
