"""Rules shared by the tools that write to a platform: when an episode may go out and what its texts may contain."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_LEAD = dt.timedelta(minutes=15)
MAX_LEAD = dt.timedelta(days=366)


class Refused(Exception):
    """The change is not acceptable. The text is shown to the chat agent."""


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


def forbidden_in(name: str, text: str, forbidden: tuple[str, ...]) -> list[str]:
    """One problem line when `text` contains something the owner does not allow."""
    found = [c for c in forbidden if c and c in text]
    return [f"the {name} contains {', '.join(repr(c) for c in found)}, which the owner does not allow"] if found else []
