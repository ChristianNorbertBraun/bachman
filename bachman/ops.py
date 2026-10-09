"""Persisted GraphQL operations of the Spotify for Creators web app.

creators-graph.spotify.com only accepts queries by hash. The hashes are compiled into the
public web bundle, so they are read from there and cached in a JSON file. A new web deploy
can change them; `refresh` rebuilds the cache.
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

APP_URL = "https://creators.spotify.com/pod/dashboard/episodes"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
_BUNDLE = re.compile(r'src="(https://[A-Za-z0-9.-]+/builds/bundle-[0-9a-f]+\.js)"')
_HEAD = re.compile(
    r'__meta__:\{hash:"([0-9a-f]{64})"\},kind:"Document",definitions:\[\{kind:"OperationDefinition",'
    r'operation:"(query|mutation|subscription)",name:\{kind:"Name",value:"([A-Za-z0-9_]+)"\}'
)


class OpsError(Exception):
    pass


def extract(bundle: str) -> dict[str, dict]:
    """Map operation name -> {"hash", "type"} for every operation in a decoded bundle."""
    return {m.group(3): {"hash": m.group(1), "type": m.group(2)} for m in _HEAD.finditer(bundle)}


def _text(resp) -> str:
    raw = resp.content
    if raw[:2] == b"\x1f\x8b":  # the CDN serves the bundle gzipped even without Content-Encoding
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def refresh(http, path: Path) -> dict[str, dict]:
    """Download the current web bundle, extract its operations and write them to `path`."""
    page = http.get(APP_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    page.raise_for_status()
    found = _BUNDLE.search(_text(page))
    if not found:
        raise OpsError("no script bundle found in the web app page")
    bundle = http.get(found.group(1), headers={"User-Agent": USER_AGENT}, timeout=60)
    bundle.raise_for_status()
    ops = extract(_text(bundle))
    if not ops:
        raise OpsError("no operations found in the web bundle")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ops, indent=1))
    return ops


def load(path: Path) -> dict[str, dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
