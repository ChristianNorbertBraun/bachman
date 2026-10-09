"""Bachman command line (run as the service user).

  python3 -m bachman serve                  run the MCP endpoint (bachman.service)
  python3 -m bachman update [--check] [--yes] [--to vX.Y.Z] [--force]
                                            install the newest release published by you on GitHub
  python3 -m bachman google-login           sign in to the podcast's Google account (once, interactive)
  python3 -m bachman agenda [section]       print the current episode's notes, or one section (or: topics, outline)
  python3 -m bachman config-check           can this code read the installed config?
  python3 -m bachman version
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from . import config, updater
from .version import __version__


def cmd_serve(_: argparse.Namespace) -> int:
    import requests

    from .bridge import Bridge, serve
    from .google import Google
    from .spotify import Spotify

    paths = config.Paths.default()
    token = config.load_token(paths.token)
    spotify = Spotify(requests, paths.spotify, paths.state / "ops.json")
    bridge = Bridge(spotify, update=config.load_update(paths.config),
                    last_result=lambda: updater.last_result(updater.Layout(state_dir=paths.state)),
                    google=Google(requests, paths.google), agenda=config.load_agenda(paths.config),
                    publish=config.load_publish(paths.config))
    server = serve(bridge, {config.CLIENT: token}, config.PORT)
    updater.mark_running(updater.Layout(state_dir=paths.state))
    print(f"bachman {__version__} on 127.0.0.1:{config.PORT} for {config.CLIENT}")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    return 0


def cmd_config_check(_: argparse.Namespace) -> int:
    paths = config.Paths.default()
    config.load_token(paths.token)
    update = config.load_update(paths.config)
    document = config.load_agenda(paths.config)
    config.load_publish(paths.config)
    print(f"config ok (updates {'on' if update else 'off'}, planning document {'set' if document else 'not set'})")
    return 0


def cmd_google_login(_: argparse.Namespace) -> int:
    import requests

    from .google import Google, GoogleError

    google = Google(requests, config.Paths.default().google)
    try:
        url, state, verifier = google.start_login()
        print("1. Open this address in a browser that is signed in to the podcast's Google account:\n")
        print(url)
        print("\n2. Agree. The browser then shows a page that cannot be loaded (127.0.0.1): that is expected.")
        pasted = input("3. Copy the full address of that page from the address bar and paste it here:\n> ")
        granted = google.finish_login(pasted, state, verifier)
    except GoogleError as e:
        print(f"sign-in failed: {e}", file=sys.stderr)
        return 1
    print("signed in. Granted access:")
    for scope in granted:
        print(f"  {scope}")
    return 0


def cmd_agenda(a: argparse.Namespace) -> int:
    import requests

    from .bridge import Bridge
    from .google import Google

    paths = config.Paths.default()
    bridge = Bridge(None, log=lambda *_: None, google=Google(requests, paths.google),
                    agenda=config.load_agenda(paths.config))
    result = bridge._call("podcast_get_agenda", {"section": a.section} if a.section else {})
    print(result["content"][0]["text"])
    return 1 if result["isError"] else 0


def cmd_update(a: argparse.Namespace) -> int:
    paths = config.Paths.default()
    cfg = config.load_update(paths.config)
    if cfg is None:
        print("update is off: add an [update] section (repo, publisher) to config.toml", file=sys.stderr)
        return 2
    try:
        release = updater.find_release(cfg, to=a.to)
    except updater.UpdateRefused as e:
        print(f"no update: {e}", file=sys.stderr)
        return 1
    newer = updater.is_newer(release)
    print(f"installed {__version__}, release {release.tag}{' (newer)' if newer else ''}")
    if a.check:
        print(release.notes[:500] if newer else "up to date")
        return 0
    if not (newer or a.force):
        print("nothing to do")
        return 0
    if not a.yes and input(f"install {release.tag}? [y/N] ").strip().lower() != "y":
        return 1
    result = updater.update(cfg, updater.Layout(state_dir=paths.state), updater.Deps(), to=a.to, force=a.force)
    return 0 if result.status in ("updated", "current") else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="bachman", description="Narrow MCP tools for preparing podcast episodes.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("config-check").set_defaults(fn=cmd_config_check)
    sub.add_parser("google-login").set_defaults(fn=cmd_google_login)
    ag = sub.add_parser("agenda")
    ag.add_argument("section", nargs="?")
    ag.set_defaults(fn=cmd_agenda)
    sub.add_parser("version").set_defaults(fn=lambda _: print(__version__) or 0)
    up = sub.add_parser("update")
    up.add_argument("--check", action="store_true", help="only say what would be installed")
    up.add_argument("--yes", action="store_true", help="do not ask before installing")
    up.add_argument("--to", help="install this tag instead of the newest release")
    up.add_argument("--force", action="store_true", help="also install an older or equal version")
    up.set_defaults(fn=cmd_update)
    args = parser.parse_args()
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
