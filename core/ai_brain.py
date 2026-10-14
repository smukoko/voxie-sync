import json
import os
import re
import asyncio
import numpy as np
from types import SimpleNamespace
from dotenv import load_dotenv

# torch, xgboost and joblib are imported inside _brain() rather than here. They
# cost ~7s to import between them, and the ebook-only path never touches the
# classifier at all -- there is no reason for `import core.ai_brain` to pull in
# a deep learning stack.

load_dotenv()
# --- CONFIGURATION & THRESHOLDS ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Groq retired the Llama models; the old default returned 404 and every
# fallback silently answered KEEP, so the safety net never once fired.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
LOWER_BOUND = float(os.getenv("ML_LOWER_BOUND", 0.05))
UPPER_BOUND = float(os.getenv("ML_UPPER_BOUND", 0.96))

# Resolved against this file, not the working directory. As relative paths these
# tied the whole engine to being launched from the repo root -- running it from
# anywhere else died at import with FileNotFoundError on the scaler.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_JSON = os.path.join(_REPO_ROOT, "safe_models", "audiobook_xgb_master.json")
SCALER_PKL = os.path.join(_REPO_ROOT, "safe_models", "audiobook_word_scaler.pkl")

_BRAIN = None

EMBEDDER = "paraphrase-multilingual-mpnet-base-v2"


def load_embedder(name=EMBEDDER, loader=None):
    """The sentence-transformer, from the local cache when it is there.

    Loaded by name, it asks the Hugging Face hub about its files on every run,
    even when they are all cached. On 2026-10-07 that question failed inside
    the hub's own HTTP client ("the client has been closed"; unauthenticated
    requests are rate-limited) and a whole book was lost with it. The network
    is needed once, to fetch the model; after that a run does not depend on it.
    """
    if loader is None:
        from sentence_transformers import SentenceTransformer as loader
    try:
        return loader(name, local_files_only=True)
    except OSError:
        # Not cached yet: the one time the model is downloaded.
        return loader(name)


def _brain():
    """Load the classifier on first use, once.

    This used to run at import: a Groq client, a sentence-transformer, a scaler
    and an XGBoost model, all constructed merely because something imported this
    module. That made `import` slow, made it fail outside the repo root, and made
    the ebook-only path (`--no-audio`) pay for a model it never consults.
    """
    global _BRAIN
    if _BRAIN is None:
        print("🧠 Booting AI Brain (XGBoost + Groq)...")
        import joblib
        import xgboost as xgb
        from groq import Groq

        clf = xgb.XGBClassifier()
        clf.load_model(MODEL_JSON)
        _BRAIN = SimpleNamespace(
            # Timeout and retries on the CLIENT, so every call site inherits
            # them. This is hardening, not a fix for an observed hang -- it was
            # added while chasing one that turned out to be block-buffered
            # stdout, not a stalled request. It earns its place anyway: this is
            # an unbounded call to a third party on the path of every book, the
            # cartographer runs before any pod exists, and a stall here leaves
            # mac_ghost waiting forever with nothing printed to say why.
            #
            # Failure is already non-fatal: ask_groq_fallback catches and the
            # caller keeps the XGBoost verdict, so a timeout degrades the filter
            # rather than the run.
            # No key is the free route, not an error: the local classifier
            # decides alone and its grey zone is kept. Constructing the client
            # without a key raised, which took the local classifier down too.
            client=Groq(api_key=GROQ_API_KEY, timeout=45.0, max_retries=2) if GROQ_API_KEY else None,
            embedder=load_embedder(),
            scaler=joblib.load(SCALER_PKL),
            clf=clf,
        )
    return _BRAIN

