"""The narration pipeline runs on Linux as well as macOS.

Run on this Mac by switching the platform flags, so a change that breaks
the Linux branch fails here and not on someone's server. CI runs the whole
suite on Ubuntu besides. The Kobo installer is macOS-only for now and must
say so instead of failing somewhere inside /Volumes.
"""
import json, os, sys, tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TTS_PROVIDER", "cosyvoice")
from core import platform_support as ps  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def on(system):
    return mock.patch.multiple(ps, IS_MAC=(system == "mac"), IS_LINUX=(system == "linux"))


with on("mac"):
    check("Mac: caffeinate holds off sleep, as before", ps.keep_awake_command() == ["caffeinate", "-i", "-m", "-s"])
    check("Mac: notifications through osascript", ps.notify_command("T", "M")[0] == "osascript")
    check("Mac: the MFA container needs no user mapping", ps.docker_user_args() == [])
    check("Mac: the log stays in ~/Library/Logs", ps.ghost_log_path() == os.path.expanduser(
        "~/Library/Logs/librasync-ghost.out"))
    check("Mac: Homebrew on the service's PATH", "/opt/homebrew/bin" in ps.tool_path_prefix())

with on("linux"), mock.patch.object(ps.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}"):
    cmd = ps.keep_awake_command()
    check("Linux: systemd-inhibit holds off sleep and idle",
          cmd[0] == "systemd-inhibit" and "--what=sleep:idle" in cmd and cmd[-2:] == ["sleep", "infinity"], f"{cmd}")
    check("Linux: notifications through notify-send", ps.notify_command("T", "M") == ["notify-send", "T", "M"])
    args = ps.docker_user_args()
    check("Linux: MFA runs as this user, with a writable home and MFA root",
          args[:2] == ["--user", f"{os.getuid()}:{os.getgid()}"] and "HOME=/tmp" in args
          and "MFA_ROOT_DIR=/tmp/mfa" in args, f"{args}")
    with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/x/state"}):
        check("Linux: the log follows XDG_STATE_HOME", ps.ghost_log_path() == "/x/state/voxie/librasync-ghost.out")
    check("Linux: install hints name apt, not brew", "apt" in ps.install_hint("ffmpeg")
          and "brew" not in ps.install_hint("kepubify"))

with on("linux"), mock.patch.object(ps.shutil, "which", return_value=None):
    check("Linux without systemd-inhibit or notify-send: the run goes on regardless",
          ps.keep_awake_command() is None and ps.notify_command("T", "M") is None)
    ps.notify("T", "M")      # must not raise

# --- the systemd unit ---------------------------------------------------------
import configparser  # noqa: E402
with on("linux"):
    unit = ps.systemd_unit("/venv/bin/python", "/repo/mac_ghost.py", "/repo", "/s/voxie/librasync-ghost.out")
cp = configparser.ConfigParser(strict=False, interpolation=None)
cp.optionxform = str
cp.read_string(unit)
svc = cp["Service"]
check("the unit starts the watcher from the repo, unbuffered",
      svc["WorkingDirectory"] == "/repo" and svc["ExecStart"] == "/venv/bin/python -u /repo/mac_ghost.py")
check("it restarts the watcher if it dies", svc["Restart"] == "always")
check("it logs to files a user can tail", svc["StandardOutput"] == "append:/s/voxie/librasync-ghost.out"
      and svc["StandardError"] == "append:/s/voxie/librasync-ghost.err")
check("it starts at login", cp["Install"]["WantedBy"] == "default.target")

# --- the engine's MFA call ----------------------------------------------------
import librerian_engine_v3 as engine  # noqa: E402
for system, expect_user in (("mac", False), ("linux", True)):
    with on(system), mock.patch.object(engine.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        engine.run_mfa_align("/w", ".", "output")
        cmd = run.call_args[0][0]
        check(f"{system}: the MFA container {'runs' if expect_user else 'does not run'} as this user",
              ("--user" in cmd) == expect_user and cmd.index(engine.MFA_IMAGE) > (cmd.index("--user") if expect_user else 0))

# --- the CLI ------------------------------------------------------------------
from click.testing import CliRunner  # noqa: E402
import libra_cli.main as cli_main  # noqa: E402

with mock.patch.object(cli_main, "IS_MAC", False):
    res = CliRunner().invoke(cli_main.cli, ["setup-kobo", "--json"])
out = [l for l in res.output.splitlines() if l.startswith("{")]
check("Linux: the Kobo installer refuses clearly, before touching anything",
      out and json.loads(out[-1]) == {"success": False, "error": "installer_macos_only"}, res.output[-200:])

cfg = tempfile.mkdtemp()
state = tempfile.mkdtemp()
calls = []
with on("linux"), mock.patch.object(cli_main, "IS_LINUX", True), \
     mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg, "XDG_STATE_HOME": state}), \
     mock.patch("shutil.which", return_value="/usr/bin/systemctl"), \
     mock.patch.object(cli_main.subprocess, "run",
                       side_effect=lambda a, **k: calls.append(a) or mock.Mock(returncode=0, stderr="")):
    res = CliRunner().invoke(cli_main.cli, ["start-ghost"])
