"""Set up the Voxie server over SSH, from the Mac or Linux machine running init.

The person creates a server (a Hetzner VPS, or any Debian/Ubuntu machine they
can SSH into with a key) and `voxie init` does the rest: it copies this
checkout's code to /opt/voxie and runs server/install_server.py there. Nothing
is typed on the server, and the secrets travel inside the SSH connection on
stdin, never in a command line.
"""
import json
import os
import socket
import subprocess

SERVER_DIR = "/opt/voxie"


def ssh_command(target, *remote):
    # BatchMode: fail instead of asking for a password nobody is there to type.
    # accept-new: a fresh server's host key is unknown the first time, and a
    # later change (a reinstalled server) still stops the connection.
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15", target, *remote]


def _sudo(target):
    """Commands run as root; a non-root login needs passwordless sudo."""
    return [] if target.split("@")[0] == "root" else ["sudo", "-n"]


def check_ssh(target, run=subprocess.run):
    """(ok, detail): can we log in with a key, and does the server have python3?"""
    r = run(ssh_command(target, "command -v python3 && uname -s"), capture_output=True, text=True)
    if r.returncode == 255:
        return False, (r.stderr.strip().splitlines() or ["ssh failed"])[-1]
    if r.returncode != 0 or "Linux" not in r.stdout:
        return False, "the server needs Linux with python3 (Ubuntu or Debian)"
    return True, "logged in"


def foreign_stack(target, run=subprocess.run):
    """The folder of a Voxie stack that is NOT /opt/voxie, if one runs there.

    A server set up by hand (an older runbook used another folder) already has
    containers with our names; a second stack would fight it for the ports and
    the container names. Better to stop and say so.
    """
    fmt = '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
    r = run(ssh_command(target, *_sudo(target), "docker", "inspect", "--format", f"'{fmt}'",
                        "librarian_server"), capture_output=True, text=True)
    folder = r.stdout.strip()
    return folder if r.returncode == 0 and folder and folder != SERVER_DIR else None


def stack_dir(target, run=subprocess.run):
    """The folder the running Voxie gateway was started from, or None."""
    fmt = '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
    r = run(ssh_command(target, *_sudo(target), "docker", "inspect", "--format", f"'{fmt}'",
                        "librarian_server"), capture_output=True, text=True)
    folder = r.stdout.strip()
    return folder if r.returncode == 0 and folder.startswith("/") else None


def _env_value(text, key):
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].split(" #", 1)[0].strip().strip("'\"")
    return ""