async def ask_groq_fallback(snippet, rho):
    """The safety net. Asks Llama 3 to classify text if XGBoost is unsure."""

    # 👇 THE UPGRADED AUDIOBOOK PRODUCER PROMPT 👇
    prompt = f"""You are a strict audiobook producer filtering EPUB files.
Apply the "Audiobook Test": Should a human narrator read this exact text out loud to a listener?

CLASSIFICATION RULES:
- "KEEP": Actual reading material. Chapters, prologues, introductions, epilogues, or narrative prose. It contains full sentences, paragraphs, and a natural reading flow.
- "JUNK": Structural front/back matter that would sound terrible if read aloud. This explicitly includes: Indices (alphabetical lists of terms, often with page numbers like 'ability, 56'), Tables of Contents, Bibliographies, Copyright pages, Title pages, and References. Look for fragmented lines, lists, legal jargon, or disconnected terms.

Analyze the text and output ONLY valid JSON in this exact format: {{"llm_classification": "KEEP"}} or {{"llm_classification": "JUNK"}}

Text snippet:
{snippet[:1500]}"""

    if _brain().client is None:
        # Keep, as on any failure; said once per section so the log shows the
        # filter ran without its second opinion.
        print("   ↪︎ No GROQ_API_KEY: grey zone kept (local classifier only).")
        return "KEEP"
    try:
        loop = asyncio.get_event_loop()
        def fetch_groq():
            return _brain().client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0, # Dropped to 0.0 for maximum strictness
                # The current Groq models think before answering, and that
                # reasoning is billed against max_tokens. The old value of 20
                # truncated every reply to an empty string.
                max_tokens=512,
                reasoning_effort="low",
            )
        completion = await loop.run_in_executor(None, fetch_groq)
        response_text = (completion.choices[0].message.content or "").strip()
        match = re.search(r'"llm_classification"\s*:\s*"([^"]+)"', response_text)
        if match: return match.group(1).upper()
        # Falling back to KEEP is deliberate -- narrating a stray page is far
        # better than silently deleting a chapter -- but it must be loud, or a
        # dead model looks exactly like a working one.
        print(f"   ‼️  Groq returned no verdict ({GROQ_MODEL}): {response_text[:80]!r}. Defaulting to KEEP.")
        return "KEEP"
    except Exception as e:
        print(f"   ‼️  Groq call FAILED ({GROQ_MODEL}): {e}")
        print("   ‼️  The junk filter has no safety net this run — check GROQ_MODEL in .env.")
        return "KEEP"

