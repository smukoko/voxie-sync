"""Does a narrated chunk say its text? The judging of what Whisper heard, shared by
the scan tool (tools/narration_scan.py) and the engine's listening pass.

The narrator sometimes says something else: in Twisted (2026-10-10) about 6 s
of "A. Dole. est ain't da all" where the text has "Paul's hand gripped the
gun". A whole-chunk score does not see it -- 6 s in a 2-minute chunk still
matches 95% -- so this looks for the local signature instead: a run of words
heard that the text does not have (babble), or of text words never said (a
skip). Whisper mishears a name or a number now and then, one or two words; a
run of RUN words or more is flagged.

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
"""
import difflib
import re

WHISPER = "mlx-community/whisper-large-v3-turbo"
try:
    from num2words import num2words
except ImportError:      # optional: digits then compare as digits
    num2words = None


def words(text):
    """Lower-case words, tags dropped, numbers spelled out as the narrator says them."""
    text = re.sub(r"\[[a-z_]+\]", " ", (text or "").lower().replace("’", "'"))
    if num2words:
        text = re.sub(r"\d+(?:\.\d+)?", lambda m: " " + num2words(float(m.group()) if "." in m.group()
                                                                    else int(m.group())) + " ", text)
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.replace("-", " "))


def listen(wav, guard=False):
    import mlx_whisper
    from core.hf_cache import model_path
    extra = {"word_timestamps": True, "hallucination_silence_threshold": 1.0} if guard else {}
    return mlx_whisper.transcribe(wav, path_or_hf_repo=model_path(WHISPER), language="en",
                                  condition_on_previous_text=False, **extra)["text"]


LOOP = 5          # one word this many times in a row, as a chunk's last words
PACE_WORDS = 10   # shorter chunks are a heading or a line: their pace says little


def book_pace(chunks):
    """(slowest, fastest) words per minute of a book's narration, from its
    chunks' (text words, seconds); None when too few chunks say."""
    paces = sorted(60 * n / s for n, s in chunks if n >= PACE_WORDS and s > 0)
    if len(paces) < 20:
        return None
    return paces[int(0.01 * (len(paces) - 1))], paces[int(0.99 * (len(paces) - 1))]


def drop_whisper_loop(said, want, seconds, pace):
    """`said` without a loop Whisper made up at the chunk's end, and what was
    dropped ("" for nothing): see the module's docstring."""
    n = 0
    while n < len(said) and said[-1 - n] == said[-1]:
        n += 1
    if n < LOOP or not (pace and seconds):
        return said, ""
    slow, fast = pace
    if slow <= 60 * len(want) / seconds <= fast < 60 * len(said) / seconds:
        return said[:len(said) - n + 1], f"{said[-1]} x{n}"
    return said, ""


def judge(heard, want, seconds, pace):
    """(extra, missing, where, loop dropped) for one hearing of a chunk."""
    said, dropped = drop_whisper_loop(words(heard), want, seconds, pace)
    return (*runs(said, want), dropped)


def runs(said, want):
    """(longest run of heard words the text lacks, longest run of text words not heard, where)."""
    m = difflib.SequenceMatcher(None, want, said, autojunk=False)
    extra = missing = 0
    where = ""
    for op, i1, i2, j1, j2 in m.get_opcodes():
        if op in ("insert", "replace") and j2 - j1 > extra:
            extra, where = j2 - j1, " ".join(said[j1:j2])[:80]
        if op in ("delete", "replace") and i2 - i1 > missing:
            missing = i2 - i1
            if not where:
                where = "skipped: " + " ".join(want[i1:i2])[:70]
    return extra, missing, where




# --- the engine's listening pass ------------------------------------------------------
#
# 2026-10-22, Winning: `"Job not finished."` was narrated "job not fail". The
# pod's aligner placed "finished" in 0.2 s at the very end of the file and said
# nothing: a forced aligner measures where words are, it cannot tell that a word
# was mangled, and the only chunks the engine retook were the 11 that MFA refused.
# Hearing every chunk with Whisper, as the scan does, finds these. In a long chunk
# a mangled word is one in a hundred and Whisper's own slips are as common, so
# only a run is flagged; in a line of a few words one mangled word is the line.
SHORT_LINE = 8       # words: at most this many, and a single bad word counts
LONG_WORD = 4        # letters: shorter words are Whisper's to drop ("one", "oh")
SIMILAR = 0.6        # a heard word this close to the text's is the same word, mispronounced by Whisper


def mangled(heard, want):
    """The text's words (of LONG_WORD letters or more) that were replaced by
    something unlike them or left out, in a short line. [] when it was said."""
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, want, heard, autojunk=False).get_opcodes():
        if op == "equal" or op == "insert":
            continue
        for w in want[i1:i2]:
            if len(w) < LONG_WORD:
                continue
            best = max((difflib.SequenceMatcher(None, w, h).ratio() for h in heard[j1:j2]), default=0.0)
            if best < SIMILAR:
                out.append(w)
    return out


