"""A chunk that does not say its text is found by hearing it, and narrated again.

2026-10-22, Winning: `"Job not finished."` was narrated "job not fail". The
pod's aligner put "finished" in 0.2 s at the very end of the file and said
nothing, so the engine's only net (chunks MFA refuses, 11 of 858) let it by.
The engine now hears each chunk with Whisper as it lands (core/narration_check):
a run of words heard or skipped, or in a short line one word that is not the one
written, is refused as if the aligner had refused it. Offline: a fake Whisper.
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

from core import narration_check as nc  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


# --- what counts --------------------------------------------------------------------
check("the Winning slip: 'finished' heard as 'fail' in a three-word line",
      nc.slip("job not fail", 'Job not finished.', 3.2, None) == "short line, not said: finished")
check("a line said right is not a slip", nc.slip("job not finished", "Job not finished.", 3.2, None) == "")
check("Whisper dropping or mishearing a short word is not the narrator's",
      nc.slip("winning is everything", "One. Winning is everything.", 3.2, None) == ""
      and nc.slip("oh conversation ended like this", "Our conversation ended like this.", 3.2, None) == ""
      and nc.slip("this is going to hurt", "This is going to hurt.", 3.2, None) == "")
check("a word said slightly differently is the same word",
      nc.slip("colour was grey", "The colour was grey.", 3.0, None) == ""
      and nc.slip("the harbour was quiet", "The harbor was quiet.", 3.0, None) == "")
long_want = " ".join(["the harbour lay grey and still under a low sky"] * 6)
check("one mangled word in a long chunk is not worth a retake",
      nc.slip(long_want.replace("harbour", "hobber", 1), long_want, 40.0, None) == "")
check("a run of words the narrator did not say is, in a long chunk",
      nc.slip(long_want.replace("grey and still under", "dole est ain't da all", 1), long_want, 40.0, None) != "")
check("so is a skip",
      nc.slip(long_want.replace("lay grey and still under", "", 1), long_want, 40.0, None) != "")
check("a 9-word line is judged as a long one: one slip alone passes",
      nc.slip("the harbour lay grey and still under a lowe sky", "The harbour lay grey and still under a low sky.", 5, None) == "")
check("a heading's digits are said as words", nc.words("Chapter 12") == ["chapter", "twelve"] or nc.num2words is None)

# --- the listener --------------------------------------------------------------------
work = tempfile.mkdtemp()
TEXT = {"001_A_One": "The harbour lay grey and still.", "002_B_One": 'Job not finished.',
        "003_C_One": "Nobody moved in the house that night at all, and the lamp went out."}
HEARD = {"001_A_One": "the harbour lay grey and still", "002_B_One": "job not fail",
         "003_C_One": "nobody moved in the house that night at all and the lamp went out"}
plan = [{"filename": n + ".mp3", "text": t} for n, t in TEXT.items()]
heard_calls = []


def fake_listen(wav, guard=False):
    heard_calls.append((os.path.basename(wav)[:-4], guard))
    return HEARD[os.path.basename(wav)[:-4]]


def land(name, nodes=True):
    open(os.path.join(work, name + ".wav"), "wb").write(b"x")
    open(os.path.join(work, name + ".txt"), "w").write(TEXT[name])
    if nodes:
        open(os.path.join(work, name + "_nodes.json"), "w").write("{}")
    os.utime(os.path.join(work, name + ".wav"), (1, 1))     # written long ago


L = nc.SlipListener(plan, work, listen=fake_listen, tail=5)
check("with a Whisper it is on, without one it is off",
      L.enabled and not nc.SlipListener(plan, work, listen=None).enabled or nc.transcriber() is not None)
check("nothing has landed: nothing to hear", L._ready() == [])
land("001_A_One"); land("002_B_One", nodes=False)
check("a chunk whose sidecars are not all written is not heard yet", L._ready() == ["001_A_One"], str(L._ready()))
land("002_B_One"); land("003_C_One")
open(os.path.join(work, "003_C_One.wav"), "wb").write(b"still being written")      # modified just now
check("nor one written a moment ago", sorted(L._ready()) == ["001_A_One", "002_B_One"], str(L._ready()))


async def go():
    task = asyncio.create_task(L.watch(every=0.05))
    await asyncio.sleep(0.4)
    task.cancel()
    return await L.finish()


suspects = asyncio.run(go())
check("the slip is found, the others are not", list(suspects) == ["002_B_One"], str(suspects))
check("a flagged chunk is heard again with the silence guard, a clean one once",
      heard_calls.count(("002_B_One", False)) == 1 and heard_calls.count(("002_B_One", True)) == 1
      and heard_calls.count(("001_A_One", False)) == 1 and ("001_A_One", True) not in heard_calls, str(heard_calls))
check("what finish() hears includes the chunk still being written, and each chunk is heard once",
      sorted({b for b, g in heard_calls}) == sorted(TEXT) and sum(1 for b, g in heard_calls if not g) == 3)

# Whisper's own invention is not the narrator's: flagged unguarded, gone with the guard.
state = {"n": 0}


def invents(wav, guard=False):
    return "job not finished" if guard else "job not finished and we will be right back"


L2 = nc.SlipListener(plan[1:2], work, listen=invents, tail=5)
land("002_B_One")
L2.hear("002_B_One")
check("a flag that vanishes with the silence guard was Whisper's", L2.suspects == {})


def crashes(wav, guard=False):
    raise RuntimeError("out of memory")


L3 = nc.SlipListener(plan[:1], work, listen=crashes, tail=5)
check("a hearing that fails is a chunk not checked, never a stopped book", L3.hear("001_A_One") == "" and not L3.suspects)

# --- time: what is not heard in the tail is left ------------------------------------------
import time  # noqa: E402


def slow(wav, guard=False):
    time.sleep(0.3)
    return HEARD[os.path.basename(wav)[:-4]]


for n in TEXT:
    land(n)
L4 = nc.SlipListener(plan, work, listen=slow, tail=0.4)
asyncio.run(L4.finish())
check("the tail is a budget: not every chunk is heard when it runs out", 0 < len(L4.heard) < 3, str(len(L4.heard)))

# --- a suspect is narrated again by the engine's own retake ---------------------------------
import sqlite3  # noqa: E402
import librerian_engine_v3 as eng  # noqa: E402
from core.database import setup_database  # noqa: E402

w2 = tempfile.mkdtemp()
out = os.path.join(w2, "output")
os.makedirs(out)
db = os.path.join(w2, "database.db")
setup_database(db)
plan2 = []
with sqlite3.connect(db) as conn:
    for i, n in enumerate(TEXT, 1):
        open(os.path.join(w2, n + ".wav"), "wb").write(b"take 1 " + n.encode())
        conn.execute("INSERT INTO chapters (book_id, chapter_index, audio_filename, duration) VALUES (1, ?, ?, 9.0)",
                     (i, n + ".mp3"))
        plan2.append({"book_id": 1, "chapter_index": i, "filename": n + ".mp3", "text": TEXT[n], "run_id": "run"})
        json.dump({"words": []}, open(os.path.join(out, n + ".json"), "w"))     # every chunk aligned
asked = []


class Pool:
    def urls(self):
        return ["http://pod0"]

    def __len__(self):
        return 1


async def worker(wid, queue, client, mfa_dir, db_path, url, provider, pod_urls=None, pool=None):
    while True:
        item = await queue.get()
        if item is None:
            return
        asked.append(item["filename"][:-4])
        open(os.path.join(mfa_dir, item["filename"][:-4] + ".wav"), "wb").write(b"take 2")
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO chapters (book_id, chapter_index, audio_filename, duration) VALUES (1, ?, ?, 9.0)",
                         (item["chapter_index"], item["filename"]))


async def align(pool, mfa_dir, run_id, language, key_to_name):
    for base in asked:
        json.dump({"words": []}, open(os.path.join(mfa_dir, "output", base + ".json"), "w"))


# What the engine does with the listener's answer, then the real retake step.
for base in suspects:
    os.remove(os.path.join(out, base + ".json"))
left = asyncio.run(eng.retake_refused(Pool(), plan2, w2, out, db, "run", "en", {}, None, retakes=2,
                                      worker=worker, align=align))
check("only the chunk that did not say its text is narrated again", asked == ["002_B_One"], str(asked))
check("and it is aligned again, so nothing is left refused", left == [], str(left))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
