#!/usr/bin/env python3
"""Refuse a tree that would publish something private. Exit 1 on any finding.

    python tools/leak_scan.py <folder>

Used by tools/export_public.sh on the public export. Python rather than
`grep -P`: macOS's BSD grep has no -P, and an erroring grep inside `|| true`
reported every export clean.

Looks for home directories, API and private keys, the files that must never
leave this machine, and values only this machine knows: the live gateway
address (~/.librasync.json), the private RunPod template id (the environment
or the repo's .env, which the export does not load), and the private voices
(private_terms): no export may name them, their speakers or their videos. An
audio sample is published only when its voice consented (sample_findings).
"""
import json
import os
import re
import sys

NEVER_EXPORTED = (".env", "training_lab", "voices", "library")
# Files that name the patterns they look for.
SELF = {"tools/leak_scan.py"}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# One private word per line (gitignored): what no file can derive, such as the
# names of the people whose voices the maintainer cloned for private tests.
DENY_FILE = ".export-deny"


def private_terms(repo=REPO):
    """Words that identify this machine's private voices: each voice's name
    (voices/<name>.wav), the id of the video it was cut from (its sidecar's
    source), and every line of .export-deny. Read here, never written into
    the code, so the list itself stays private."""
    terms = set()
    vdir = os.path.join(repo, "voices")
    for f in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
        stem, ext = os.path.splitext(f)
        if ext == ".wav":
            terms.add(stem)
        elif ext == ".json":
            try:
                source = str(json.load(open(os.path.join(vdir, f))).get("source") or "")
            except (OSError, ValueError):
                continue
            terms.update(re.findall(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", source))
    try:
        terms.update(t.strip() for t in open(os.path.join(repo, DENY_FILE))
                     if t.strip() and not t.startswith("#"))
    except OSError:
        pass
    return sorted(terms)


def template_ids(repo=REPO):
    """The RunPod template ids this machine uses: the environment's, and .env's."""
    values = [os.environ.get("RUNPOD_TEMPLATE_ID") or ""]
    try:
        values += [line.split("=", 1)[1] for line in open(os.path.join(repo, ".env"))
                   if line.startswith("RUNPOD_TEMPLATE_ID=")]
    except OSError:
        pass
    # A value can hold more than one ("<public>#<private>"); each id is a word.
    return {t for v in values for t in re.findall(r"[a-z0-9]{8,}", v)}


def checks(repo=REPO):
    found = [
        ("home directory", re.compile(r"/Users/[a-z]")),
        ("API key", re.compile(r"gsk_[A-Za-z0-9]{20,}|sk-or-v1-[a-f0-9]{20,}|rpa_[A-Z0-9]{20,}"
                               r"|ghp_[A-Za-z0-9]{20,}")),
        ("private key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ]
    try:
        ip = json.load(open(os.path.expanduser("~/.librasync.json"))).get("GATEWAY_IP")
    except (OSError, ValueError):
        ip = None
    # The public template's id is meant to be in the code (libra_cli/main.py);
    # any other template id in .env is a private one.
    main = open(os.path.join(REPO, "libra_cli", "main.py")).read()
    public = re.search(r'^RUNPOD_PUBLIC_TEMPLATE_ID = "([^"]*)"', main, re.M)
    public = public.group(1) if public else ""
    if ip:
        found.append(("live gateway address", re.compile(re.escape(ip))))
    for tid in sorted(template_ids(repo) - {public}):
        found.append(("RunPod template id", re.compile(re.escape(tid))))
    for term in private_terms(repo):
        found.append((f"private voice ({term[:2]}...)", re.compile(r"\b" + re.escape(term) + r"\b", re.I)))
    return found


def scan(root, repo=REPO):
    findings = []
    rules = checks(repo)
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules")]
        for f in files:
            path = os.path.join(d, f)
            rel = os.path.relpath(path, root)
            if rel in SELF or f.startswith("LICENSE"):
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            for label, rx in rules:
                for m in rx.finditer(text):
                    findings.append(f"{label}: {rel}:{text.count(chr(10), 0, m.start()) + 1}")
    findings += [f"{f} is in the export" for f in NEVER_EXPORTED if os.path.exists(os.path.join(root, f))]
    return findings + sample_findings(root)


SAMPLES = "assets/samples"
CONSENTED = ("preset", "self", "permission")


def sample_findings(root):
    """Every audio sample is listed in assets/samples/SAMPLES.json, made with a
    voice that may be published: a preset voice (Kokoro's) or a clone whose
    speaker consented. A clone without that record stays private; it may sit
    in the README as a placeholder, but no export carries it."""
    folder = os.path.join(root, SAMPLES)
    audio = sorted(f for f in os.listdir(folder) if f.lower().endswith((".mp3", ".wav", ".m4a", ".ogg"))) \
        if os.path.isdir(folder) else []
    if not audio:
        return []
    try:
        listed = json.load(open(os.path.join(folder, "SAMPLES.json")))
    except (OSError, ValueError):
        listed = {}
    found = []
    for f in audio:
        consent = (listed.get(f) or {}).get("voice_consent")
        if f not in listed:
            found.append(f"sample not listed in {SAMPLES}/SAMPLES.json: {SAMPLES}/{f}")
        elif consent not in CONSENTED:
            found.append(f"sample made with a voice without consent ({consent}): {SAMPLES}/{f}")
    return found


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    problems = scan(sys.argv[1])
    for p in problems:
        print(f"LEAK ({p})")
    sys.exit(1 if problems else 0)
