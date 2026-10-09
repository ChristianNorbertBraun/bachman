"""Read-only view of the show's YouTube channel: the newest uploads and their state."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .google import Google

API = "https://www.googleapis.com/youtube/v3/"
_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    description: str
    privacy: str            # private | public | unlisted
    publish_at: str | None  # UTC timestamp of a scheduled video
    upload_status: str
    seconds: int
    raw: dict               # the full resource, needed to keep the other fields when updating

    @property
    def minutes(self) -> float:
        return round(self.seconds / 60, 1)


def seconds(duration: str) -> int:
    """'PT50M47S' -> 3047."""
    m = _DURATION.fullmatch(duration or "")
    if not m:
        return 0
    days, hours, minutes, secs = (int(x or 0) for x in m.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + secs


def video_from(item: dict) -> Video:
    snippet, status = item.get("snippet") or {}, item.get("status") or {}
    return Video(item.get("id", ""), snippet.get("title", ""), snippet.get("description", ""),
                 status.get("privacyStatus", ""), status.get("publishAt"), status.get("uploadStatus", ""),
                 seconds((item.get("contentDetails") or {}).get("duration", "")), item)


def get(google: Google, video_id: str) -> Video | None:
    items = google.get_json(API + "videos", {"part": "snippet,status,contentDetails", "id": video_id}).get("items") or []
    return video_from(items[0]) if items else None


def uploads(google: Google, count: int = 10) -> list[Video]:
    """The newest uploads of the signed-in account's channel, private ones included."""
    channels = google.get_json(API + "channels", {"part": "contentDetails", "mine": "true"}).get("items") or []
    if not channels:
        return []
    playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    listed = google.get_json(API + "playlistItems", {"part": "contentDetails", "playlistId": playlist, "maxResults": count})
    ids = [i["contentDetails"]["videoId"] for i in listed.get("items") or []]
    if not ids:
        return []
    found = google.get_json(API + "videos", {"part": "snippet,status,contentDetails", "id": ",".join(ids)}).get("items") or []
    by_id = {v.get("id"): video_from(v) for v in found}
    return [by_id[i] for i in ids if i in by_id]
