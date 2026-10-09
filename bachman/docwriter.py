"""The one write Bachman makes to Google Docs: adding the block for a new episode.

The planning document holds a template between two marker lines. `plan` copies its lines,
puts `START <topic>` before and `END` after them and returns the requests that insert the
block above the newest episode. Text is only ever inserted: no request here deletes or
replaces anything that is already in the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .agenda import Line
from .google import Google, GoogleError

BATCH_URL = "https://docs.googleapis.com/v1/documents/{}:batchUpdate"
WRITE_SCOPE = "https://www.googleapis.com/auth/documents"
MAX_TOPIC = 80
MAX_TEMPLATE_LINES = 200


class WriteRefused(Exception):
    """The block cannot be added. The text is shown to the chat agent."""


@dataclass(frozen=True)
class Plan:
    topic: str
    lines: tuple[Line, ...]   # the template lines that are copied
    index: int                # where the block is inserted
    tab_id: str
    requests: tuple[dict, ...]


def _is_start(text: str, start: str) -> bool:
    return text == start or text.startswith(start + " ")


def clean_topic(topic, start: str, end: str) -> str:
    if not isinstance(topic, str):
        raise WriteRefused("topic must be text")
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic or len(topic) > MAX_TOPIC:
        raise WriteRefused(f"topic must have 1 to {MAX_TOPIC} characters")
    if topic == end or any(ord(c) < 32 for c in topic):
        raise WriteRefused("topic must be plain text on one line")
    return topic


def template(all_lines: list[Line], begin: str, finish: str) -> list[Line]:
    """The lines between the template markers."""
    first = next((i for i, l in enumerate(all_lines) if l.text == begin), None)
    last = next((i for i in range((first or 0) + 1, len(all_lines)) if all_lines[i].text == finish), None) \
        if first is not None else None
    if first is None or last is None:
        raise WriteRefused(f"the document has no template: put it between a line {begin} and a line {finish}")
    found = all_lines[first + 1:last]
    if not found:
        raise WriteRefused("the template is empty")
    if len(found) > MAX_TEMPLATE_LINES:
        raise WriteRefused(f"the template has more than {MAX_TEMPLATE_LINES} lines")
    return found


def _location(index: int, tab_id: str) -> dict:
    return {"index": index, **({"tabId": tab_id} if tab_id else {})}


def _range(begin: int, finish: int, tab_id: str) -> dict:
    return {"startIndex": begin, "endIndex": finish, **({"tabId": tab_id} if tab_id else {})}


def plan(all_lines: list[Line], topic, start: str, end: str, begin: str, finish: str) -> Plan:
    """Work out where the new block goes and which requests create it."""
    topic = clean_topic(topic, start, end)
    if any(_is_start(l.text, start) and l.text[len(start):].strip().lower() == topic.lower() for l in all_lines):
        raise WriteRefused(f"the document already has an episode with the topic {topic}")
    copied = template(all_lines, begin, finish)
    # above the newest episode; without one, right after the template
    anchor = next((l for l in all_lines if _is_start(l.text, start)), None)
    if anchor is None:
        after = next(i for i, l in enumerate(all_lines) if l.text == finish)
        if after + 1 >= len(all_lines):
            raise WriteRefused("there is nothing after the template to insert the block in front of")
        anchor = all_lines[after + 1]
    if not anchor.index:
        raise WriteRefused("the document did not say where its paragraphs start")

    # one paragraph per line; list items lose their "- " and get one tab per nesting level instead,
    # which Docs turns into the nesting when the list is created
    rows: list[tuple[str, int, int]] = [(f"{start} {topic}", 0, -1)]
    for line in copied:
        text = line.text
        if line.bullet >= 0:
            text = "\t" * line.bullet + re.sub(r"^\s*- ", "", text)
        rows.append((text, line.level, line.bullet))
    rows.append((end, 0, -1))
    requests: list[dict] = [{"insertText": {"location": _location(anchor.index, anchor.tab_id),
                                            "text": "".join(text + "\n" for text, _, _ in rows)}}]
    spans, at = [], anchor.index
    for text, level, bullet in rows:
        spans.append((at, at + len(text) + 1, level, bullet))
        at += len(text) + 1
    if anchor.new_page:
        # the block went in after the page break of the episode below it: give that episode its break back
        requests.append({"insertPageBreak": {"location": _location(at, anchor.tab_id)}})
    # every new paragraph first becomes plain text, so nothing is inherited from the paragraph it was put before
    requests.append({"updateParagraphStyle": {"range": _range(anchor.index, at, anchor.tab_id),
                                              "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                              "fields": "namedStyleType"}})
    requests.append({"deleteParagraphBullets": {"range": _range(anchor.index, at, anchor.tab_id)}})
    # Neighbouring list items become one list in one request: only then do the leading tabs turn into nesting.
    # From the bottom up, because creating a list removes those tabs and so shifts everything after it.
    groups: list[list] = []
    for begin_at, end_at, level, bullet in spans:
        if bullet >= 0 and groups and groups[-1][2] == "list" and groups[-1][1] == begin_at:
            groups[-1][1] = end_at
        else:
            groups.append([begin_at, end_at, "list" if bullet >= 0 else level])
    for begin_at, end_at, kind in reversed(groups):
        if kind == "list":
            requests.append({"createParagraphBullets": {"range": _range(begin_at, end_at, anchor.tab_id),
                                                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
        elif kind:
            requests.append({"updateParagraphStyle": {"range": _range(begin_at, end_at, anchor.tab_id),
                                                      "paragraphStyle": {"namedStyleType": f"HEADING_{kind}"},
                                                      "fields": "namedStyleType"}})
    return Plan(topic, tuple(copied), anchor.index, anchor.tab_id, tuple(requests))


ALLOWED = {"insertText", "insertPageBreak", "updateParagraphStyle", "deleteParagraphBullets", "createParagraphBullets"}


def apply(google: Google, document_id: str, planned: Plan) -> None:
    """Send the requests of a plan. Refuses anything but the request kinds a plan is made of."""
    if WRITE_SCOPE not in google.granted():
        raise WriteRefused("the Google sign-in only allows reading: sign in again to allow adding an episode")
    for request in planned.requests:
        if set(request) - ALLOWED:
            raise WriteRefused("refused: unexpected request in the plan")
    resp = google.http.post(BATCH_URL.format(document_id),
                            headers={"Authorization": f"Bearer {google.access_token()}"},
                            json={"requests": list(planned.requests)}, timeout=60)
    if resp.status_code == 403:
        raise GoogleError("Google denied the change: the account may only view this document")
    if resp.status_code != 200:
        raise GoogleError(f"Google answered HTTP {resp.status_code} to the change; nothing was added")
