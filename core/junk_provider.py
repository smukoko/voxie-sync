"""The seam between the engine and whatever decides a section is not the book.

The engine asks one question -- "is this the author's work, or the publisher's
furniture?" -- and has always asked it of one implementation. That answer has
been wrong in expensive ways twice: Groq retired the model behind the fallback
and every call 404'd while `ask_groq_fallback` answered KEEP, and the whole
Project Gutenberg licence was narrated as 46% of a finished audiobook. Both
were failures of a *provider*, not of the question, and neither was visible
from the outside because a wrong verdict looks exactly like a right one until
somebody listens to the book.

So the verdict is now a value with its own provenance -- who decided, how
confident, and on what -- and the implementations sit behind one interface, the
way TTS already does in core/tts_providers.py.

Three of them:

  ai_brain   XGBoost + sentence-transformers, with a Groq fallback in the grey
             zone. The incumbent. Returns no calibrated probability, so its
             confidence is None rather than invented.
  jev        TypeSafe's System One model. Typed decisions only -- it cannot
             emit a string, so it cannot rewrite the author's text even by
             accident, and an unparseable reply is not a failure mode it has.
  shadow     Runs two providers on the same input, returns the primary's
             verdict, and writes every disagreement to a file. Costs one extra
             call per document and changes no behaviour, which is what makes it
             safe to leave on for a book before switching.

Chosen with JUNK_PROVIDER (auto | ai_brain | jev | shadow). Nothing here is imported
at module load: ai_brain pulls in a deep learning stack and jev needs a network
client, and the --no-audio path consults neither.
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict

KEEP = "KEEP"
JUNK = "JUNK"


@dataclass
class Verdict:
    """What a provider decided, and enough to argue with it later."""
    verdict: str
    provider: str
    confidence: float | None = None          # None where a provider has none
    probabilities: dict = field(default_factory=dict)
    detail: str = ""
    elapsed_ms: float | None = None

    def __str__(self):
        c = f" p={self.confidence:.2f}" if self.confidence is not None else ""
        return f"{self.verdict}({self.provider}{c})"


class JunkProvider:
    """Decide whether a stretch of text is the author's work.

    One method, deliberately. `identifier` is for logs only -- a provider that
    changed its answer based on a filename would be reading the wrong evidence.
    """
    name = "base"

    async def classify(self, text, word_count, position_ratio, identifier=""):
        raise NotImplementedError

    async def aclose(self):
        return None


class AiBrainProvider(JunkProvider):
    """The incumbent: XGBoost, with Groq consulted only in the grey zone."""
    name = "ai_brain"

    async def classify(self, text, word_count, position_ratio, identifier=""):
        from core.ai_brain import evaluate_text_block   # deep learning stack
        started = time.perf_counter()
        verdict = await evaluate_text_block(text, word_count, position_ratio, identifier)
        return Verdict(verdict=verdict, provider=self.name,
                       elapsed_ms=(time.perf_counter() - started) * 1000)


# How sure JEV must be before a section is cut. Deliberately high and not
# symmetric with KEEP: narrating a page of copyright costs a listener ninety
# seconds, and dropping a chapter costs them the book. When in doubt, keep.
JEV_JUNK_THRESHOLD = float(os.getenv("JEV_JUNK_THRESHOLD", "0.85"))

# What the model is actually asked. Atomic questions composed in code is
# TypeSafe's own guidance, so this asks two narrow things rather than one broad
# one: whether the text is the author's, and what kind of section it is. They
# are evaluated in parallel against the same state and cost one call, and the
# second is kept as evidence rather than used as a second opinion -- a verdict
# reached two ways that disagree is worse than one reached once.
# Redeeming Love's shadow run (2026-09-24) found the gap in the first version
# of this list: its back matter carries a reading-group guide -- "How was
# Sarah/Angel rejected? Describe a time when you were rejected" -- 1,850 words
# JEV judged KEEP at p=0.18 while correctly naming it back_matter, because the
# definition it was given did not mention study questions. ai_brain was right.
# Screwtape's read-through (2026-09-25) found the next two: "Amazon.com Review"
# and "From Library Journal" jacket copy at p=0.74, and a scan's "About this
# e-text / Version 1.0" sign-off -- neither was named, so neither was caught.
_JUNK_INSTRUCTIONS = (
    "This text is the publisher's or distributor's furniture rather than the "
    "author's own work -- a copyright page, title page, table of contents, "
    "index, acknowledgements, a licence, an advertisement, a catalogue of "
    "other books, an excerpt from a different book, a reading-group or "
    "discussion guide with questions for readers, reviews or praise of the book "
    "(jacket copy, Amazon or magazine reviews), or the credits and version notes "
    "of whoever scanned or converted it. Narrative prose, dialogue, "
    "poetry, a preface or an introduction written for this book are the "
    "author's work and are NOT this."
)

_KIND_CRITERIA = {
    "narrative": "The author's actual work: prose, dialogue, poetry, an essay",
    "front_matter": "Title page, copyright page, dedication, epigraph",
    "contents": "A table of contents, a list of chapters or an index",
    "back_matter": "Acknowledgements, about the author, an afterword, a discussion guide",
    "legal": "A licence, terms of use, or distribution restrictions",
    "advertising": "Promotion of other books or an excerpt from another title",
}


# Two ways to reach the same model, with the same request and response shape:
# TypeSafe's own API, or OpenRouter's Decisions endpoint. OpenRouter is the
# default because it needs no separate account -- the key already exists for
# training_lab -- and it bills to credit that is already there.
_TRANSPORTS = {
    "openrouter": {
        "url": "https://openrouter.ai/api/alpha/decisions",
        # Pinned, not "-latest". A floating model is how Groq broke the junk
        # filter once already: the provider changed what the name meant and
        # nothing on this side noticed. The served model is logged per call.
        "model": "typesafe/jev-1.13",
        "keys": ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"),
    },
    "typesafe": {
        "url": "https://api.typesafe.ai/v1/systemone",
        "model": "jev-latest",
        "keys": ("TYPESAFE_API_KEY",),
    },
}

# Measured on OpenRouter, 2026-09-24: 32 concurrent callers gave 40.5 req/s
# with no errors and a p95 of 497ms; 64 produced HTTP 520s and throughput
# collapsed to 3 req/s behind the slow tail. 24 sits safely under the knee.
JEV_MAX_CONCURRENCY = int(os.getenv("JEV_MAX_CONCURRENCY", "24"))
JEV_REQUEST_TIMEOUT = float(os.getenv("JEV_REQUEST_TIMEOUT", "20"))
JEV_ATTEMPTS = int(os.getenv("JEV_ATTEMPTS", "4"))


class JevProvider(JunkProvider):
    """TypeSafe System One. Typed decisions, calibrated, no string output.

    Talks HTTP directly rather than through an SDK: both transports take the
    identical state/questions body, and the engine already speaks httpx.
    """
    name = "jev"

    def __init__(self, transport=None, model=None, threshold=None):
        self.transport = (transport or os.getenv("JEV_TRANSPORT", "openrouter")).strip().lower()
        if self.transport not in _TRANSPORTS:
            raise ValueError(f"Unknown JEV_TRANSPORT {self.transport!r}. "
                             f"Known: {', '.join(sorted(_TRANSPORTS))}")
        spec = _TRANSPORTS[self.transport]
        self.url = spec["url"]
        self.model = model or os.getenv("JEV_MODEL") or spec["model"]
        self._key_names = spec["keys"]
        self.threshold = JEV_JUNK_THRESHOLD if threshold is None else threshold
        self._client = None
        self._sem = None
        self.served_models = set()
        self.cost = 0.0
        self.calls = 0

    def _api_key(self):
        for name in self._key_names:
            value = os.getenv(name)
            if value:
                return value
        raise RuntimeError(f"JUNK_PROVIDER=jev over {self.transport} needs "
                           f"{' or '.join(self._key_names)} in .env")

    def _get_client(self):
        if self._client is None:
            key = self._api_key()
            import httpx
            self._client = httpx.AsyncClient(
                timeout=JEV_REQUEST_TIMEOUT,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                limits=httpx.Limits(max_connections=JEV_MAX_CONCURRENCY),
            )
            self._sem = asyncio.Semaphore(JEV_MAX_CONCURRENCY)
        return self._client

    async def decide(self, state, questions):
        """One Decisions call: state and questions in, the answers dict out.

        Retries only what is worth retrying -- rate limits, 5xx and timeouts,
        which are properties of a moment rather than of the request. A 4xx is
        a mistake in the request or the key, and retrying it only hides that.
        """
        import httpx
        client = self._get_client()
        body = {"model": self.model, "state": state, "questions": questions}
        last = None
        for attempt in range(1, JEV_ATTEMPTS + 1):
            async with self._sem:
                try:
                    r = await client.post(self.url, json=body)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    r = None
            if r is not None:
                if r.status_code == 200:
                    data = r.json()
                    self.calls += 1
                    self.cost += float((data.get("usage") or {}).get("cost") or 0.0)
                    if data.get("model"):
                        self.served_models.add(data["model"])
                    return data["answers"]
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code != 429 and r.status_code < 500:
                    raise RuntimeError(f"JEV rejected the request ({last})")
            if attempt < JEV_ATTEMPTS:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
        raise RuntimeError(f"JEV failed after {JEV_ATTEMPTS} attempts ({last})")

    async def classify(self, text, word_count, position_ratio, identifier=""):
        # The same window ai_brain uses, so a shadow run compares two verdicts
        # on one input rather than on two different readings of a document.
        snippet = text[500:2000] if len(text) > 1500 else text[:1500]

        started = time.perf_counter()
        answers = await self.decide(
            state={
                "text": snippet,
                "word_count": word_count,
                # Where this sits in the book, 0.0 at the front and 1.0 at the
                # back. A block of legal prose is ambiguous alone and obvious
                # as the last section of twelve.
                "position_in_book": round(position_ratio, 3),
            },
            questions=_document_questions(),
        )
        elapsed = (time.perf_counter() - started) * 1000
        return self._verdict(answers, elapsed)

    async def classify_block(self, text, before="", after="", tag="p",
                             position_in_doc=0.0, position_in_book=0.0):
        """One block, judged with its neighbours as context. See core/block_filter.py."""
        started = time.perf_counter()
        answers = await self.decide(
            state={
                "block": text[:1200],
                "tag": tag,
                "before": before,
                "after": after,
                "position_in_chapter": round(position_in_doc, 3),
                "position_in_book": round(position_in_book, 3),
            },
            questions=_block_questions(),
        )
        return self._verdict(answers, (time.perf_counter() - started) * 1000)

    def _verdict(self, answers, elapsed):
        p_junk = float(answers["is_not_the_author"]["noul"])
        kind = answers.get("section_kind") or {}
        # Composed in code, not in the prompt: the threshold is asymmetric and
        # lives here where it can be read and changed.
        verdict = JUNK if p_junk >= self.threshold else KEEP
        return Verdict(
            verdict=verdict, provider=self.name, confidence=p_junk,
            probabilities=dict(kind.get("probabilities") or {}),
            detail=f"kind={kind.get('choice', '?')}", elapsed_ms=elapsed,
        )

    def summary(self):
        served = ", ".join(sorted(self.served_models)) or "nothing yet"
        return f"jev: {self.calls} calls, ${self.cost:.4f}, served by {served}"

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _block_questions():
    """The same two questions, pointed at one block rather than a document.

    `before` and `after` are there so a paragraph of licence is judged as part
    of a licence, and a lone formal-sounding line as part of the story around it.
    """
    return {
        "is_not_the_author": {
            "type": "noul",
            "instructions": ("Is `block` the publisher's furniture rather than the author's own work? "
                             "`before` and `after` are the neighbouring blocks, for context only."),
            "criteria": {
                "true": _JUNK_INSTRUCTIONS,
                "false": "The author's own work: narrative prose, dialogue, a chapter heading, an "
                         "epigraph, a letter or verse the author includes, or a preface written for this book.",
            },
        },
        "section_kind": {
            "type": "choice",
            "instructions": "What kind of text is `block`?",
            "criteria": _KIND_CRITERIA,
        },
    }


def _document_questions():
    """The two atomic questions asked of a stretch of text.

    Instructions name the state field in backticks, which is how the Decisions
    API expects a question to point at the part of the state it is about.
    """
    return {
        "is_not_the_author": {
            "type": "noul",
            "instructions": "Is `text` the publisher's furniture rather than the author's own work?",
            "criteria": {
                "true": _JUNK_INSTRUCTIONS,
                "false": "The author's own work: narrative prose, dialogue, poetry, an essay, "
                         "or a preface or introduction written for this book.",
            },
        },
        "section_kind": {
            "type": "choice",
            "instructions": "What kind of section is `text`?",
            "criteria": _KIND_CRITERIA,
        },
    }


class ShadowProvider(JunkProvider):
    """Run two providers, trust one, and write down every time they differ.

    The point is to make a switch decidable on evidence from a real book rather
    than on a benchmark. The primary's verdict is returned unchanged, so
    turning this on cannot alter a single audiobook -- the only costs are one
    extra call per document and a file of disagreements.

    A shadow that raises must never take the book down with it: the whole
    reason it is running is that nobody trusts it yet.
    """
    name = "shadow"

    def __init__(self, primary, shadow, log_path=None):
        self.primary = primary
        self.shadow = shadow
        self.log_path = log_path or os.getenv(
            "JUNK_SHADOW_LOG", os.path.expanduser("~/librasync_junk_shadow.jsonl"))
        self.agreements = 0
        self.disagreements = []

    @property
    def display(self):
        return f"{self.primary.name} (shadowed by {self.shadow.name})"

    async def classify(self, text, word_count, position_ratio, identifier=""):
        primary_task = asyncio.create_task(
            self.primary.classify(text, word_count, position_ratio, identifier))
        shadow_task = asyncio.create_task(
            self.shadow.classify(text, word_count, position_ratio, identifier))

        primary = await primary_task
        try:
            shadow = await shadow_task
        except Exception as exc:
            print(f"   👥 shadow {self.shadow.name} failed on {identifier}: "
                  f"{type(exc).__name__}: {exc}")
            return primary

        if shadow.verdict == primary.verdict:
            self.agreements += 1
        else:
            row = {
                "identifier": identifier,
                "words": word_count,
                "position": round(position_ratio, 3),
                "primary": asdict(primary),
                "shadow": asdict(shadow),
                "snippet": " ".join(text.split())[:300],
            }
            self.disagreements.append(row)
            print(f"   👥 DISAGREE on {identifier}: "
                  f"{self.primary.name}={primary.verdict} vs {self.shadow.name}={shadow}")
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            except OSError as exc:
                print(f"   ⚠️  could not write the shadow log: {exc}")

        return primary

    def summary(self):
        total = self.agreements + len(self.disagreements)
        if not total:
            return "shadow: nothing classified"
        pct = 100 * self.agreements / total
        line = (f"shadow: {self.primary.name} vs {self.shadow.name} agreed on "
                f"{self.agreements}/{total} documents ({pct:.1f}%); "
                f"{len(self.disagreements)} disagreements -> {self.log_path}")
        if hasattr(self.shadow, "summary"):
            line += f" [{self.shadow.summary()}]"
        return line

    async def aclose(self):
        await self.primary.aclose()
        await self.shadow.aclose()


class FallbackProvider(JunkProvider):
    """Ask one provider; if it cannot answer, ask another -- loudly.

    Found on 2026-09-25 when the OpenRouter key hit its monthly limit: every
    JEV call answered HTTP 403, and with JUNK_PROVIDER=jev the engine would
    have died at planning on the next dropped book. No GPU is spent by then,
    but the walk-away promise is gone. An outage should cost the better
    verdict, not the book -- the same rule the block filter follows. The
    fallback is built only when first needed, so a healthy JEV never loads
    ai_brain's deep learning stack.
    """
    name = "fallback"

    def __init__(self, primary, fallback_name):
        self.primary = primary
        self.fallback_name = fallback_name
        self.fallback = None
        self.failures = 0

    @property
    def display(self):
        return f"{self.primary.name} (falls back to {self.fallback_name})"

    async def classify(self, text, word_count, position_ratio, identifier=""):
        try:
            return await self.primary.classify(text, word_count, position_ratio, identifier)
        except Exception as exc:
            self.failures += 1
            if self.failures == 1:
                print(f"   ⚠️  {self.primary.name} unavailable ({type(exc).__name__}: {exc}); "
                      f"falling back to {self.fallback_name} for this book")
            if self.fallback is None:
                self.fallback = build(self.fallback_name)
            return await self.fallback.classify(text, word_count, position_ratio, identifier)

    def summary(self):
        line = self.primary.summary() if hasattr(self.primary, "summary") else self.primary.name
        if self.failures:
            line += f"; {self.failures} document(s) decided by {self.fallback_name} instead"
        return line

    async def aclose(self):
        await self.primary.aclose()
        if self.fallback is not None:
            await self.fallback.aclose()


_REGISTRY = {"ai_brain": AiBrainProvider, "jev": JevProvider}


def spend(provider):
    """Dollars a provider, and whatever it wraps, has spent on OpenRouter so far.

    The director and the filters are part of what a book costs: on Twisted
    (2026-10-08) they came to $0.25 against $0.58 of GPU, so a budget that
    counted the GPU alone could pass $1 for a book it planned under it.
    """
    if provider is None:
        return 0.0
    total = float(getattr(provider, "cost", 0.0) or 0.0)
    for inner in ("primary", "shadow", "fallback"):
        total += spend(getattr(provider, inner, None))
    return total


def build(name):
    """One provider by name, with no shadowing."""
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown junk provider {name!r}. "
                         f"Known: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[key]()


def has_openrouter_key(env=None):
    """True when the environment carries an OpenRouter key (either spelling)."""
    env = os.environ if env is None else env
    return any((env.get(n) or "").strip() for n in _TRANSPORTS["openrouter"]["keys"])


def resolved_choice(env=None):
    """JUNK_PROVIDER with "auto" (the default) settled: JEV for someone who pays
    for OpenRouter, the local classifier for someone who does not. A person who
    pays for the director gets the better filter too (user, 2026-10-22: even at
    15 cents more a book); the free route stays what it was."""
    env = os.environ if env is None else env
    choice = (env.get("JUNK_PROVIDER") or "auto").strip().lower()
    if choice == "auto":
        return "jev" if has_openrouter_key(env) else "ai_brain"
    return choice


def get_junk_provider():
    """The provider the environment asks for.

    JEV falls back to ai_brain when it cannot answer (JUNK_FALLBACK=none turns
    that off). JUNK_PROVIDER=shadow needs two more names, and defaults to comparing the
    incumbent against jev in that order -- the incumbent decides, because a
    shadow run must not be able to change a book.
    """
    choice = resolved_choice()
    if choice != "shadow":
        provider = build(choice)
        fallback = os.getenv("JUNK_FALLBACK", "ai_brain" if choice == "jev" else "none").strip().lower()
        if fallback in ("", "none") or fallback == choice:
            return provider
        return FallbackProvider(provider, fallback)
    primary = os.getenv("JUNK_PRIMARY", "ai_brain")
    shadow = os.getenv("JUNK_SHADOW", "jev")
    if primary == shadow:
        raise ValueError("JUNK_PRIMARY and JUNK_SHADOW are both "
                         f"{primary!r}; shadowing a provider with itself proves nothing")
    return ShadowProvider(build(primary), build(shadow))
