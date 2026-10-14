"""The public export refuses a tree with anything private in it.

The first version ran `grep -P`, which macOS's grep does not have; inside
`|| true` it reported every export clean. Offline: plants each kind of leak.
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["RUNPOD_TEMPLATE_ID"] = "tmpl1234zz"
import leak_scan  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def tree(files):
    root = tempfile.mkdtemp()
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)
    return root


check("a clean tree passes", leak_scan.scan(tree({"README.md": "hello", "core/x.py": "x = 1"})) == [])
for label, files in [
    ("a home directory", {"a.py": 'p = "/Us' + 'ers/alice/x"'}),
    ("a Groq key", {"a.py": "k = 'gsk_" + "A" * 30 + "'"}),
    ("an OpenRouter key", {"a.md": "sk-or-v1-" + "a" * 40}),
    ("a private key", {"id": "-----BEGIN OPENSSH PRIV" + "ATE KEY-----"}),
    ("the RunPod template id", {"docs/x.md": "template tmpl1234zz"}),
    ("a .env file", {".env": "X=1"}),
    ("the voices folder", {"voices/me.json": "{}"}),
]:
    found = leak_scan.scan(tree(files))
    check(f"{label} is caught", len(found) == 1, f"({found})")
check("the licence text is not scanned", leak_scan.scan(tree({"LICENSE": "/Us" + "ers/x"})) == [])

# --- What only this machine knows: its private voices and its .env ------------
# A repository like the maintainer's: two cloned voices, a deny list, an .env.
repo = tree({"voices/speaker_one.wav": "", "voices/speaker_one.json":
             '{"source": "https://www.youtube.com/watch?v=AbCdEfGhIjK @ 00:35:43"}',
             "voices/other_voice.json": '{"source": "https://youtu.be/ZyXwVuTsRqP?t=9"}',
             ".export-deny": "# the people\nFamousname\n",
             ".env": "RUNPOD_API_KEY=x\nRUNPOD_TEMPLATE_ID=y1s4i5a5qa#priv8tmpl9\n"})
saved = os.environ.pop("RUNPOD_TEMPLATE_ID")
for label, text in [("a private voice's name", "voices/speaker_one.wav was cut from"),
                    ("its video", "see watch?v=AbCdEfGhIjK"), ("another voice's video", "youtu.be/ZyXwVuTsRqP"),
                    ("a word from .export-deny, any case", "FAMOUSNAME's clip ended on a sigh"),
                    ("a template id only .env holds (the export does not load it)", "# was y1s4i5a5qa#priv8tmpl9")]:
    found = leak_scan.scan(tree({"core/x.py": text}), repo=repo)
    check(f"{label} is caught", len(found) >= 1, f"({found})")
check("and not printed whole in the report", not any("AbCdEfGhIjK" in f or "Famousname" in f for f in
                                                    leak_scan.scan(tree({"a": "AbCdEfGhIjK Famousname"}), repo=repo)))
public = leak_scan.re.search(r'RUNPOD_PUBLIC_TEMPLATE_ID = "([^"]*)"',
                             open(os.path.join(leak_scan.REPO, "libra_cli", "main.py")).read()).group(1)
check("the public template id is allowed", leak_scan.scan(tree({"a.py": f"T = '{public}'"}), repo=repo) == [])
check("a longer word that holds a name is not a leak",
      leak_scan.scan(tree({"a.md": "speaker_ones and famousnames"}), repo=repo) == [])
check("a machine with no voices, list or .env has no private words",
      leak_scan.private_terms(tree({"README.md": ""})) == [])
os.environ["RUNPOD_TEMPLATE_ID"] = saved

# --- Audio samples: published only in a voice that may be ----------------------
listing = '{"free.mp3": {"voice_consent": "preset"}, "mine.mp3": {"voice_consent": "self"}, ' \
          '"placeholder.mp3": {"voice_consent": null}}'
ok = tree({"assets/samples/free.mp3": "", "assets/samples/mine.mp3": "",
           "assets/samples/SAMPLES.json": listing.replace(', "placeholder.mp3": {"voice_consent": null}', "")})
check("samples in a preset voice or the maintainer's own pass", leak_scan.scan(ok, repo=ok) == [],
      f"({leak_scan.scan(ok, repo=ok)})")
bad = tree({"assets/samples/free.mp3": "", "assets/samples/placeholder.mp3": "", "assets/samples/SAMPLES.json": listing})
found = leak_scan.scan(bad, repo=bad)
check("a sample in a voice without consent stops the export",
      len(found) == 1 and "placeholder.mp3" in found[0] and "without consent" in found[0], f"({found})")
found = leak_scan.scan(tree({"assets/samples/stray.mp3": ""}), repo=repo)
check("and so does a sample nobody listed", len(found) == 1 and "not listed" in found[0], f"({found})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
