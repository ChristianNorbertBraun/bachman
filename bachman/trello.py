"""Read-only view of the show's Trello boards: lists, cards and one card in detail.

The owner creates a Power-Up in his Trello account to get an API key and authorizes a token
for it. Both are sent in the Authorization header, never in the address, so they do not end
up in logs. Nothing in this module sends a write.
"""
from __future__ import annotations

import re
from pathlib import Path

API = "https://api.trello.com/1/"
_LINK = re.compile(r"trello\.com/[bc]/([A-Za-z0-9]{6,24})")
_ID = re.compile(r"[A-Za-z0-9]{6,24}")


class TrelloError(Exception):
    """A failure that is safe to show to the chat agent (no secrets, no internals)."""


def short_id(value) -> str:
    """Accept a board or card id, its short link, or its address from the browser."""
    if not isinstance(value, str):
        raise TrelloError("pass the address or the id of the board or card")
    found = _LINK.search(value)
    candidate = found.group(1) if found else value.strip()
    if not _ID.fullmatch(candidate):
        raise TrelloError("pass the address or the id of the board or card")
    return candidate


class Trello:
    def __init__(self, http, conf_dir: Path):
        self.http = http
        self.conf_dir = conf_dir

    def _read(self, name: str) -> str:
        try:
            return (self.conf_dir / name).read_text().strip()
        except OSError:
            raise TrelloError("Trello is not set up: the API key or the token is missing") from None

    def _headers(self) -> dict:
        return {"Authorization": f'OAuth oauth_consumer_key="{self._read("key")}", oauth_token="{self._read("token")}"',
                "Accept": "application/json"}

    def get(self, path: str, params: dict | None = None):
        """One authorized GET. The only request this class makes."""
        resp = self.http.get(API + path, params=params or {}, headers=self._headers(), timeout=60)
        if resp.status_code == 401:
            raise TrelloError("Trello rejected the key or the token: create a new token and enter it again")
        if resp.status_code in (403, 404):
            raise TrelloError("Trello did not find this, or the account may not see it")
        if resp.status_code == 429:
            raise TrelloError("Trello asks to slow down: try again in a minute")
        if resp.status_code != 200:
            raise TrelloError(f"Trello answered HTTP {resp.status_code}")
        return resp.json()

    def boards(self) -> list[dict]:
        found = self.get("members/me/boards", {"fields": "name,shortLink,closed,dateLastActivity", "filter": "open"})
        return [b for b in found if not b.get("closed")]

    def board(self, board: str) -> dict:
        """The board with its open lists and their open cards, in the order shown in Trello."""
        board = short_id(board)
        info = self.get(f"boards/{board}", {"fields": "name,shortLink,desc"})
        lists = self.get(f"boards/{board}/lists", {
            "filter": "open", "fields": "name", "cards": "open",
            "card_fields": "name,shortLink,due,dueComplete,labels,badges,dateLastActivity"})
        return {**info, "lists": lists}

    def card(self, card: str) -> dict:
        return self.get(f"cards/{short_id(card)}", {
            "fields": "name,desc,due,dueComplete,labels,shortLink,dateLastActivity",
            "list": "true", "list_fields": "name", "board": "true", "board_fields": "name",
            "checklists": "all", "checklist_fields": "name",
            "attachments": "true", "attachment_fields": "name,url",
            "actions": "commentCard", "actions_limit": "10", "action_fields": "data,date", "action_memberCreator_fields": "fullName"})
