"""The narration director: JEV decides how each paragraph is performed.

Tested by ear on 2026-10-06 (the baseline; CLAUDE.md, "Narration director"):
the same Poe passage in a cloned English voice, plain and directed,
and the directed take was the one the user called amazing -- frantic lines 2.5x
faster, the shriek +5.4 dB, the hushed lines 2-5 dB quieter, with Whisper's word
match unchanged at 98%. English only, as all narration (core/languages.py):
in French the instruction cost the speaker's accent (words 92% -> 83%).

JEV returns typed decisions, never text: it cannot touch the author's words. It
chooses from the baseline's eight deliveries, and each maps to exactly one
thing CosyVoice 3 knows natively: one of the instructions it was trained on
(upstream cosyvoice/utils/common.py instruct_list, copied verbatim below), or
none for plain, plus a breath from its tokenizer where JEV judges one helps.

Back to the baseline's mapping (user, 2026-10-12): a richer menu of sixteen,
composed with speed changes and sigh and laugh tags, made the user's cloned
voice sound less natural in the README's scenes than the baseline did. Each
option beyond these is added back only after it has been tested on its own.

Only providers that declare SUPPORTS_DIRECTION are directed: Kokoro has no
instruction input (a speed-and-volume take was indistinguishable by ear).
"""
import asyncio
import os
from dataclasses import dataclass

# CosyVoice 3's own instruction strings. Verbatim, Chinese included: these are
# the phrasings it was trained to follow, and an English paraphrase of the
# Chinese ones is a guess the model was never taught.
_P, _E = "You are a helpful assistant. ", "<|endofprompt|>"
INSTRUCTIONS = {
    "happy": _P + "请非常开心地说一句话。" + _E,
    "sad": _P + "请非常伤心地说一句话。" + _E,
    "angry": _P + "请非常生气地说一句话。" + _E,
    "soft": _P + "Please say a sentence in a very soft voice." + _E,
    "loud": _P + "Please say a sentence as loudly as possible." + _E,
    "slow": _P + "请用尽可能慢地语速说一句话。" + _E,
    "fast": _P + "请用尽可能快地语速说一句话。" + _E,
}

# The vocal tags the director may put in front of a paragraph (CosyVoice 3's
# tokenizer, special tokens). The server strips these before alignment.
TAGS = ("[breath]", "[quick_breath]")

@dataclass(frozen=True)
class Delivery:
    criteria: str                 # what JEV is told this choice means
    instruct: str = None          # key into INSTRUCTIONS, or None
    tag: str = ""                 # one of TAGS, put before the paragraph


# The baseline's eight (~/Desktop/voxie-emotion-test/baseline-2026-10-06,
# director.py), wording and mapping verbatim: one native instruction each,
# never a speed change, never a sigh or a laugh.
DELIVERIES = {
    "plain": Delivery("Calm, ordinary narration or speech with no marked emotion."),
    "joyful": Delivery("Delight, happiness, warmth, wonder, triumph or pride.", "happy"),
    "sorrowful": Delivery("Grief, despair, melancholy, resignation or defeat.", "sad"),
    "angry": Delivery("Anger, fury, indignation, accusation or contempt, spoken with force but not screamed.",
                      "angry"),
    "hushed": Delivery("Whispered, secretive, cautious, stealthy, or quiet dread: the voice drops.", "soft"),
    "frantic": Delivery("Panic, terror, breathless agitation, words rushing out.", "fast", "[quick_breath]"),
    "shouted": Delivery("Screaming, shrieking or shouting at the top of the voice.", "loud"),
    "heavy": Delivery("Slow, solemn, exhausted or deliberate weight on every word.", "slow"),
}

