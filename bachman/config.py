"""Paths and the optional config file of the service user.

Everything lives in the XDG directories of that user: config and credentials in
$XDG_CONFIG_HOME/bachman (default ~/.config/bachman), state in $XDG_STATE_HOME/bachman
(default ~/.local/state/bachman). Set the two variables in the service unit to move them.

config.toml:

    [update]
    repo = "your-name/bachman"       # where the releases are
    publisher = "your-github-login"  # only releases published by this login are installed

    [agenda]
    document = "https://docs.google.com/document/d/..."  # the planning document (address or id)
    start = "START"   # optional: a line starting with this opens the current episode ...
    end = "END"       # ... and a line that is exactly this closes it
    max_recent_episodes = 4        # optional: how many marked episodes the default answer lists
    sort_direction = "newest_first"  # optional: the newest marked episode is at the top ("oldest_first": bottom)
    template_start = "TEMPLATE"    # optional: the lines between these two marker lines are
    template_end = "TEMPLATE END"  # copied when a new episode is added
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PORT = 8766
CLIENT = "merlin"  # name of the one chat client; its token file is token-<name>
SORT_DIRECTIONS = ("newest_first", "oldest_first")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class UpdateConfig:
    repo: str
    publisher: str


@dataclass(frozen=True)
class AgendaConfig:
    document: str
    start: str = "START"
    end: str = "END"
    max_recent_episodes: int = 4
    sort_direction: str = "newest_first"
    template_start: str = "TEMPLATE"
    template_end: str = "TEMPLATE END"


@dataclass(frozen=True)
class Paths:
    conf: Path
    state: Path

    @classmethod
    def default(cls, env=os.environ, home: Path | None = None) -> "Paths":
        home = home or Path.home()

        def base(var: str, fallback: str) -> Path:
            value = env.get(var, "")
            return Path(value) if value.startswith("/") else home / fallback  # XDG: relative values are ignored

        return cls(base("XDG_CONFIG_HOME", ".config") / "bachman", base("XDG_STATE_HOME", ".local/state") / "bachman")

    @property
    def spotify(self) -> Path:
        return self.conf / "spotify"

    @property
    def token(self) -> Path:
        return self.conf / f"token-{CLIENT}"

    @property
    def google(self) -> Path:
        return self.conf / "google"

    @property
    def config(self) -> Path:
        return self.conf / "config.toml"


def _load(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path.name}: {type(e).__name__}") from None


def load_agenda(path: Path) -> AgendaConfig | None:
    """The [agenda] section with the id of the planning document, or None when it is not configured."""
    section = _load(path).get("agenda")
    if section is None:
        return None
    document = section.get("document")
    found = re.search(r"/document/d/([A-Za-z0-9_-]{20,})", document) if isinstance(document, str) else None
    doc_id = found.group(1) if found else document
    if not (isinstance(doc_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,}", doc_id)):
        raise ConfigError("[agenda] document must be the address or the id of a Google Doc")
    markers = {}
    for key in ("start", "end", "template_start", "template_end"):
        value = section.get(key)
        if value is None:
            continue
        if not (isinstance(value, str) and value.strip() and len(value) <= 40):
            raise ConfigError(f"[agenda] {key} must be a short text")
        markers[key] = value.strip()
    count = section.get("max_recent_episodes", 4)
    if not (type(count) is int and 1 <= count <= 100):
        raise ConfigError("[agenda] max_recent_episodes must be a whole number from 1 to 100")
    direction = section.get("sort_direction", "newest_first")
    if direction not in SORT_DIRECTIONS:
        raise ConfigError("[agenda] sort_direction must be newest_first or oldest_first")
    return AgendaConfig(doc_id, **markers, max_recent_episodes=count, sort_direction=direction)


def load_update(path: Path) -> UpdateConfig | None:
    """The [update] section, or None when updates are not configured."""
    section = _load(path).get("update")
    if section is None:
        return None
    repo, publisher = section.get("repo"), section.get("publisher")
    if not (isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)):
        raise ConfigError("[update] repo must look like owner/name")
    if not (isinstance(publisher, str) and re.fullmatch(r"[A-Za-z0-9-]+", publisher)):
        raise ConfigError("[update] publisher must be a GitHub login")
    return UpdateConfig(repo, publisher)


def load_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
    except OSError:
        raise ConfigError(f"{path.name} is missing") from None
    if len(token) < 32:
        raise ConfigError(f"{path.name} is too short")
    return token
