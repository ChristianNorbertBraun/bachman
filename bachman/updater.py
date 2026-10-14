"""Self-update through agent-tool-selfupdate (github.com/ChristianNorbertBraun/agent-tool-selfupdate).

The library is not part of this repository: an installation puts the newest release that selfupdate.toml allows
next to this package, so it is imported only when an update function runs. The service and the tests work
without it. What an update checks before it switches (tests in the sandbox without access to the stored
credentials, a probe of the new updater, config check, health check and rollback) is described in the library's
README."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import UpdateConfig

PACKAGE = "bachman"
MISSING = ("the updater library agent-tool-selfupdate is not installed next to this version; "
           "install it once as described in its README (\"First installation\")")


class UpdateRefused(Exception):
    """No update is possible. The text is safe to show."""


def _lib():
    try:
        import agent_tool_selfupdate as su
    except ImportError:
        raise UpdateRefused(MISSING) from None
    return su


def _layout(su, root: Path | None):
    return su.Layout(root) if root else su.Layout()  # the home of the service user


def installed() -> str:
    """The release directory this code runs from, e.g. 0.12.0+u1.0.0 (in a checkout: its folder name)."""
    return Path(__file__).resolve().parents[1].name


def mark_running(state: Path) -> None:
    """The service records which release it runs, so the updater can see the restart worked."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / "running-version").write_text(installed())


def last_result(state: Path) -> str:
    """What the last update attempt reported, for a client that could not wait for it."""
    try:
        import agent_tool_selfupdate as su
    except ImportError:
        return ""
    return su.last_result(state)


def plan(cfg: UpdateConfig, to: str | None = None, fetch: Callable[[str], Any] | None = None,
         root: Path | None = None):
    """What an update would install, compared with the release this code runs from: .summary(), .notes(),
    .newer, .target. Downloads no archive."""
    su = _lib()
    try:
        extra = {"fetch": fetch} if fetch else {}
        return su.plan(su.Source(cfg.repo, cfg.publisher), _layout(su, root), to=to, installed=installed(), **extra)
    except su.UpdateRefused as e:
        raise UpdateRefused(str(e)) from None


def spawn() -> None:
    """Start `python3 -m bachman update --yes` in its own systemd unit, so restarting the service does not kill it."""
    su = _lib()
    try:
        su.spawn(PACKAGE)
    except su.UpdateRefused as e:
        raise UpdateRefused(str(e)) from None


def update(cfg: UpdateConfig, state: Path, to: str | None = None, force: bool = False, root: Path | None = None,
           **deps: Any) -> str:
    """Install the newest release. Returns the status: updated | current | failed | rolled-back."""
    su = _lib()
    result = su.update(su.Source(cfg.repo, cfg.publisher), _layout(su, root), su.Deps(**deps), to=to, force=force,
                       state_dir=state)
    return result.status
