"""The planning document of the podcast: one long Google Doc that keeps growing.

The document is turned into plain text lines. A heading starts a section, so the chat agent
can ask for the outline first and then for the one section of the episode it works on,
instead of loading the whole document.
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


def sections(document: dict) -> list[Section]:
    """Split a Docs API document into sections at its headings. Text before the first heading is its own section."""
    out: list[Section] = []
    title, level, lines = "(start of the document)", 0, []

    def close():
        if lines or level:
            out.append(Section(title, level, tuple(lines)))

    for body, tab_title in _bodies(document):
        if tab_title:
            close()
            title, level, lines = f"Tab: {tab_title}", 1, []
        for block in body.get("content") or []:
            paragraph = block.get("paragraph")
            if not paragraph:
                continue
            text, heading = _paragraph(paragraph)
            if heading and text:
                close()
                title, level, lines = text, heading, []
            elif text:
                lines.append(text)
    close()
    return out


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