BREATH_THRESHOLD = 0.7     # the baseline's value
CONTEXT_BLOCKS = 3         # neighbours on each side that JEV reads (2 until 2026-10-22)
# JEV answers a choice with a probability for every delivery. Until 2026-10-22
# only the top choice was read, so "heavy" at 0.44 against "plain" at 0.42 slowed
# a paragraph to a crawl. A delivery has to beat plain by this much.
MIN_MARGIN = float(os.getenv("DIRECTOR_MIN_MARGIN", "0.1"))
# ...and the paragraph has to be a scene: events, a character feeling or
# speaking. An author explaining, arguing or advising is one voice, however
# forceful ("Winning wants to take your control" came back angry); the narrator
# reads that plainly (the user: exaggerated narration "becomes weird"). Measured
# on JEV's answers for six books, 2026-10-22: Winning, a self-help book, went
# from 69% of its paragraphs acted (angry 329, heavy 361) to 8%; the fiction
# kept 74-89% (Cask 91 -> 89, Yellow Wallpaper 90 -> 84, Canterville 74 -> 74).
MIN_SCENE_P = float(os.getenv("DIRECTOR_MIN_SCENE_P", "0.5"))
# Headings, numbers and credits ("One.", "By", "Wilde...") are read neutrally:
# Canterville's byline came back "amused". Not asked at all, which also saves
# the call.
MIN_DIRECTED_WORDS = 4


@dataclass(frozen=True)
class Direction:
    delivery: str
    instruct_text: str = None
    speed: float = 1.0
    tag: str = ""

    @property
    def plain(self):
        return not self.instruct_text and self.speed == 1.0 and not self.tag

    def key(self):
        """Part of the chunk cache key: a directed take is a different take."""
        return f"{self.delivery}|{self.instruct_text or ''}|{self.speed}|{self.tag}"

    def as_dict(self):
        return {"delivery": self.delivery, "instruct_text": self.instruct_text,
                "speed": self.speed, "tag": self.tag}

    @classmethod
    def from_dict(cls, d):
        return cls(**d) if d else None


PLAIN = Direction("plain")


def settle(answers):
    """The delivery to use from JEV's answers: its top choice when it clearly beats
    plain and the paragraph is a scene, else plain.

    Answers without probabilities (an older JEV, a test's fake) are taken as
    they come: the top choice, as before.
    """
    delivery = answers["delivery"]
    choice = delivery["choice"]
    if choice == "plain":
        return "plain"
    probs = delivery.get("probabilities") or {}
    if choice in probs and "plain" in probs and probs[choice] - probs["plain"] < MIN_MARGIN - 1e-9:
        return "plain"
    scene = ((answers.get("register") or {}).get("probabilities") or {}).get("scene")
    if scene is not None and scene < MIN_SCENE_P:
        return "plain"
    return choice


def direction_for(delivery, language, p_breath=0.0):
    """The controls for one paragraph from JEV's answers: one instruction, a breath."""
    d = DELIVERIES.get(delivery) or DELIVERIES["plain"]
    instruct = INSTRUCTIONS[d.instruct] if d.instruct else None
    tag = d.tag or ("[breath]" if p_breath >= BREATH_THRESHOLD else "")
    return Direction(delivery if delivery in DELIVERIES else "plain", instruct, 1.0, tag)


QUESTIONS = {
    "delivery": {
        "type": "choice",
        "instructions": ("You are directing an audiobook narrator. How should `line` be performed? "
                         "Judge it from the story: `book` and `chapter` say what it is part of, and "
                         "`before` and `after` are the paragraphs on either side, so look ahead as well "
                         "as back. A scene's mood carries over unless the text turns."),
        "criteria": {name: d.criteria for name, d in DELIVERIES.items()},
    },
    "register": {
        "type": "choice",
        "instructions": ("Is `line` a scene, or the author speaking to the reader? `book` and "
                         "`chapter` say what it is part of; `before` and `after` are its neighbours."),
        "criteria": {
            "scene": "A story told as it happens: events, a character acting, feeling or speaking, "
                     "including quoted speech.",
            "exposition": "The author in their own voice: explaining, arguing, advising, reflecting, "
                          "addressing the reader or listing, however forceful or emotional it is.",
        },
    },
    "breath_before": {
        "type": "noul",
        "instructions": ("Would a skilled narrator take an audible breath just before `line` "
                         "(a new paragraph, a turn in the scene, or before an outburst)?"),
        "criteria": {"true": "An audible breath before this line helps the performance.",
                     "false": "No audible breath: the line flows on from the one before."},
    },
}


