"""The one write Bachman makes to YouTube: title, description and publish time of a private video.

Nothing here uploads, deletes or publishes right away. A video must be private, the publish
time must lie in the future, and every other field of the video is sent back unchanged,
because the API deletes fields that an update leaves out.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import youtube
from .google import Google, GoogleError

SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
MAX_TITLE = 100             # characters
MAX_DESCRIPTION = 5000      # bytes, as the API counts them
MIN_LEAD = dt.timedelta(minutes=15)
MAX_LEAD = dt.timedelta(days=366)
KEEP_SNIPPET = ("categoryId", "tags", "defaultLanguage", "defaultAudioLanguage")
KEEP_STATUS = ("embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia")


class Refused(Exception):
    """The change is not acceptable. The text is shown to the chat agent."""


@dataclass(frozen=True)
class Change:
    video_id: str
    title: str
    description: str
    publish_at: dt.datetime  # timezone-aware

    @property
    def publish_utc(self) -> str:
        return self.publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def code(self) -> str:
        """Ties a confirmation to exactly these values."""
        blob = json.dumps([self.video_id, self.title, self.description, self.publish_utc], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:8]


def parse_time(value, zone: str, now: dt.datetime) -> dt.datetime:
    """'2026-01-31T06:00' in the configured time zone, or a timestamp with its own offset."""
    if not isinstance(value, str):
        raise Refused("publish_at must be a time like 2026-01-31T06:00")
    try:
        when = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise Refused("publish_at must be a time like 2026-01-31T06:00") from None
    if when.tzinfo is None:
        try:
            when = when.replace(tzinfo=ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            raise Refused(f"the configured time zone {zone} is unknown") from None
    if when < now + MIN_LEAD:
        raise Refused("publish_at must be at least 15 minutes in the future")
    if when > now + MAX_LEAD:
        raise Refused("publish_at is more than a year away")
    return when


def check_text(title, description, forbidden: tuple[str, ...] = ()) -> tuple[str, str]:
    """Return the cleaned title and description, or refuse with every problem named."""
    if not isinstance(title, str) or not isinstance(description, str):
        raise Refused("title and description must be text")
    title, description = title.strip(), description.strip()
    problems = []
    if not title:
        problems.append("the title is empty")
    if len(title) > MAX_TITLE:
        problems.append(f"the title has {len(title)} characters, YouTube allows {MAX_TITLE}")
    if "\n" in title:
        problems.append("the title must be one line")
    if len(description.encode()) > MAX_DESCRIPTION:
        problems.append(f"the description has {len(description.encode())} bytes, YouTube allows {MAX_DESCRIPTION}")
    for name, text in (("title", title), ("description", description)):
        if "<" in text or ">" in text:
            problems.append(f"the {name} contains < or >, which YouTube rejects")
        found = [c for c in forbidden if c and c in text]
        if found:
            problems.append(f"the {name} contains {', '.join(repr(c) for c in found)}, which the owner does not allow")
    if problems:
        raise Refused("; ".join(problems))
    return title, description


def check_video(video: youtube.Video | None) -> None:
    if video is None:
        raise Refused("no video with this id on the channel")
    if video.privacy != "private":
        raise Refused(f"this video is {video.privacy}; only a private video can be scheduled")
    if video.upload_status not in ("processed", "uploaded"):
        raise Refused(f"this video is not ready (upload status: {video.upload_status})")


def body(video: youtube.Video, change: Change) -> dict:
    """The update request: the new values plus every other field as it is now."""
    snippet, status = video.raw.get("snippet") or {}, video.raw.get("status") or {}
    return {
        "id": video.id,
        "snippet": {**{k: snippet[k] for k in KEEP_SNIPPET if k in snippet},
                    "title": change.title, "description": change.description},
        "status": {**{k: status[k] for k in KEEP_STATUS if k in status},
                   "privacyStatus": "private", "publishAt": change.publish_utc},
    }


def _matches(item: dict, change: Change) -> bool:
    snippet, status = item.get("snippet") or {}, item.get("status") or {}
    return (snippet.get("title") == change.title and snippet.get("description") == change.description
            and status.get("privacyStatus") == "private"
            and (status.get("publishAt") or "").rstrip("Z").split(".")[0] == change.publish_utc.rstrip("Z"))


def apply(google: Google, video: youtube.Video, change: Change, sleep=time.sleep, attempts: int = 5) -> bool:
    """Send the update. True when a later read shows the new values too; False when YouTube accepted the
    change but still shows old values after a few seconds (its reads lag behind its writes)."""
    if SCOPE not in google.granted():
        raise Refused("the Google sign-in does not include YouTube: sign in again")
    resp = google.http.put(youtube.API + "videos", params={"part": "snippet,status"},
                           headers={"Authorization": f"Bearer {google.access_token()}"}, json=body(video, change), timeout=60)
    if resp.status_code != 200:
        try:
            reason = ((resp.json().get("error") or {}).get("errors") or [{}])[0].get("reason", "")
        except ValueError:
            reason = ""
        raise GoogleError(f"YouTube rejected the change (HTTP {resp.status_code}{', ' + reason[:60] if reason else ''}); nothing was changed")
    if not _matches(resp.json(), change):
        raise GoogleError("YouTube answered with other values than were sent: check the video in YouTube Studio")
    for attempt in range(attempts):
        seen = youtube.get(google, video.id)
        if seen is not None and _matches(seen.raw, change):
            return True
        if attempt < attempts - 1:
            sleep(2)
    return False
