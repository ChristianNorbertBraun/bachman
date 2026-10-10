"""Connectors: one module per outside service, switched on and set up in config.toml.

A connector knows how to talk to one service and offers tools for it. It is written once and
used by every instance of this program that enables it: another instance (another unix user,
another port, another config.toml) gets its own credentials and settings, never its own copy
of the code.

    [connectors.trello]          # the table being present switches the connector on
    board = "https://trello.com/b/..."
    write = true                 # offer the tools that change something (default: false)

    [connectors.devboard]        # a second Trello, for another account or another default board
    type = "trello"              # the table name is free; `type` says which connector it is
    board = "https://trello.com/b/..."

The table name is the instance name. It is the prefix of the tool names (`trello_get_board`,
`devboard_get_board`) and the name of the credential directory, <config dir>/<instance>/,
where a setup script stores the credentials with mode 600. They are never in config.toml.
Which chat client may use which instance is set under [clients], see config.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ConnectorError(Exception):
    """A failure that is safe to show to the chat agent (no secrets, no internals)."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str          # the first sentence states the purpose; a tool that changes something says WRITES
    schema: dict              # JSON schema of the arguments
    handler: Callable[[dict], str]
    writes: bool = False


class Connector:
    """Base class. `settings` is the connector's table from config.toml, `conf_dir` its credential directory."""

    name = ""   # the connector type

    def __init__(self, settings: dict, conf_dir: Path, http, instance: str | None = None):
        self.settings, self.conf_dir, self.http = settings, conf_dir, http
        self.instance = instance or self.name
        self.check_settings()

    def check_settings(self) -> None:
        """Raise ConnectorError for a setting that cannot work. Runs at start and in config-check."""

    def all_tools(self) -> list[Tool]:
        raise NotImplementedError

    def tools(self) -> list[Tool]:
        """The tools this instance offers: the writing ones only when `write = true` is set."""
        allow = self.settings.get("write", False)
        if not isinstance(allow, bool):
            raise ConnectorError(f"[connectors.{self.instance}] write must be true or false")
        return [t for t in self.all_tools() if allow or not t.writes]


RESERVED = {"podcast", "bachman", "spotify", "google"}  # groups and credential directories of the built-in tools


def registry() -> dict[str, type[Connector]]:
    from .trello import TrelloConnector

    return {c.name: c for c in (TrelloConnector,)}


def load(tables: dict, conf: Path, http) -> list[Connector]:
    """Build the connectors that config.toml enables. An unknown name is an error, not a silent skip."""
    known = registry()
    out = []
    for instance, settings in (tables or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9]{1,23}", instance) or instance in RESERVED:
            raise ConnectorError(f"[connectors.{instance}]: an instance name is 2 to 24 lowercase letters and digits "
                                 f"and none of {', '.join(sorted(RESERVED))}")
        if not isinstance(settings, dict):
            raise ConnectorError(f"[connectors.{instance}] must be a table")
        kind = settings.get("type", instance)
        if kind not in known:
            raise ConnectorError(f"[connectors.{instance}]: {kind} is not a connector this version knows ({', '.join(sorted(known))})")
        out.append(known[kind](settings, conf / instance, http, instance))
    return out