def slip(heard_text, want_text, seconds, pace, run=LOOP - 1):
    """Why a chunk does not say its text, or "": a run of words heard that the
    text lacks or of text words not said (`judge`), or, in a short line, a
    single word that is not the one written."""
    want = words(want_text)
    extra, missing, where, _ = judge(heard_text, want, seconds, pace)
    if extra >= run or missing >= run:
        return where or f"{extra} heard, {missing} missing"
    if len(want) <= SHORT_LINE:
        said, _ = drop_whisper_loop(words(heard_text), want, seconds, pace)
        bad = mangled(said, want)
        if bad:
            return "short line, not said: " + " ".join(bad)
    return ""


def transcriber():
    """fn(wav, guard=False) -> text, from the Whisper this machine has, or None.

    mlx-whisper on Apple silicon, faster-whisper elsewhere; neither is a hard
    dependency (Kokoro runs need neither), so no Whisper means no listening pass.
    """
    import importlib.util
    if importlib.util.find_spec("mlx_whisper"):
        return listen
    if importlib.util.find_spec("faster_whisper"):
        state = {}

        def faster(wav, guard=False):
            from faster_whisper import WhisperModel
            if "m" not in state:
                state["m"] = WhisperModel("large-v3-turbo", device="auto", compute_type="int8")
            segments, _ = state["m"].transcribe(
                wav, language="en", condition_on_previous_text=False,
                **({"hallucination_silence_threshold": 1.0, "word_timestamps": True} if guard else {}))
            return " ".join(s.text for s in segments)
        return faster
    return None


class SlipListener:
    """Hears each chunk as it lands, while the pods are still narrating, and
    keeps the names of the ones that do not say their text.

    Overlapped with the narration on purpose: hearing a book costs about as long
    as narrating it on one pod, and the pods bill while this runs. What is not
    heard by the end of the narration is heard for at most `tail` seconds more,
    and then left; a chunk not heard is a chunk not checked, as before.
    """

    def __init__(self, plan, mfa_dir, listen=None, tail=None):
        import os
        self.mfa_dir = mfa_dir
        self.listen = listen if listen is not None else transcriber()
        self.names = {(c["filename"][:-4] if c["filename"].endswith(".mp3") else c["filename"]): c
                      for c in plan}
        self.heard, self.suspects = set(), {}
        self.tail = float(os.getenv("LISTEN_TAIL_SECONDS", "240")) if tail is None else tail

    @property
    def enabled(self):
        return self.listen is not None

    def _ready(self, settle=1.5):
        """Chunks whose audio and sidecars are all written, not yet heard."""
        import os
        import time
        out = []
        for base in self.names:
            if base in self.heard:
                continue
            wav = os.path.join(self.mfa_dir, base + ".wav")
            try:
                if (os.path.exists(os.path.join(self.mfa_dir, base + ".txt"))
                        and os.path.exists(os.path.join(self.mfa_dir, base + "_nodes.json"))
                        and time.time() - os.path.getmtime(wav) >= settle):
                    out.append(base)
            except OSError:
                continue
        return out

    def hear(self, base):
        """Listen to one chunk (blocking). Returns the reason it is a slip, or ""."""
        import os
        self.heard.add(base)
        wav = os.path.join(self.mfa_dir, base + ".wav")
        try:
            want = open(os.path.join(self.mfa_dir, base + ".txt"), encoding="utf-8").read()
            why = slip(self.listen(wav), want, 0.0, None)
            if why:
                # Again with the silence guard: what vanishes was Whisper's.
                why = slip(self.listen(wav, guard=True), want, 0.0, None)
        except Exception:
            return ""           # a failed hearing is a chunk not checked, never a stopped book
        if why:
            self.suspects[base] = why
            print(f"   👂 {base}: {why}", flush=True)
        return why

    async def watch(self, every=5.0):
        """Hear chunks as they land, until cancelled."""
        import asyncio
        while True:
            ready = self._ready()
            for base in ready:
                await asyncio.to_thread(self.hear, base)
            if not ready:
                await asyncio.sleep(every)

    async def finish(self):
        """Hear what is left, for at most `tail` seconds. Returns {chunk: reason}."""
        import asyncio
        import time
        deadline = time.monotonic() + self.tail
        left = self._ready(settle=0.0)
        for base in left:
            if time.monotonic() > deadline:
                print(f"   👂 Out of time: {len([b for b in self.names if b not in self.heard])} "
                      "chunk(s) were not listened to.")
                break
            await asyncio.to_thread(self.hear, base)
        print(f"   👂 Listened to {len(self.heard)} of {len(self.names)} chunks: "
              f"{len(self.suspects)} do not say their text.")
        return dict(self.suspects)
