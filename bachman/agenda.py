"""The planning document of the podcast: one long Google Doc that keeps growing.

The document is turned into plain text lines. Two ways lead to the part that matters:

- Markers. Many shows keep the episodes they are working on at the top, each between a line
  that starts with a start marker ("START <topic>") and a line that is the end marker ("END").
  `marked` returns exactly those parts.
- Sections. A heading or a page break starts a section, so the chat agent can ask for the
  outline and then for one section, instead of loading the whole document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DOCS_API = "https://docs.googleapis.com/v1/documents/"

@dataclass(frozen=True)
class Section:
    title: str
    level: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass(frozen=True)
class Line:
    text: str
    level: int         # heading level, 0 for normal text
    new_page: bool     # a page break or a new tab comes right before this line
    tab: str = ""      # set on the first line of a tab


def _paragraph(paragraph: dict) -> tuple[str, int]:
    """Return (text with link targets written out, heading level or 0)."""
    parts = []
    for el in paragraph.get("elements") or []:
        run = el.get("textRun")
        if run:
            content = run.get("content", "")
            url = ((run.get("textStyle") or {}).get("link") or {}).get("url")
            shown = content.strip()
            if url and shown and url.rstrip("/") != shown.rstrip("/"):
                content = content.replace(shown, f"{shown} ({url})")
            parts.append(content)
            continue
        rich = (el.get("richLink") or {}).get("richLinkProperties") or {}
        if rich.get("uri"):
            parts.append(f"{rich.get('title') or 'Link'} ({rich['uri']})")
    text = "".join(parts).replace("\u000b", " ").strip()
    style = (paragraph.get("paragraphStyle") or {}).get("namedStyleType", "")
    level = 1 if style == "TITLE" else int(style[-1]) if re.fullmatch(r"HEADING_[1-6]", style) else 0
    if paragraph.get("bullet") and text:
        text = "  " * int(paragraph["bullet"].get("nestingLevel") or 0) + "- " + text
    return text, level


def _bodies(document: dict):
    """The body of every tab (documents with tabs), or the single body of an old-style response."""
    def walk(tabs):
        for tab in tabs or []:
            yield ((tab.get("documentTab") or {}).get("body") or {}, (tab.get("tabProperties") or {}).get("title", ""))
            yield from walk(tab.get("childTabs"))

    if document.get("tabs"):
        yield from walk(document["tabs"])
    else:
        yield document.get("body") or {}, ""


def lines(document: dict) -> list[Line]:
    """Every non-empty paragraph of the document in reading order."""
    out: list[Line] = []
    for body, tab_title in _bodies(document):
        pending_break, pending_tab = bool(tab_title), tab_title
        for block in body.get("content") or []:
            paragraph = block.get("paragraph")
            if not paragraph:
                continue
            if any("pageBreak" in el for el in paragraph.get("elements") or []):
                pending_break = True
            text, level = _paragraph(paragraph)
            if text:
                out.append(Line(text, level, pending_break, pending_tab))
                pending_break, pending_tab = False, ""
    return out


def _render(line: Line) -> str:
    return f"{'#' * line.level} {line.text}" if line.level else line.text


def marked(all_lines: list[Line], start: str, end: str) -> list[tuple[str, str]]:
    """Every part between a line that starts with the start marker and the next line that is the end marker,
    as (topic written after the start marker, text). Several episodes can be in preparation at once. A start
    marker without an end marker is ignored."""
    out, i = [], 0
    while i < len(all_lines):
        text = all_lines[i].text
        if text == start or text.startswith(start + " "):
            last = next((j for j in range(i + 1, len(all_lines)) if all_lines[j].text == end), None)
            if last is None:
                break
            out.append((text[len(start):].strip(), "\n".join(_render(l) for l in all_lines[i + 1:last]).strip()))
            i = last
        i += 1
    return out


def sections(all_lines: list[Line]) -> list[Section]:
    """Split the lines into sections. A tab or a page break starts a container (level 0, a page is named after
    its first line); a heading starts a section inside it, at its own heading level."""
    out: list[tuple[str, int, list[str]]] = []

    def start(title: str, level: int) -> None:
        out.append((title, level, []))

    for line in all_lines:
        if line.tab:
            start(f"Tab: {line.tab}", 0)
        elif line.new_page and not line.level:
            start(f"Page: {line.text.lstrip('- ')[:60]}", 0)
        if line.level:
            start(line.text, line.level)
            continue
        if not out:
            start("(start of the document)", 0)
        out[-1][2].append(line.text)
    return [Section(title, level, tuple(body)) for title, level, body in out]


def find(all_sections: list[Section], wanted: str) -> list[int]:
    """Indexes of the sections whose title matches: by position ("#12"), exactly, or by containing the text."""
    wanted = wanted.strip()
    if re.fullmatch(r"#\d+", wanted):
        return [int(wanted[1:])] if int(wanted[1:]) < len(all_sections) else []
    low = wanted.lower()
    exact = [i for i, s in enumerate(all_sections) if s.title.lower() == low]
    return exact or [i for i, s in enumerate(all_sections) if low in s.title.lower()]


def with_children(all_sections: list[Section], index: int) -> str:
    """The text of a section together with the deeper sections that follow it."""
    head = all_sections[index]
    parts = [head.text]
    for s in all_sections[index + 1:]:
        if s.level <= head.level:
            break
        parts.append(f"{'#' * s.level} {s.title}\n{s.text}".strip())
    return "\n\n".join(p for p in parts if p)