unit_path = os.path.join(cfg, "systemd", "user", ps.SYSTEMD_UNIT)
check("Linux: start-ghost writes a systemd user unit", os.path.exists(unit_path), res.output[-300:])
check("and enables and starts it, as the user, never as root",
      ["systemctl", "--user", "enable", ps.SYSTEMD_UNIT] in calls
      and ["systemctl", "--user", "restart", ps.SYSTEMD_UNIT] in calls
      and not any("launchctl" in c for c in calls), f"{calls}")
check("and never writes a LaunchAgent", not any("LaunchAgents" in str(c) for c in calls))

ghost = open(os.path.join(ROOT, "mac_ghost.py"), encoding="utf-8").read()
check("the watcher asks the platform module, with no Mac-only command left inline",
      not any(c in ghost for c in ('["caffeinate"', '["osascript"', '"/opt/homebrew/bin:'))
      and "keep_awake = platform_support.keep_awake" in ghost)

# --- staying awake for a whole book, and saying so on battery --------------------
# 2026-10-08: the engine, run by itself, had nothing holding off sleep, and a
# laptop at 1% slept for four hours while three pods billed.
started, stopped = [], []


class Inhibitor:
    def __init__(self, cmd):
        started.append(cmd)

    def terminate(self):
        stopped.append(True)

    def wait(self, timeout=None):
        return 0


with on("mac"), mock.patch.object(ps.subprocess, "Popen", Inhibitor):
    with ps.keep_awake():
        check("keep_awake holds sleep off while the book is made", started == [["caffeinate", "-i", "-m", "-s"]]
              and not stopped)
    check("and lets go when it is done", stopped == [True])
    stopped.clear()
    try:
        with ps.keep_awake():
            raise RuntimeError("the run failed")
    except RuntimeError:
        pass
    check("and lets go when the run fails, too", stopped == [True])
class Stubborn(Inhibitor):
    killed = []

    def wait(self, timeout=None):
        raise ps.subprocess.TimeoutExpired("caffeinate", timeout)

    def kill(self):
        Stubborn.killed.append(True)


with on("mac"), mock.patch.object(ps.subprocess, "Popen", Stubborn):
    with ps.keep_awake():
        pass
    check("an inhibitor that will not stop is killed, not waited on for ever", Stubborn.killed == [True])
with on("mac"), mock.patch.object(ps.subprocess, "Popen", side_effect=FileNotFoundError("caffeinate")):
    with ps.keep_awake():
        check("no inhibitor at all: the run goes on", True)

batt = lambda text: (lambda *a, **k: mock.Mock(stdout=text))  # noqa: E731
with on("mac"):
    check("Mac on battery is seen", ps.on_battery(run=batt("Now drawing from 'Battery Power'\n -InternalBattery-0 71%")) is True)
    check("Mac on mains is seen", ps.on_battery(run=batt("Now drawing from 'AC Power'\n")) is False)
    check("a Mac that cannot say is neither", ps.on_battery(run=batt("")) is None)

    def no_pmset(*a, **k):
        raise OSError("pmset")
    check("a Mac where pmset fails is neither", ps.on_battery(run=no_pmset) is None)
with on("other"):
    check("neither a Mac nor Linux: it cannot tell", ps.on_battery() is None)


def power(*supplies):
    d = tempfile.mkdtemp()
    for name, kind, online in supplies:
        os.makedirs(os.path.join(d, name))
        open(os.path.join(d, name, "type"), "w").write(kind)
        open(os.path.join(d, name, "online"), "w").write(online)
    return d


with on("linux"):
    check("Linux laptop unplugged is on battery",
          ps.on_battery(power_dir=power(("BAT0", "Battery", ""), ("AC", "Mains", "0"))) is True)
    check("Linux laptop plugged in is not",
          ps.on_battery(power_dir=power(("BAT0", "Battery", ""), ("AC", "Mains", "1"))) is False)
    check("a server with no battery is neither", ps.on_battery(power_dir=power(("AC", "Mains", "1"))) is None)
    odd = power(("BAT0", "Battery", ""), ("AC", "Mains", "1"))
    os.remove(os.path.join(odd, "AC", "online"))
    check("a supply that cannot be read is skipped, not trusted",
          ps.on_battery(power_dir=odd) is True)

engine = open(os.path.join(ROOT, "librerian_engine_v3.py"), encoding="utf-8").read()
check("the engine holds sleep off for the whole run, not only under the watcher",
      "with platform_support.keep_awake():\n        asyncio.run(main(" in engine)
check("and says so before renting pods on battery",
      engine.index("platform_support.on_battery()") < engine.index("acquisition = asyncio.create_task(acquire_pool("))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
