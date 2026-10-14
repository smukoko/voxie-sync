#!/usr/bin/env python3
"""The typesetter's furniture, which a PDF-converted EPUB narrates as text.

Redeeming Love was converted from a print PDF, so every page boundary left
three things in the block stream: the page number, a production footer
(`Redeem.000 7/25/07 4:38 PM Page 12`), and the running head spelled out one
letter per block. 49.1% of its 24,444 blocks. The footer lands mid-sentence,
so the listener would have heard

    "Can't I eleven Redeem dot zero zero zero seven twenty-five zero seven
     four thirty-eight P M Page twelve stay? I'll be very quiet."

These rules only ever DELETE, so the test that matters is not that they catch
the artifacts -- it is that they catch nothing else.

    python tools/test_print_artifacts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.structure import drop_print_artifacts  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


def blocks(*texts):
    return [(object(), t) for t in texts]


def kept_texts(pairs):
    kept, counts = drop_print_artifacts(pairs)
    return [t for _, t in kept], counts


# --- what it must remove ---------------------------------------------------
t, c = kept_texts(blocks("Real prose here, at length.",
                         "Redeem.000 7/25/07 4:38 PM Page 12",
                         "More real prose."))
check("a production footer is dropped", t == ["Real prose here, at length.", "More real prose."]
      and c["footer"] == 1, str(t))

run = ["R", "E", "D", "E", "E", "M", "I", "N", "G"]
t, c = kept_texts(blocks("Prose before.", *run, "Prose after."))
check("a letter-spaced running head is dropped",
      t == ["Prose before.", "Prose after."] and c["letter-run"] == 9, str(t))

many = [str(i) for i in range(1, 40)]
t, c = kept_texts(blocks("Prose.", *many))
check("page numbers go once there are enough of them",
      t == ["Prose."] and c["page-number"] == 39, f"{len(t)} kept")

# --- what it must NOT remove ----------------------------------------------
t, c = kept_texts(blocks("Chapter", "12", "It was the best of times."))
check("a lone numeric heading survives", t == ["Chapter", "12", "It was the best of times."],
      str(t))

t, c = kept_texts(blocks("I", "The first chapter begins here."))
check("a lone Roman numeral survives", t == ["I", "The first chapter begins here."], str(t))

t, c = kept_texts(blocks("A", "B", "Prose in between breaks the run.", "C", "D"))
check("runs shorter than three survive", len(t) == 5, str(t))

t, c = kept_texts(blocks("She turned to Page 12 of the ledger, dated 7/25/07, and paused "
                         "for a long moment before reading what her father had written."))
check("prose mentioning a date and a page survives", len(t) == 1 and c["footer"] == 0,
      f"{len(t)} kept")

t, c = kept_texts(blocks("Prose.", "7", "More prose.", "8"))
check("a few bare numbers survive (below the threshold)", len(t) == 4, str(t))

# --- the real book, and the ones that must be untouched --------------------
import glob  # noqa: E402
import re  # noqa: E402
import zipfile  # noqa: E402


def epub_documents(path):
    """[(name, [(tag, text)])], one per content document.

    Per document, because the engine filters each spine file on its own
    (librerian_engine_v3.py, cartographer). Pooled over the whole book, Het
    verweer's 72 chapter numbers -- "<p>7</p>" opening each chapter's file --
    read as page numbers, a drop the engine never makes.
    """
    out = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(x for x in z.namelist()
                        if x.lower().endswith((".htm", ".html", ".xhtml"))):
            html = z.read(n).decode("utf-8", "replace")
            doc = []
            for m in re.finditer(r"<(p|h[1-6])[^>]*>(.*?)</\1>", html, re.S):
                txt = re.sub(r"<[^>]+>", "", m.group(2))
                txt = txt.replace("&nbsp;", " ").replace("&#160;", " ").strip()
                if txt:
                    doc.append((m.group(1), txt))
            out.append((n, doc))
    return out


library = sorted(glob.glob(os.path.expanduser("~/Kobo_Archive/*.epub")))
if library:
    untouched = True
    detail = ""
    for p in library:
        try:
            docs = epub_documents(p)
        except Exception:
            continue
        for name, b in docs:
            kept, _ = drop_print_artifacts(b)
            if len(kept) != len(b):
                untouched = False
                detail = f"{os.path.basename(p)} {name}: {len(b) - len(kept)} dropped"
    check(f"every book already in the library is untouched ({len(library)} epubs)",
          untouched, detail)
else:
    print("ℹ️  no library EPUBs on this machine; skipping the regression sweep")

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
