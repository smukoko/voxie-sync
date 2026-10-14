"""What differs between macOS and Linux, in one place.

The narration pipeline (engine, watcher) runs on both. The Kobo installer is
macOS-only for now: it extracts with Apple's ditto because the Mac's own FAT
driver gives two files the same 8.3 short name (docs/LESSONS.md), and a Linux
port has to be measured with the FAT gate on a real device first.

Everything here degrades instead of failing: a machine without a sleep
inhibitor or a notifier still narrates, it just may sleep or stay quiet.
"""
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def keep_awake_command():
    """The command that holds off sleep while it runs, or None.

    macOS: caffeinate (-i idle, -m disk, -s system sleep on AC power).
    Linux: systemd-inhibit around a process that sleeps forever; the caller
    terminates it when the book is done, which releases the lock.
    """
    if IS_MAC:
        return ["caffeinate", "-i", "-m", "-s"]
    if IS_LINUX and shutil.which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=sleep:idle", "--who=Voxie",
                "--why=Narrating a book", "--mode=block", "sleep", "infinity"]
    return None


@contextmanager
def keep_awake():
    """Hold off idle sleep for the length of one book.

    launchd will happily start the watcher and then let the Mac sleep out from
    under it: a run is 15-40 minutes of GPU work and upload with no keyboard
    activity, which is exactly what the idle timer is measuring. Sleeping
    mid-run does not pause anything -- the RunPod pods go on billing while
    nothing consumes them, and the upload connection dies -- so the assertion
    covers the whole job rather than the process lifetime, which would keep
    the machine awake forever. The engine holds it too, since it is also run
    by itself.

    It cannot stop a battery running out: on 2026-10-08 a Mac at 1% slept for
    four hours mid-book (the pods now end themselves: server/idle_watchdog.py).
    If no inhibitor exists the run still goes ahead.
    """
    proc = None
    cmd = keep_awake_command()
    try:
        proc = subprocess.Popen(cmd) if cmd else None
    except (FileNotFoundError, OSError) as e:
        print(f"   ⚠️  Could not hold off sleep ({e}); the run may be cut short if the machine idles.")
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def on_battery(run=subprocess.run, power_dir="/sys/class/power_supply"):
    """True on battery, False on mains, None when it cannot tell (a desktop, a server)."""
    if IS_MAC:
        try:
            out = run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if "'Battery Power'" in out:
            return True
        return False if "'AC Power'" in out else None
    if IS_LINUX and os.path.isdir(power_dir):
        kinds = {}
        for name in os.listdir(power_dir):
            try:
                kind = open(os.path.join(power_dir, name, "type")).read().strip()
                online = open(os.path.join(power_dir, name, "online")).read().strip() == "1" \
                    if kind == "Mains" else None
            except OSError:
                continue
            kinds.setdefault(kind, []).append(online)
        if "Battery" not in kinds:
            return None
        return not any(kinds.get("Mains", []))
    return None


def notify_command(title, message):
    """The command that shows a desktop notification, or None."""
    if IS_MAC:
        import json
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        return ["osascript", "-e", script]
    if IS_LINUX and shutil.which("notify-send"):
        return ["notify-send", title, message]
    return None


def notify(title, message):
    """Best effort: a background watcher's log is read by nobody, so anything
    the user must act on is also shown on screen where the desktop allows."""
    cmd = notify_command(title, message)
    if not cmd:
        return
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def tool_path_prefix():
    """Directories a background service needs on PATH to find ffmpeg, kepubify, docker.

    launchd starts agents with a minimal PATH that has no Homebrew; a systemd
    user unit has the system PATH but not ~/.local/bin, where pip --user and
    hand-installed binaries such as kepubify go.
    """
    if IS_MAC:
        return ["/opt/homebrew/bin", "/usr/local/bin"]
    return [os.path.expanduser("~/.local/bin"), "/usr/local/bin"]


def log_dir():
    """Where the watcher's log lives. Never /tmp, which macOS sweeps after three days."""
    if IS_MAC:
        return os.path.expanduser("~/Library/Logs")
    state = os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "voxie")


def ghost_log_path():
    return os.path.join(log_dir(), "librasync-ghost.out")


def install_hint(tool):
    """How to install a missing command-line tool on this system."""
    if IS_MAC:
        return f"brew install {tool}"
    if tool == "kepubify":
        return ("download kepubify from https://github.com/pgaskin/kepubify/releases "
                "into ~/.local/bin and make it executable")
    if tool == "yt-dlp":
        return "pip install -U yt-dlp"
    return f"sudo apt install {tool}   (or your distribution's package manager)"


def docker_user_args():
    """Run a container as this user, so files it writes stay ours.

    Docker Desktop on a Mac maps ownership to the user already. On Linux a
    container writes as root, and the engine could then not clear its own
    workspace before the next book. HOME and MFA's root go to the container's
    /tmp (writable by any user, discarded with --rm); measured 2026-10-03:
    MFA aligns as uid 1000 with exactly this.
    """
    if not IS_LINUX:
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-e", "MFA_ROOT_DIR=/tmp/mfa"]


SYSTEMD_UNIT = "voxie-ghost.service"


def systemd_unit(python_path, ghost_path, repo_root, log_path):
    """A systemd user unit that runs the watcher, the Linux twin of the LaunchAgent."""
    err_path = log_path[:-len(".out")] + ".err" if log_path.endswith(".out") else log_path + ".err"
    path = ":".join(tool_path_prefix() + ["/usr/bin", "/bin"])
    return f"""[Unit]
Description=Voxie watcher: narrates the books dropped in ~/Kobo_Dropzone
After=network-online.target

[Service]
WorkingDirectory={repo_root}
ExecStart={python_path} -u {ghost_path}
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10
StandardOutput=append:{log_path}
StandardError=append:{err_path}

[Install]
WantedBy=default.target
"""
