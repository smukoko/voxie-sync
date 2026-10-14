"""Adaptive chunk splitting: reassembly, naming, ordering, and the depth cap.

A chunk that fails twelve identical attempts is halved rather than asked a
thirteenth time. Three things have to hold or the split silently loses audio,
and two of them have cost this project narration before:

  * the halves must reassemble to the original text, word for word
  * the new filenames must match the stitcher's pattern -- a name it rejects is
    dropped from the chapter without a word in the log, which is how the
    alphabet overflow at chunk 27 lost 207 seconds
  * the halves must sort between their neighbours and in order, because the
    stitcher concatenates in sorted filename order

    python tools/test_chunk_split.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.chunking import split_chunk, MAX_SPLIT_DEPTH

STITCHER_PATTERN = re.compile(r'^\d{3}_[^_]+_(.+)\.wav$')
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make(nodes, filename="042_B_IX.mp3", depth=0):
    text = " ".join(n["text"] for n in nodes)
    cursor, rebuilt = 0, []
    for n in nodes:
        end = cursor + len(n["text"])
        rebuilt.append({**n, "start_idx": cursor, "end_idx": end,
                        "char_count": len(n["text"])})
        cursor = end + 1
    return {"book_id": 1, "chapter_index": 42, "filename": filename,
            "text": text, "nodes": rebuilt, "split_depth": depth}


def apply_names(_unused, halves):
    for n, half in enumerate(halves, start=1):
        stem, label, rest = half["filename"].split("_", 2)
        half["filename"] = f"{stem}_{label}{n}_{rest}"
    return halves


print("adaptive chunk split\n" + "=" * 60)

# 1. multi-node: split between paragraphs, no paragraph cut at all
nodes = [{"xpath": f"point(/1/4/{i})", "text": f"Paragraph number {i}. " * 6}
         for i in range(1, 7)]
ch = make(nodes)
halves = split_chunk(ch, "en")
check("a multi-node chunk splits in two", len(halves) == 2)
joined = " ".join(h["text"] for h in halves)
check("multi-node halves reassemble exactly", joined == ch["text"],
      f"{len(joined)} chars vs {len(ch['text'])}")
check("no paragraph was cut",
      sum(len(h["nodes"]) for h in halves) == len(nodes),
      f"{sum(len(h['nodes']) for h in halves)} nodes from {len(nodes)}")
sizes = [len(h["text"]) for h in halves]
check("halves are balanced", max(sizes) / min(sizes) < 1.6, f"{sizes}")

# 2. single node: must cut at a sentence boundary
one = [{"xpath": "point(/1/4/2)",
        "text": "The first sentence runs here. The second follows it. "
                "A third arrives now. And a fourth closes the paragraph."}]
ch1 = make(one)
h1 = split_chunk(ch1, "en")
check("a single-paragraph chunk still splits", len(h1) == 2)
if h1:
    rejoined = " ".join(h["text"] for h in h1)
    check("single-node halves reassemble exactly", rejoined == ch1["text"],
          f"{rejoined[:40]!r}...")
    check("the cut landed on a sentence boundary",
          h1[0]["text"].rstrip().endswith((".", "!", "?")),
          repr(h1[0]["text"][-25:]))
    check("both halves keep the paragraph's xpath",
          all(n["xpath"] == "point(/1/4/2)" for h in h1 for n in h["nodes"]))

# 3. one sentence, one paragraph: nothing safe to cut
check("an unsplittable chunk returns nothing",
      split_chunk(make([{"xpath": "p", "text": "Indivisible."}]), "en") == [])

# 4. naming and ordering -- the part that has silently eaten chunks twice
named = apply_names(ch, split_chunk(make(nodes), "en"))
names = [h["filename"].replace(".mp3", ".wav") for h in named]
check("split names match the stitcher pattern",
      all(STITCHER_PATTERN.match(n) for n in names), str(names))
check("split names keep the chapter title",
      all(STITCHER_PATTERN.match(n).group(1) == "IX" for n in names))
neighbours = sorted(["041_A_IX.wav", "043_C_IX.wav"] + names)
check("halves sort between their neighbours and in order",
      neighbours == ["041_A_IX.wav", "042_B1_IX.wav", "042_B2_IX.wav", "043_C_IX.wav"],
      str(neighbours))

# 5. depth cap
deep = make(nodes, depth=MAX_SPLIT_DEPTH)
check(f"splitting stops at depth {MAX_SPLIT_DEPTH}", split_chunk(deep, "en") == [])
# Renamed first, exactly as the worker does before requeuing, so the second
# split starts from 042_B1_IX and not from the name it replaced.
lvl1 = apply_names(None, split_chunk(make(nodes), "en"))
lvl2 = apply_names(None, split_chunk(lvl1[0], "en"))
check("a half can be split once more", len(lvl2) == 2)
check("and its halves are at the cap",
      all(h["split_depth"] == MAX_SPLIT_DEPTH for h in lvl2))
n2 = sorted(h["filename"].replace(".mp3", ".wav") for h in lvl2)
check("twice-split names still match the pattern",
      all(STITCHER_PATTERN.match(n) for n in n2), str(n2))
check("twice-split names sort under their parent",
      sorted(n2 + ["042_B2_IX.wav", "043_C_IX.wav"]) ==
      ["042_B11_IX.wav", "042_B12_IX.wav", "042_B2_IX.wav", "043_C_IX.wav"],
      str(sorted(n2 + ["042_B2_IX.wav", "043_C_IX.wav"])))

# 6. the retry budget must reset, or the halves die untried
check("halves start with a fresh retry budget",
      all(h.get("requeue_count") == 0 for h in lvl1))

# 7. offsets must be rebuilt against the half, not inherited
for h in lvl1:
    cursor = 0
    for n in h["nodes"]:
        if n["start_idx"] != cursor:
            check("offsets are rebuilt per half", False,
                  f"{n['start_idx']} != {cursor}")
            break
        cursor = n["end_idx"] + 1
    else:
        continue
    break
else:
    check("offsets are rebuilt per half", True)

print("=" * 60)
failed = [r for r in results if not r[1]]
print(f"{len(results)} checks: {len(results) - len(failed)} passed, {len(failed)} FAILED")
sys.exit(1 if failed else 0)
