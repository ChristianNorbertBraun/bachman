"""The one write Bachman makes to Spotify for Creators: title, description and publish time of a draft.

It sends what the web app sends when you press "Schedule" in its editor. Nothing here
uploads, deletes or publishes right away: the episode must be an unpublished draft (or
already scheduled) and the publish time must lie in the future.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser

from .rules import Refused, forbidden_in
from .spotify import REST, REST_HEADERS, Spotify, SpotifyError

MAX_TITLE = 200
MAX_TEXT = 4000   # characters of visible text in the description
ALLOWED_TAGS = ("p", "strong", "a", "ul", "ol", "li")
ORIGIN = "https://creators.spotify.com"


@dataclass(frozen=True)
class Change:
    episode_id: str
    title: str
    description: str          # HTML, one line
    publish_at: dt.datetime   # timezone-aware
    sponsored: bool | None    # None: leave the "contains paid promotion" setting as it is

    @property
    def publish_iso(self) -> str:
        return self.publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @property
    def publish_unix(self) -> int:
        return int(self.publish_at.timestamp())

    @property
    def code(self) -> str:
        """Ties a confirmation to exactly these values."""
        blob = json.dumps([self.episode_id, self.title, self.description, self.publish_iso, self.sponsored], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:8]


class _Checker(HTMLParser):
    """Collects the visible text and every problem with tags and attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.problems, self.open = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"the tag <{tag}> is not allowed")
            return
        self.open.append(tag)
        if tag == "a":
            names = [k for k, _ in attrs]
            href = dict(attrs).get("href") or ""
            if names != ["href"] or not re.fullmatch(r"https?://[^\s\"'<>]+", href):
                self.problems.append("a link must have exactly one attribute, href, with an http or https address")
        elif attrs:
            self.problems.append(f"the tag <{tag}> must not have attributes")
        if tag in ("p", "li"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in self.open:
            while self.open and self.open.pop() != tag:
                pass
        else:
            self.problems.append(f"</{tag}> closes nothing")

    def handle_data(self, data):
        self.text.append(data)


def check_text(title, description, forbidden: tuple[str, ...] = ()) -> tuple[str, str, str]:
    """Return (title, HTML on one line, visible text), or refuse with every problem named."""
    if not isinstance(title, str) or not isinstance(description, str):
        raise Refused("title and description must be text")
    title = title.strip()
    html = re.sub(r"\s*\n\s*", "", description.strip())  # the web app sends the description without line breaks
    problems = []
    if not title:
        problems.append("the title is empty")
    if len(title) > MAX_TITLE:
        problems.append(f"the title has {len(title)} characters, at most {MAX_TITLE} are allowed")
    if "\n" in title or "<" in title or ">" in title:
        problems.append("the title must be one line of plain text")
    checker = _Checker()
    checker.feed(html)
    checker.close()
    text = re.sub(r"\n+", "\n", "".join(checker.text)).strip()
    problems += sorted(set(checker.problems))
    if checker.open:
        problems.append(f"the tag <{checker.open[-1]}> is not closed")
    if not html.startswith("<"):
        problems.append("the description must be HTML: put every paragraph in <p>...</p>")
    if not text:
        problems.append("the description is empty")
    if len(text) > MAX_TEXT:
        problems.append(f"the description has {len(text)} characters of text, at most {MAX_TEXT} are allowed")
    problems += forbidden_in("title", title, forbidden) + forbidden_in("description", text, forbidden)
    if problems:
        raise Refused("; ".join(problems))
    return title, html, text


def check_episode(overview: dict) -> None:
    if overview.get("isDeleted"):
        raise Refused("this episode was deleted")
    published_at = overview.get("publishOnUnixTimestamp")
    if overview.get("isPublished") or (published_at and int(published_at) <= time.time()):
        raise Refused("this episode is already published; only a draft or a scheduled episode can be scheduled")
    audios = overview.get("episodeAudios") or []
    if not audios:
        raise Refused("this draft has no audio or video yet")
    if any((a.get("audioTransformationStatus") or "finished") != "finished" for a in audios):
        raise Refused("Spotify is still processing the upload of this draft")


def body(overview: dict, change: Change) -> dict:
    """The update request as the web app's editor builds it when scheduling."""
    out = {
        "userId": overview["userId"],
        "title": change.title,
        "description": change.description,
        "episodeType": overview.get("podcastEpisodeType") or "full",
        "podcastEpisodeIsExplicit": bool(overview.get("podcastEpisodeIsExplicit")),
        "isVideoEighteenPlus": bool(overview.get("isVideoEighteenPlus")),
        "isPublished": True,
        "publishOn": change.publish_iso,
        "wizardDraftedToPublishOn": change.publish_iso,
    }
    for ours, theirs in (("seasonNumber", "podcastSeasonNumber"), ("episodeNumber", "podcastEpisodeNumber")):
        if overview.get(theirs) is not None:
            out[ours] = overview[theirs]
    return out


def _plain(html: str) -> str:
    """HTML without the attributes Spotify adds to links, for comparing what was sent with what is stored."""
    return re.sub(r'\s+(rel|target)="[^"]*"', "", html or "")


def _matches(overview: dict, change: Change) -> bool:
    return (overview.get("title") == change.title and _plain(overview.get("description")) == change.description
            and overview.get("publishOnUnixTimestamp") == change.publish_unix and not overview.get("isPublished"))


def apply(spotify: Spotify, overview: dict, change: Change, sleep=time.sleep, attempts: int = 4) -> bool:
    """Send the change. True when a later read shows the new values; False when Spotify accepted the change
    but a read still shows other values after a few seconds."""
    headers = {"Authorization": f"Bearer {spotify.bearer()}", "Accept": "application/json", "Content-Type": "application/json",
               "Origin": ORIGIN, "Referer": ORIGIN + "/", **REST_HEADERS}
    params = {"isMumsCompatible": "true"}
    base = f"{REST}/v3/episodes/{change.episode_id}"
    if change.sponsored is not None:
        resp = spotify.http.put(f"{base}/sponsoredContentStatus", params=params, headers=headers, timeout=60,
                                data=json.dumps({"containsSponsoredContent": change.sponsored, "publishOn": change.publish_unix * 1000}))
        if resp.status_code != 200:
            raise SpotifyError(f"Spotify rejected the paid-promotion setting (HTTP {resp.status_code}); nothing was scheduled")
    resp = spotify.http.post(f"{base}/update", params=params, headers=headers, timeout=60, data=json.dumps(body(overview, change)))
    if resp.status_code != 200:
        raise SpotifyError(f"Spotify rejected the change (HTTP {resp.status_code}); the episode was not scheduled")
    for attempt in range(attempts):
        if _matches(spotify.overview(change.episode_id), change):
            return True
        if attempt < attempts - 1:
            sleep(2)
    return False