def link_existing(target, username, userkey, run=subprocess.run):
    """Join this computer to a Voxie server that already runs: nothing typed on it.

    Reads the server's own UPLOAD_TOKEN and ABS_API_TOKEN from its .env (no new
    secret, no restart), and creates the account in its gateway, or sets that
    account's password when it already exists. The setup page used to print
    both steps for the person to run on the server themselves (user,
    2026-10-08: "users don't have to run commands on their own").
    Returns {"upload_token", "abs_token"}; raises RuntimeError with a sentence.
    """
    folder = stack_dir(target, run)
    if not folder:
        raise RuntimeError("No Voxie server (a container called librarian_server) runs there.")
    r = run(ssh_command(target, *_sudo(target), "cat", f"{folder}/.env"), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Could not read {folder}/.env on the server.")
    upload_token, abs_token = _env_value(r.stdout, "UPLOAD_TOKEN"), _env_value(r.stdout, "ABS_API_TOKEN")
    if not upload_token:
        raise RuntimeError(f"The server's {folder}/.env has no UPLOAD_TOKEN: it does not accept uploads.")
    admin = ["docker", "exec", "librarian_server", "python", "tools/gateway_admin.py"]
    r = run(ssh_command(target, *_sudo(target), *admin, "add-user", username, "--userkey", userkey),
            capture_output=True, text=True)
    if r.returncode != 0 and "already exists" in (r.stdout + r.stderr):
        r = run(ssh_command(target, *_sudo(target), *admin, "set-password", username, "--userkey", userkey),
                capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Could not create the account on the server: {(r.stderr or r.stdout).strip()[-200:]}")
    return {"upload_token": upload_token, "abs_token": abs_token}


def push_code(repo_root, target, run=subprocess.run):
    """Copy the committed code (git archive) to SERVER_DIR; .env and data stay."""
    archive = subprocess.Popen(["git", "-C", repo_root, "archive", "--format=tar", "HEAD"],
                               stdout=subprocess.PIPE)
    r = run(ssh_command(target, *_sudo(target), "sh", "-c",
                        f"'mkdir -p {SERVER_DIR} && tar -x -C {SERVER_DIR}'"),
            stdin=archive.stdout, capture_output=True, text=True)
    archive.stdout.close()
    if archive.wait() != 0:
        raise RuntimeError("git archive failed: is this a git checkout of Voxie?")
    if r.returncode != 0:
        raise RuntimeError(f"copying the code failed: {r.stderr.strip()}")


def install(target, settings, run=subprocess.run, on_line=None):
    """Run the server's installer and return the ABS key.

    Its progress shows live: in the terminal, or line by line through
    `on_line` (the setup page).
    """
    cmd = ssh_command(target, *_sudo(target), "python3", f"{SERVER_DIR}/server/install_server.py")
    if on_line is None:
        r = run(cmd, input=json.dumps(settings), stdout=subprocess.PIPE, text=True)
        returncode, stdout = r.returncode, r.stdout
    else:
        returncode, stdout = _stream(cmd, json.dumps(settings), on_line)
    if returncode != 0:
        raise RuntimeError("the server setup stopped (see the details)")
    for line in reversed(stdout.splitlines()):
        if line.startswith("VOXIE_ABS_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("the server finished without giving Audiobookshelf's key")


def _stream(cmd, stdin_text, on_line):
    """(returncode, stdout): stderr goes to on_line as it arrives."""
    import threading
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    out = []
    reader = threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)
    reader.start()
    proc.stdin.write(stdin_text)
    proc.stdin.close()
    for line in proc.stderr:
        on_line(line.rstrip("\n"))
    proc.wait()
    reader.join()
    return proc.returncode, "".join(out)


def resolves_to(domain, ip):
    """True when `domain` already points at `ip` (an A record at the registrar)."""
    try:
        found = {a[4][0] for a in socket.getaddrinfo(domain, 443, socket.AF_INET)}
    except socket.gaierror:
        return False
    return ip in found


SSH_DIR = os.path.expanduser("~/.ssh")


def public_key():
    """The text of this computer's SSH public key, or None if it has none."""
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        path = os.path.join(SSH_DIR, name)
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    return None


def create_key():
    """Make ~/.ssh/id_ed25519 (no passphrase: init and the updates log in
    unattended) and return its public half."""
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    key = os.path.join(SSH_DIR, "id_ed25519")
    if not os.path.exists(key):
        r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "voxie", "-f", key],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {r.stderr.strip()}")
    return public_key()


def explain_ssh_error(detail, host):
    """What a person can do about an SSH failure, in their words."""
    d = detail.lower()
    if "permission denied" in d:
        return ("The server did not accept this computer's SSH key. Add the key shown above "
                "to the server (at Hetzner: when you create it, or rebuild it with the key).")
    if "could not resolve" in d or "name or service not known" in d:
        return f"{host} does not exist. Check the address."
    if "timed out" in d or "no route" in d or "connection refused" in d or "unreachable" in d:
        return f"No answer from {host}. Check the address, and that the server is running."
    if "host key verification failed" in d or "remote host identification has changed" in d:
        return (f"{host} has a new identity (was it rebuilt?). Remove the old one with "
                f"ssh-keygen -R {host}, then try again.")
    return detail
