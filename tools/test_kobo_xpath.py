#!/usr/bin/env python3
"""The position handed back to KOReader must resolve on the device.

The failure this exists to prevent has a distinctive symptom -- the reader
jumps to page one -- because an xpointer KOReader cannot resolve is not an
error it reports, it is a position it cannot honour.

The trap is that the device does not read the file this project parsed. The
engine builds its map from the source EPUB; the Kobo reads the KEPUB, where
kepubify has wrapped every run of text in <span class="koboSpan">. So a
paragraph that has a text node in the EPUB has none in the KEPUB, and
`/…/p[157]/text().42` addresses nothing at all.

    python tools/test_kobo_xpath.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ABS_URL", "http://127.0.0.1:13378")
os.environ.setdefault("ABS_API_TOKEN", "test")

import librerian_gateway_v3 as gw  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


STORED = "point(/1/body/DocFragment[44]/body/div/div/p[157])"

out = gw.prepare_kobo_xpath(STORED, 0)
check("an offset-less path is unwrapped to an element pointer",
      out == "/body/DocFragment[44]/body/div/div/p[157]", out)

out = gw.prepare_kobo_xpath(STORED, 42)
check("a word offset does NOT become /text().N — it would not resolve in a kepub",
      "/text()." not in out, out)
check("and the element pointer is returned instead",
      out == "/body/DocFragment[44]/body/div/div/p[157]", out)

# The round trip is the invariant that ties the two directions together: what
# we hand out must be something we would accept back and match to the same
# node. A pointer that fails this desynchronises the reader from the map.
for offset in (0, 1, 42, 999):
    emitted = gw.prepare_kobo_xpath(STORED, offset)
    check(f"round trip at offset {offset}: clean_xpath(emitted) == stored",
          gw.clean_xpath(emitted) == STORED, gw.clean_xpath(emitted))

# What the device really sends, span components and all, must still match.
for sent in (
    "/body/DocFragment[44]/body/div/div/p[157]/span[2]/text().5",
    "/body/DocFragment[44]/body/div/div/p[157]/text().0",
    "/body/DocFragment[44]/body/div/div/p[157]",
):
    check(f"inbound still normalises: {sent[-28:]}",
          gw.clean_xpath(sent) == STORED, gw.clean_xpath(sent))

check("the switch is documented, not deleted", hasattr(gw, "EMIT_TEXT_OFFSETS"))
check("and it is off", gw.EMIT_TEXT_OFFSETS is False)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