def windows(text):
    """Where a document is read: one 1,500-character window, or three for a long one.

    One window from near the start judged the whole document, and a chapter can
    open on the previous chapter's footnotes or on a table: Gutenberg's Worst
    Journey (h-0, h-1, h-5, h-6, ~20,000 words each) and Standard Ebooks'
    chapter 7 (29,340 words) were cut as JUNK in 3 runs of 3 (2026-10-06). A
    long document is read at its start, middle and end.
    """
    n = len(text)
    if n <= 1500:
        return [text[:1500]]
    if n <= 4500:
        # Skip the first 500 chars (chapter title fluff) and grab the next 1500.
        return [text[500:2000]]
    return [text[500:2000], text[n // 2 - 750:n // 2 + 750], text[n - 1600:n - 100]]


def local_score(ps):
    """The classifier's score for a document from its windows' scores.

    The lower of the first window (what it has always judged, so nothing that
    was kept moves) and the median of the three: one window of notes cannot
    outvote two of prose, and one window of prose cannot rescue a document of
    notes. Measured on 1,932 documents (2026-10-06): the only verdicts that
    move are grey -> keep, all on prose; endnotes, reading guides and
    appendices of notes stay grey or junk.
    """
    return min(ps[0], sorted(ps)[len(ps) // 2])


def majority_junk(verdicts):
    """A document is cut when most of its windows read as junk.

    Asked one window at a time, Groq called only the first window of each of
    those chapters JUNK (a footnote list, a table of sledge weights) and every
    window of real notes JUNK: 3 runs of 3 on each, 2026-10-06.
    """
    return "JUNK" if sum(v == "JUNK" for v in verdicts) * 2 > len(verdicts) else "KEEP"


async def evaluate_text_block(text, word_count, position_ratio, file_identifier=""):
    parts = windows(text)
    brain = _brain()
    wcs_scaled = brain.scaler.transform(np.array([[word_count]]))
    embs = np.asarray(brain.embedder.encode(parts, show_progress_bar=False))
    ps = [brain.clf.predict_proba(np.hstack([e[None, :], np.array([[position_ratio]]), wcs_scaled]))[0, 1]
          for e in embs]
    p_junk = local_score(ps)

    if p_junk <= LOWER_BOUND:
        return "KEEP"
    elif p_junk >= UPPER_BOUND:
        return "JUNK"
    else:
        print(f"   🤔 AI Gray Zone ({p_junk:.2f}) on {file_identifier}. Asking Groq...")
        verdicts = []
        for part in parts:
            verdicts.append(await ask_groq_fallback(part, position_ratio))
            await asyncio.sleep(0.5)
        verdict = majority_junk(verdicts)
        if len(parts) > 1 and "JUNK" in verdicts:
            print(f"   🧭 {file_identifier}: Groq per window {'/'.join(verdicts)} -> {verdict}")
        return verdict


async def classify_document_map(documents):
    """Classify every document of a book in one call, with the whole map visible.

    evaluate_text_block judges each document alone, which throws away the most
    useful evidence there is: where a section sits relative to the rest of the
    book. A 200-word block of legal prose is ambiguous on its own and obvious
    when you can see it is the last section of twelve.

    One request also costs less than one per document -- a book is 10-60 spine
    files, so title plus a short snippet each is a few tens of thousands of
    tokens, well under a cent. Cost is not a reason to look at only part of a
    book.

    `documents` is a list of dicts with index, title, words, position and
    snippet. Returns {index: "KEEP"|"JUNK"}. Anything the model does not answer
    for is left out, so the caller keeps its own verdict for that document.
    """
    if not documents or _brain().client is None:
        return {}

    listing = "\n".join(
        f'[{d["index"]}] title={d["title"]!r} words={d["words"]} '
        f'position={d["position"]:.2f}\n    {d["snippet"][:300]}'
        for d in documents
    )

    prompt = f"""You are preparing an EPUB for audiobook narration. Below is every
section of one book, in reading order, with its title, word count, relative
position (0.0 = start, 1.0 = end) and opening text.

For each section decide whether a narrator should read it aloud.

KEEP: the actual work -- chapters, prologue, epilogue, introduction, essays,
      narrative or argumentative prose the reader came for.
JUNK: apparatus nobody listens to -- title pages, copyright and licence pages,
      tables of contents, indexes, bibliographies, endnote lists, colophons,
      publisher advertisements, donation appeals, transcriber's notes.

Judge each section in the context of the others. Boilerplate is often
well-written prose, so do not decide on writing quality; decide on what the
section IS. A long legal or fundraising passage at the very end of a book is
almost always JUNK even though it reads fluently.

Sections:
{listing}

Reply with ONLY a JSON array, one object per section, no other text:
[{{"index": 0, "verdict": "KEEP"}}, {{"index": 1, "verdict": "JUNK"}}]"""

    try:
        loop = asyncio.get_event_loop()

        def fetch():
            return _brain().client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=4000,
                reasoning_effort="low",
            )

        completion = await loop.run_in_executor(None, fetch)
        raw = (completion.choices[0].message.content or "").strip()
        match = re.search(r"\[.*\]", raw, re.S)
        if not match:
            print("   ⚠️ Document map: no JSON in the reply; falling back per document.")
            return {}
        verdicts = {}
        for entry in json.loads(match.group(0)):
            verdict = str(entry.get("verdict", "")).upper()
            if verdict in ("KEEP", "JUNK"):
                verdicts[int(entry["index"])] = verdict
        return verdicts
    except Exception as e:
        # Never fail the book over the filter. The caller keeps its own verdicts.
        print(f"   ⚠️ Document map classification failed ({e}); falling back per document.")
        return {}
