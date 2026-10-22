"""Starting Docker Desktop for the user, and giving up honestly.

Every first user met "Docker is installed but not running" (2026-10-22). On a
Mac the engine now runs `open -a Docker` and waits; where that cannot work (no
Docker, Linux, Docker Desktop never opened once) it answers False so the
caller can say what to do. Offline: a fake `run` and no sleeping.
"""
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import platform_support as ps  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


class Fake:
    """`docker info` answers from the `up_after`th call on; `open` returns open_rc."""

    def __init__(self, up_after=None, open_rc=0):
        self.calls, self.up_after, self.open_rc, self.infos = [], up_after, open_rc, 0

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[0])
        if cmd[0] == "open":
            return type("R", (), {"returncode": self.open_rc})()
        self.infos += 1
        up = self.up_after is not None and self.infos >= self.up_after
        return type("R", (), {"returncode": 0 if up else 1})()


naps = []
sleep = naps.append
say = []
with mock.patch.object(ps.shutil, "which", lambda t: "/usr/local/bin/docker"):
    with mock.patch.object(ps, "IS_MAC", True):
        f = Fake(up_after=1)
        check("Docker already running: nothing is opened", ps.ensure_docker(run=f, sleep=sleep) and "open" not in f.calls)

        f = Fake(up_after=4)
        ok = ps.ensure_docker(run=f, sleep=sleep, say=say.append)
        check("a Mac with Docker Desktop closed: it is opened and waited for", ok and f.calls[1] == "open"
              and say and len(naps) == 3, f"({f.calls}, {naps})")

        naps.clear()
        f = Fake(up_after=None)
        check("Docker that never comes up: gives up after `wait`, False",
              ps.ensure_docker(wait=30, run=f, sleep=sleep) is False and sum(naps) == 30, str(sum(naps)))

        f = Fake(up_after=None, open_rc=1)
        check("Docker Desktop that cannot be opened: False at once",
              ps.ensure_docker(run=f, sleep=sleep) is False and f.calls.count("open") == 1)
    with mock.patch.object(ps, "IS_MAC", False):
        f = Fake(up_after=None)
        check("Linux: never tries to open anything", ps.ensure_docker(run=f, sleep=sleep) is False
              and "open" not in f.calls)

with mock.patch.object(ps.shutil, "which", lambda t: None), mock.patch.object(ps, "IS_MAC", True):
    f = Fake()
    check("no Docker installed: False, nothing run", ps.ensure_docker(run=f, sleep=sleep) is False and f.calls == [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