def mode(provider, env=None):
    """'on' or 'off'. NARRATION_DIRECTOR = auto (default) | jev | off.

    auto directs when the provider can be directed and an OpenRouter key exists;
    jev insists on it and fails loudly without a key; off never directs. The
    free route (Kokoro, no keys) is never directed and needs nothing.
    """
    env = os.environ if env is None else env
    choice = (env.get("NARRATION_DIRECTOR") or "auto").strip().lower()
    supported = bool(getattr(provider, "SUPPORTS_DIRECTION", False))
    has_key = bool(env.get("OPENROUTER_API_KEY") or env.get("OPEN_ROUTER_API_KEY")
                   or env.get("TYPESAFE_API_KEY"))
    if choice == "off" or not supported:
        return "off"
    if choice == "jev":
        if not has_key:
            raise RuntimeError("NARRATION_DIRECTOR=jev needs OPENROUTER_API_KEY in .env.")
        return "on"
    return "on" if has_key else "off"


async def direct_blocks(texts, language, jev, book="", chapter=""):
    """One Direction per paragraph, decided concurrently.

    Each paragraph is judged with CONTEXT_BLOCKS neighbours on each side, so
    the calls are independent and a novel's ~4,500 paragraphs take minutes,
    not the better part of an hour one after another. A failed call costs that
    paragraph its direction, never its narration.
    """
    async def one(i):
        if len(texts[i].split()) < MIN_DIRECTED_WORDS:
            return PLAIN
        lo, hi = max(0, i - CONTEXT_BLOCKS), i + 1 + CONTEXT_BLOCKS
        try:
            ans = await jev.decide(
                state={"line": texts[i][:1200],
                       "before": " ".join(texts[lo:i])[-1200:],
                       "after": " ".join(texts[i + 1:hi])[:1200],
                       "book": book, "chapter": chapter,
                       "language": language or "en"},
                questions=QUESTIONS)
            return direction_for(settle(ans), language, float(ans["breath_before"]["noul"]))
        except Exception:
            return PLAIN
    return list(await asyncio.gather(*(one(i) for i in range(len(texts)))))


# A chunk of a few words is where the narrator fails: on Winning, 9 of the 63
# chunks of 8 words or fewer did not say their text (a skipped word, "Zero." heard
# as "See you", "Welcome." as noise) against 15 of the other 795, and the 3-word
# `"Job not finished."` came out "job not fail". A short plain line therefore
# joins the paragraph before it instead of standing alone.
SHORT_RUN_WORDS = 8
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6", "header")


def group_by_direction(blocks, directions):
    """[(Direction, [blocks])]: consecutive paragraphs with the same direction.

    A request carries one instruction, so a chunk must never span two
    deliveries; the packer then works inside each run. A run of a few plain
    words (and no heading) is folded into the run before it, taking its
    delivery: the director's own rule is that a scene's mood carries over.
    """
    runs = []
    for block, d in zip(blocks, directions):
        if runs and runs[-1][0] == d:
            runs[-1][1].append(block)
        else:
            runs.append((d, [block]))

    def short_plain(run):
        d, members = run
        return (d.plain and sum(len(m[1].split()) for m in members) <= SHORT_RUN_WORDS
                and not any(getattr(m[0], "tag", "") in _HEADINGS for m in members))

    merged = []
    for run in runs:
        if merged and short_plain(run):
            merged[-1][1].extend(run[1])
        elif merged and merged[-1][0] == run[0]:
            merged[-1][1].extend(run[1])        # a fold can leave two runs of one delivery side by side
        else:
            merged.append((run[0], list(run[1])))
    return merged


def summary(directions):
    counts = {}
    for d in directions:
        counts[d.delivery] = counts.get(d.delivery, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
