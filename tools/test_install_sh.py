"""install.sh survives a brew that reads stdin, and upgrades nothing.

The install line pipes the script into bash, so every command it starts shares
the pipe the script is being read from. On the first from-scratch test
(2026-10-16) brew's children read that pipe and ate the script's next lines:
the install stopped after brew, `~/voxie` was never cloned and nothing said
why. The same `brew install` also upgraded five tools the user already had and
about fifteen things that depend on them (ffmpeg, mpv, openssl ...).

This runs the real script, piped into bash, on a fake Homebrew whose `install`
reads whatever stdin it is given. Old script: it stops after `brew install`.
"""
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMULAE = {"python@3.12", "ffmpeg", "espeak-ng", "kepubify", "git"}


def script(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def run_install(installed, install_sh):
    """Run install.sh as the install line does. Returns (output, brew log)."""
    with tempfile.TemporaryDirectory() as tmp:
        fake, prefix, home = (os.path.join(tmp, d) for d in ("fakebin", "prefix", "home"))
        for d in (fake, os.path.join(prefix, "bin"), home):
            os.makedirs(d)
        log = os.path.join(tmp, "brew.log")
        open(log, "w").close()

        # The script takes the Darwin branch whatever machine runs the test.
        script(os.path.join(fake, "uname"), 'echo Darwin\n')
        script(os.path.join(fake, "docker"), "exit 0\n")
        script(os.path.join(fake, "brew"), f"""
echo "$@" >> {log}
case "$1" in
  --prefix) echo {prefix} ;;
  list) case " {' '.join(sorted(installed))} " in *" $3 "*) exit 0 ;; *) exit 1 ;; esac ;;
  install) cat >/dev/null ;;  # what brew's children did to the script
esac
""")
        # A clone is the files the script then reads.
        script(os.path.join(fake, "git"), """
[ "$1" = clone ] || exit 0
mkdir -p "$3" && : > "$3/requirements.txt" && : > "$3/.env.example"
""")
        script(os.path.join(prefix, "bin", "python3.12"), """
mkdir -p "$3/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/pip"; cp "$3/bin/pip" "$3/bin/voxie"
chmod +x "$3/bin/pip" "$3/bin/voxie"
""")

        env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": home, "VOXIE_NO_SETUP": "1",
               "VOXIE_REPO": "fake-repo"}
        with open(install_sh, "rb") as f:
            piped = f.read()
        r = subprocess.run(["/bin/bash"], input=piped, env=env, capture_output=True,
                           timeout=60)
        out = (r.stdout + r.stderr).decode()
        installed_cmd = os.path.islink(os.path.join(prefix, "bin", "voxie"))
        with open(log) as f:
            calls = f.read().splitlines()
        return out, calls, installed_cmd


def main():
    sh = os.path.join(ROOT, "install.sh")

    # Nothing installed: brew installs all five, and the script still reaches its end.
    out, calls, linked = run_install(set(), sh)
    installs = [c for c in calls if c.startswith("install ") and "--cask" not in c]
    assert len(installs) == 1 and set(installs[0].split()[1:]) == FORMULAE, \
        f"brew should install the five tools once, got {installs}"
    assert "Voxie is installed" in out and linked, \
        "the script stopped after brew (brew read the script's own stdin?):\n" + out[-600:]

    # Everything installed (the user's own, maybe outdated): brew install is not called at all.
    out, calls, linked = run_install(FORMULAE, sh)
    assert not [c for c in calls if c.startswith("install")], \
        f"brew install ran for tools that are already installed (it upgrades them): {calls}"
    assert "Voxie is installed" in out and linked, out[-600:]

    # One missing: only that one is installed.
    out, calls, linked = run_install(FORMULAE - {"kepubify"}, sh)
    assert [c for c in calls if c.startswith("install")] == ["install kepubify"], calls
    assert "Voxie is installed" in out and linked, out[-600:]

    print("✅ install.sh: reaches its end past a brew that reads stdin; installs only what is missing")


if __name__ == "__main__":
    sys.exit(main())
