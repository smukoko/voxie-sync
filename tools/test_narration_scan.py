"""The narration scan drops Whisper's loops at a chunk's end, and only those.

Twisted's 837_H (2026-10-11): 62 words in 30.34 s, all of them heard, then
"coming" 223 times in the last 0.3 s, in both hearings. It was flagged as 223
words of babble. 1732_AG (2026-10-07): 207 words in 113.4 s, with 7 words of
real babble mid-chunk ("a dole est ain't da all all's"); it must stay flagged.
The shapes and numbers are theirs, the text is not (the book is not ours to
copy). The paces are the runs' own: 1st to 99th percentile of each run's
chunks of 10+ words (book_pace over the whole workspace, 2026-10-12). Their
real hearings, judged the same day: 837_H's recorded loop dropped in both
(1 heard, 2 missing), 1732_AG still 7 heard in both.

Offline: no Whisper, no audio.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import narration_scan as scan  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


TWISTED = (74.9, 252.8)       # the 2026-10-11 run (837_H)
TWISTED_1007 = (72.5, 262.9)  # the 2026-10-07 run (1732_AG)
RUN = 4                       # the scan's default --run
VOCAB = ("the harbour lay grey and still under a low sky and nobody on the quay looked up when "
         "the ferry came in late again with its lights burning").split()


def text(n):
    return " ".join(VOCAB[i % len(VOCAB)] for i in range(n))


def flagged(extra, missing):
    return extra >= RUN or missing >= RUN


# --- 837_H: the loop is Whisper's -------------------------------------------
said_837 = text(62) + " coming" * 223
want_837 = scan.words(text(62))
extra, missing, where, dropped = scan.judge(said_837, want_837, 30.34, TWISTED)
check("837_H: a loop of 223 at the end of a chunk whose text fits its length is dropped",
      dropped == "coming x223" and not flagged(extra, missing), f"({extra}, {missing}, {dropped!r})")
last = want_837[-1]
check("one of its words is kept: a loop of the text's own last word costs nothing",
      scan.judge(text(62) + (" " + last) * 223, want_837, 30.34, TWISTED)[:2] == (0, 0))

# --- 1732_AG: real babble stays flagged --------------------------------------
want_1732 = scan.words(text(207))
babble = "a dole est ain't da all all's"
said_1732 = " ".join(want_1732[:120] + babble.split() + want_1732[121:])
extra, missing, where, dropped = scan.judge(said_1732, want_1732, 113.4, TWISTED_1007)
check("1732_AG: babble mid-chunk is still flagged, nothing dropped",
      flagged(extra, missing) and extra == 7 and not dropped, f"({extra}, {missing}, {where!r})")
extra, missing, where, dropped = scan.judge(said_1732 + " gun" * 30, want_1732, 113.4, TWISTED_1007)
check("and a loop after it does not hide it", flagged(extra, missing) and dropped == "",
      f"({extra}, {missing}, {dropped!r})")

# --- a loop the narrator said: the audio has room for it ---------------------
# 62 words and 40 more of "coming" at the book's median pace take ~46 s.
extra, missing, where, dropped = scan.judge(text(62) + " coming" * 40, want_837, 46.0, TWISTED)
check("a loop with the time to have been said is the narrator's: kept and flagged",
      flagged(extra, missing) and not dropped, f"({extra}, {missing}, {dropped!r})")

# --- a chunk whose text does not fit its length ------------------------------
# 62 words in 80 s (47 a minute) is slower than anything in the book: the
# audio holds something else, and the loop may be part of it.
extra, missing, where, dropped = scan.judge(said_837, want_837, 80.0, TWISTED)
check("a chunk slower than the book's slowest keeps its loop", flagged(extra, missing) and not dropped,
      f"({extra}, {missing}, {dropped!r})")

# --- only a loop, only at the end, only of five or more ----------------------
extra, missing, where, dropped = scan.judge(text(62) + " coming" * 4, want_837, 30.34, TWISTED)
check("four in a row is not a loop", extra == 4 and not dropped, f"({extra}, {dropped!r})")
said = " ".join(want_837[:30] + ["coming"] * 50 + want_837[30:])
extra, missing, where, dropped = scan.judge(said, want_837, 30.34, TWISTED)
check("a run mid-chunk is not dropped", flagged(extra, missing) and not dropped, f"({extra}, {dropped!r})")
extra, missing, where, dropped = scan.judge(said_837, want_837, 30.34, None)
check("with no pace for the book nothing is dropped", extra == 223 and not dropped)
extra, missing, where, dropped = scan.judge(said_837, want_837, 0.0, TWISTED)
check("nor for a chunk of unknown length", extra == 223 and not dropped)

# --- the book's pace ---------------------------------------------------------
chunks = [(n, 60 * n / wpm) for n, wpm in ((30, 100), (40, 140), (50, 120))] * 10 + [(3, 0.5)] * 50
pace = scan.book_pace(chunks)
check("the book's pace is read from its chunks of 10+ words",
      pace and abs(pace[0] - 100) < 1e-6 and abs(pace[1] - 140) < 1e-6, f"({pace})")
check("and is not guessed from too few", scan.book_pace([(30, 15.0)] * 19) is None)
check("a chunk of no length is left out", scan.book_pace([(30, 15.0)] * 20 + [(30, 0.0)]) == (120.0, 120.0))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
