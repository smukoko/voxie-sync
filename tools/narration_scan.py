#!/usr/bin/env python3
"""Listen to every narrated chunk and find the ones that do not say their text. Read-only.

The narrator sometimes says something else: in Twisted (2026-10-10) about 6 s
of "A. Dole. est ain't da all" where the text has "Paul's hand gripped the
gun". A whole-chunk score does not see it -- 6 s in a 2-minute chunk still
matches 95% -- so this looks for the local signature instead: a run of words
heard that the text does not have (babble), or of text words never said (a
skip). Whisper mishears a name or a number now and then, one or two words; a
run of --run words or more is flagged.

A flag is checked twice before it counts. Whisper invents a sign-off over
trailing silence ("I'll see you next time", "We'll be right back"), so a
flagged chunk is listened to again with Whisper's silence guard, and a flag
that vanishes is Whisper's, not the narrator's. Not the aligner's word
durations: forced through the wider beam, MFA gives time to words never said
(Twisted's 381_AS, 23 words skipped). That MFA refused a chunk at its normal
beam is the aligner's verdict, and it agreed with every skip found here.

Whisper also loops at a chunk's end, both times: Twisted's 837_H (2026-10-11)
was heard saying all 62 of its words and then "coming" 223 times in its last
0.3 s. A run of one word heard LOOP times or more as a chunk's last words is
dropped (one kept) when the audio could not hold it: the text alone fits the
chunk's length at the book's pace (1st to 99th percentile of its chunks'
words per minute), and everything heard would need more than the book's
fastest. A loop the narrator really said takes its time, and stays flagged.

    python tools/narration_scan.py mfa_workspace [--sample 200] [--run 4]

Reads each chunk's .wav and its .txt (what the narrator was given), writes
the report beside this run's output (--report), prints the flagged chunks.
Needs mlx-whisper (Apple silicon). The judging itself is core/narration_check.py,
which the engine also uses while it narrates.
"""
import argparse
import difflib
import glob
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.narration_check import (LOOP, PACE_WORDS, WHISPER, book_pace, drop_whisper_loop,  # noqa: E402,F401
                                  judge, listen, runs, words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", help="the run's mfa_workspace: NNN_X_Title.wav + .txt per chunk")
    ap.add_argument("--sample", type=int, default=0, help="this many chunks at random (0: all)")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--run", type=int, default=4, help="flag a run of this many words heard or skipped")
    ap.add_argument("--report", default=None, help="JSON report path (default: beside the workspace)")
    args = ap.parse_args()
    wavs = sorted(w for w in glob.glob(os.path.join(args.workspace, "*.wav"))
                  if os.path.exists(w[:-4] + ".txt"))
    if args.sample:
        wavs = sorted(random.Random(args.seed).sample(wavs, min(args.sample, len(wavs))))
    report = args.report or os.path.join(os.path.dirname(os.path.abspath(args.workspace)),
                                         "narration_scan.json")
    # The book's pace, from all its chunks even when only a sample is heard.
    from core.tts_worker import probe_duration
    every = sorted(w for w in glob.glob(os.path.join(args.workspace, "*.wav"))
                   if os.path.exists(w[:-4] + ".txt"))
    print(f"      measuring {len(every)} chunks for the book's pace ...", flush=True)
    length = {w: (len(words(open(w[:-4] + ".txt").read())), probe_duration(w)) for w in every}
    pace = book_pace(length.values())
    print(f"      pace: {'%.0f to %.0f words a minute' % pace if pace else 'too few chunks, no loop is dropped'}",
          flush=True)
    rows, started = [], time.time()
    for n, wav in enumerate(wavs, 1):
        want = words(open(wav[:-4] + ".txt").read())
        seconds = length[wav][1]
        heard = listen(wav)
        extra, missing, where, dropped = judge(heard, want, seconds, pace)
        row = {"chunk": os.path.basename(wav)[:-4], "words": len(want), "seconds": round(seconds, 2),
               "extra": extra, "missing": missing, "where": where, "heard": heard.strip()}
        if dropped:
            row["loop_dropped"] = dropped
        if extra >= args.run or missing >= args.run:
            # Again, with the silence guard: what vanishes was Whisper's.
            guarded = listen(wav, guard=True)
            extra, missing, where, dropped = judge(guarded, want, seconds, pace)
            row.update(extra=extra, missing=missing, where=where, heard_guarded=guarded.strip())
            if dropped:
                row["loop_dropped_guarded"] = dropped
            if extra >= args.run or missing >= args.run:
                print(f"FLAG  {row['chunk']}: {extra} heard not in the text, {missing} not said  ({where})",
                      flush=True)
        rows.append(row)
        if n % 100 == 0:
            print(f"      {n}/{len(wavs)} chunks, {time.time() - started:.0f} s", flush=True)
            json.dump(rows, open(report, "w"), indent=1)
    json.dump(rows, open(report, "w"), indent=1)
    flagged = [r for r in rows if r["extra"] >= args.run or r["missing"] >= args.run]
    print(f"\n{len(rows)} chunks listened to; {len(flagged)} flagged "
          f"({100 * len(flagged) / max(1, len(rows)):.1f}%). Report: {report}")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
