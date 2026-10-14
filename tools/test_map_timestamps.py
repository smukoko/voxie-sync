"""core.tts_worker.map_timestamps against how MFA really tokenises.

The case that drifted on 261 of Redeeming Love's 616 chunks: MFA splits
"e-mails" into two entries, and a 1:1 pairing then gave every later word the
next word's time.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tts_worker import map_timestamps  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append(bool(ok)); print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))

text = "letters and e-mails to Francine — poured in."
mfa = [[0.0, 0.4, "letters"], [0.4, 0.6, "and"], [0.6, 0.8, "e"], [0.8, 1.2, "mails"],
       [1.2, 1.3, "to"], [1.3, 1.9, "francine"], [2.0, 2.4, "poured"], [2.4, 2.6, "in"]]
out = map_timestamps(text, mfa)
by = {w["text"]: w for w in out}
check("one entry per whitespace word", len(out) == len(text.split()))
check("a split word spans both of its MFA entries", by["e-mails"]["start"] == 0.6 and by["e-mails"]["end"] == 1.2)
check("the word after it keeps its own time (no drift)", by["to"]["start"] == 1.2 and by["poured"]["start"] == 2.0)
check("punctuation-only tokens sit where the previous word ended", by["—"]["start"] == by["Francine"]["end"])

oov = map_timestamps("the Zyzzogeton sang", [[0, .2, "the"], [.2, .9, "<unk>"], [.9, 1.3, "sang"]])
check("an out-of-dictionary word MFA wrote as <unk> is paired by position", oov[1]["start"] == 0.2 and oov[2]["start"] == 0.9)

short = map_timestamps("one two three", [[0, .3, "one"]])
check("MFA returning fewer words never raises or shortens the list", len(short) == 3)

print("=" * 60)
print(f"{len(results)} checks: {sum(results)} passed, {len(results) - sum(results)} FAILED")
sys.exit(0 if all(results) else 1)
