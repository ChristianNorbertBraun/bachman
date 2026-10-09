"""bubblewrap sandbox for running the unit tests of a downloaded release.

What the sandboxed process sees: the system read-only, a few /etc files, the unpacked release
at /work, a small tmpfs home and /tmp. It does NOT see the home of the service user, so the
stored credentials are out of reach until the release has passed its own tests.
"""
from __future__ import annotations

import shutil
from pathlib import Path

WORK = "/work"
SANDBOX_HOME = "/home/sandbox"
ETC_RO = ("ssl", "ca-certificates", "resolv.conf", "hosts", "nsswitch.conf", "passwd", "group",
          "ld.so.cache", "alternatives", "localtime")
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
TMP_BYTES = 256 * 1024 * 1024
HOME_BYTES = 64 * 1024 * 1024


def available() -> bool:
    return shutil.which("bwrap") is not None


def wrap(cmd: list[str], work: Path, etc_dir: Path = Path("/etc")) -> list[str]:
    """Prefix `cmd` with a bwrap invocation that only exposes `work` (read-write) and the system (read-only)."""
    args = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
        "--unshare-user", "--disable-userns",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev",
        "--size", str(TMP_BYTES), "--tmpfs", "/tmp",
        "--size", str(HOME_BYTES), "--tmpfs", SANDBOX_HOME,
    ]
    for name in ETC_RO:
        src = Path(etc_dir) / name
        if src.exists():
            args += ["--ro-bind", str(src), f"/etc/{name}"]
    return args + ["--bind", str(Path(work).resolve()), WORK, "--chdir", WORK, "--", *cmd]
