"""Announce a released episode: start a discussion thread named like the episode, with the links.

Run every few minutes by a timer (`bachman announce`). It needs no record of what was
scheduled: it looks at what is actually out. An episode counts as released when Spotify
lists it with a publish time in the past. The thread is started once per episode; the
state file remembers which episodes are done.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .connectors import ConnectorError
from .connectors.discord import MAX_NAME, DiscordConnector
from .google import GoogleError
from .spotify import SpotifyError

DEFAULT_MESSAGE = "Spotify: {spotify}\nYouTube: {youtube}"


@dataclass(frozen=True)
class AnnounceConfig:
    connector: str = "discord"                # the instance under [connectors] that posts
    message: str = DEFAULT_MESSAGE            # first message in the thread; a line whose link is missing is left out
    min_minutes: float = 5.0                  # shorter episodes are test uploads and are skipped
    youtube_wait_minutes: int = 60            # how long to wait for the video before posting without its link


def _load(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def render(template: str, links: dict[str, str | None]) -> str:
    """Fill the links into the template. A line that names a link we do not have is dropped."""
    lines = []
    for line in template.splitlines():
        missing = [k for k, v in links.items() if "{" + k + "}" in line and not v]
        if missing:
            continue
        for key, value in links.items():
            line = line.replace("{" + key + "}", value or "")
        lines.append(line)
    return "\n".join(lines).strip()


def run(spotify, videos: Callable[[], list], discord: DiscordConnector, cfg: AnnounceConfig, state_path: Path,
        now: float, log: Callable[[str], None] = print, dry_run: bool = False) -> int:
    """One pass. Returns how many threads were started (or would be, in a dry run)."""
    state = _load(state_path)
    if "since" not in state:
        # first run: everything already out stays unannounced, only later releases count
        state = {"since": int(now), "done": {}}
        if not dry_run:
            _save(state_path, state)
        log("first run: episodes released from now on will be announced")
        return 0
    done = state.setdefault("done", {})
    try:
        episodes = spotify.episodes()
    except SpotifyError as e:
        log(f"cannot read the episode list: {e}")
        return 0
    due = [e for e in episodes
           if e.get("published") and not e.get("scheduled") and state["since"] <= e["published"] <= now
           and e.get("title") and e.get("minutes", 0) >= cfg.min_minutes
           and not done.get(e["id"], {}).get("complete")]
    if not due:
        return 0
    try:
        public = [v for v in videos() if v.privacy == "public"]
    except GoogleError as e:
        log(f"cannot read the YouTube uploads, continuing without them: {e}")
        public = []
    started = 0
    for episode in sorted(due, key=lambda e: e["published"]):
        title, entry = episode["title"].strip(), done.get(episode["id"], {})
        video = next((v for v in public if v.title.strip() == title), None)
        waited = (now - episode["published"]) / 60
        if video is None and waited < cfg.youtube_wait_minutes and not entry:
            log(f"{title!r}: out on Spotify, waiting for the YouTube video ({int(waited)} of {cfg.youtube_wait_minutes} min)")
            continue
        try:
            links = {"spotify": spotify.overview(episode["id"]).get("spotifyUrl"),
                     "youtube": f"https://youtu.be/{video.id}" if video else None}
            message = render(cfg.message, links)
            name = title if len(title) <= MAX_NAME else title[:MAX_NAME - 1] + "…"
            if dry_run:
                log(f"would start the thread {name!r} with:\n{message}")
                started += 1
                continue
            if not entry.get("thread"):
                if any((t.get("name") or "").strip() == name for t in discord.active_threads()):
                    # somebody started it by hand: leave that thread alone
                    done[episode["id"]] = {"complete": True, "note": "a thread with this name already existed"}
                    _save(state_path, state)
                    log(f"{name!r}: a thread with this name already exists, nothing posted")
                    continue
                made = discord.start_thread(name)
                entry = done[episode["id"]] = {"thread": made["id"], "complete": False}
                _save(state_path, state)  # remembered before the message goes out: never a second thread
                started += 1
            if message:
                discord.post_message(entry["thread"], message)
            entry.update(complete=True, at=dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            _save(state_path, state)
            log(f"{name!r}: thread started" + (" with links" if message else ""))
        except (ConnectorError, SpotifyError) as e:
            log(f"{title!r}: not announced yet, will try again: {e}")
    return started
