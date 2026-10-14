"""The landing rules (core/landing.py) against tools/landing_examples.json.

The same file is run against the plugin's Lua (tools/test_landing.lua), so a
rule changed on one side only fails here or there. Also checks the device map
itself: what the gateway packs is what the examples describe, positions only,
and its checksum and length are the ones the plugin needs.

    python tools/test_landing.py
"""
import hashlib
import json
import os
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import landing  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


with open(os.path.join(ROOT, "tools", "landing_examples.json"), encoding="utf-8") as f:
    ex = json.load(f)
nodes = [tuple(n) for n in ex["map"]["nodes"]]
chapters = [tuple(c) for c in ex["map"]["chapters"]]

check("chapters are grouped from chunk rows the way the stitcher groups them",
      landing.audio_chapters([tuple(r) for r in ex["chunk_rows"]]) == chapters,
      landing.audio_chapters([tuple(r) for r in ex["chunk_rows"]]))

for case in ex["to_page"]:
    got = landing.landing_position(nodes, chapters, case["seconds"])
    check(f"to page  {case['seconds']:>6}s: {case['why']}", got == case["position"], f"(got {got})")

for case in ex["to_audio"]:
    found = landing.time_for_position(nodes, case["position"]) is not None
    got = landing.audio_time(nodes, chapters, case["position"], case["percentage"])
    check(f"to audio {case['position'][-24:]:>24}: {case['why']}",
          abs(got - case["seconds"]) < 1e-6 and found == case["from_map"], f"(got {got}, from map {found})")

for case in ex["chapter_at"]:
    n, off = landing.chapter_at(chapters, case["seconds"])
    check(f"chapter at {case['seconds']}s", n == case["chapter"] and abs(off - case["offset"]) < 1e-6,
          f"(got chapter {n}, {off}s)")

check("an empty map lands nowhere", landing.landing_position([], chapters, 10.0) is None)

# The map the device downloads, built from the gateway's own rows.
node_rows = [(f"point(/1{p})", s, e) for p, s, e in reversed(nodes)]   # stored form, any order
book_map = landing.build_map(node_rows, [tuple(r) for r in ex["chunk_rows"]])
check("the device map is the examples' map", book_map["nodes"] == [list(n) for n in nodes]
      and book_map["chapters"] == [list(c) for c in chapters], json.dumps(book_map)[:200])
check("it carries positions and chapter titles only", set(book_map) == {"v", "chapters", "nodes"}
      and all(len(n) == 3 for n in book_map["nodes"]))
packed, digest, length = landing.pack_map(book_map)
raw = zlib.decompress(packed)
check("zlib's own format, inflating to the stated length", len(raw) == length and json.loads(raw) == book_map)
check("the checksum is of the bytes the device stores", digest == hashlib.sha256(packed).hexdigest())

# Size, for the decision's "about 60 KB for a 16-hour novel": a synthetic book
# of 5,000 paragraphs at Redeeming Love's density (4,924 paragraphs, 16 h).
big = [(f"point(/1/body/DocFragment[{9 + i // 400}]/body/div/div/div/p[{i % 400 + 1}])", i * 11.7, i * 11.7 + 11.7)
       for i in range(5000)]
rows = [(f"{c + 1:03d}_A_Chapter {c + 1}.mp3", 11.7 * 125) for c in range(40)]
size = len(landing.pack_map(landing.build_map(big, rows))[0])
check("a 16-hour book's map stays small", size < 120_000, f"({size // 1024} KB compressed)")

# Past chunk 999 the name has four digits; three-digit matching made every such
# chunk its own "chapter", named after its whole file name (Twisted, 2026-10-07).
rows = [("998_A_Chapter Nine.mp3", 10.0), ("999_B_Chapter Nine.mp3", 10.0),
        ("1000_C_Chapter Nine.mp3", 10.0), ("1001_A_Chapter Ten.mp3", 5.0)]
check("chapters past chunk 999 are still grouped by title",
      landing.audio_chapters(rows) == [("Chapter Nine", 0.0, 30.0), ("Chapter Ten", 30.0, 35.0)],
      str(landing.audio_chapters(rows)))

print("=" * 60)
print(f"{passed + failed} checks: {passed} passed, {failed} FAILED")
sys.exit(1 if failed else 0)
