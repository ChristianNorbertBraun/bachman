"""Paths and the optional config file of the service user.

Everything lives in the XDG directories of that user: config and credentials in
$XDG_CONFIG_HOME/bachman (default ~/.config/bachman), state in $XDG_STATE_HOME/bachman
(default ~/.local/state/bachman). Set the two variables in the service unit to move them.

config.toml:

    [update]
    repo = "your-name/bachman"       # where the releases are
    publisher = "your-github-login"  # only releases published by this login are installed
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PORT = 8766
CLIENT = "merlin"  # name of the one chat client; its token file is token-<name>


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class UpdateConfig:
    repo: str
    publisher: str


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
    def config(self) -> Path:
        return self.conf / "config.toml"


def load_update(path: Path) -> UpdateConfig | None:
    """The [update] section, or None when updates are not configured."""
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path.name}: {type(e).__name__}") from None
    section = data.get("update")
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
