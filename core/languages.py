"""The language Voxie narrates: English, and only English (user, 2026-10-07).

French and Dutch were tried and never reached the quality English has; every
other language is untested. So a book in another language still reaches the
Kobo and the catalogue, as an ebook, and is not narrated: the engine says so
and stops before anything is billed. Other languages are a later milestone
(docs/grants/NLNET_RESTACK.md); their tables and reading rules are in git
history up to 2026-10-07.

MFA publishes each language on its own release cadence, and the dictionary and
the acoustic model are pinned to the *same* version: mixing versions mixes
phone sets, which MFA accepts and then aligns badly, with no error.
"""

MFA_RELEASES = "https://github.com/MontrealCorpusTools/mfa-models/releases/download"

LANGUAGE = DEFAULT_LANGUAGE = "en"

# (model slug, dictionary version, acoustic version)
MFA_MODEL = ("english_mfa", "v3.1.0", "v3.1.0")

# Kokoro's preset voice for the free CPU route (server/cpu_tts.py).
KOKORO_VOICE = "af_heart"

NOT_ENGLISH = ("Voxie narrates English only for now: no other language has been tested. "
               "'{title}' is in '{language}', so it goes to your Kobo as an ebook, without "
               "an audiobook. If it is English and the book says otherwise, run the engine "
               "with --language en.")


def normalise(code):
    """'en-GB' / 'EN' / 'eng' -> 'en'; 'fr-FR' -> 'fr'. None for anything unrecognisable."""
    if not code:
        return None
    code = str(code).strip().lower().replace("_", "-")
    primary = code.split("-")[0]
    # A few EPUBs carry ISO 639-2 three-letter codes.
    three = {"eng": "en", "fra": "fr", "fre": "fr", "spa": "es", "esl": "es",
             "nld": "nl", "dut": "nl", "deu": "de", "ger": "de", "ita": "it"}
    if primary in three:
        return three[primary]
    return primary if len(primary) == 2 else None


def is_english(code):
    return normalise(code) == LANGUAGE


def mfa_files():
    """(dict filename, acoustic filename) as they land in mfa_workspace/."""
    slug = MFA_MODEL[0]
    return f"{slug}.dict", f"{slug}.zip"


def mfa_downloads():
    """{filename: url} for the dictionary and acoustic model."""
    slug, dict_ver, acoustic_ver = MFA_MODEL
    return {
        f"{slug}.dict": f"{MFA_RELEASES}/dictionary-{slug}-{dict_ver}/{slug}.dict",
        f"{slug}.zip": f"{MFA_RELEASES}/acoustic-{slug}-{acoustic_ver}/{slug}.zip",
    }


# Common words, to tell English prose from the languages people are most likely
# to drop, when the EPUB declares no language. Detection only: none of these
# languages is narrated. A full detector would be a dependency for one question.
_MARKERS = {
    "en": {"the", "and", "of", "to", "that", "with", "was", "for", "his", "her"},
    "fr": {"le", "la", "les", "des", "une", "est", "dans", "pour", "qui", "pas"},
    "es": {"el", "la", "los", "las", "una", "que", "por", "con", "para", "más"},
    "nl": {"de", "het", "een", "van", "en", "is", "dat", "niet", "op", "voor"},
    "de": {"der", "die", "und", "ist", "nicht", "ein", "eine", "zu", "mit", "sich"},
}


def detect(text, sample=20000):
    """Best-effort language guess from prose, when the EPUB declares nothing."""
    words = [w.strip(".,;:!?\"'()[]«»¿¡„“”").lower() for w in text[:sample].split()]
    if not words:
        return DEFAULT_LANGUAGE
    scores = {code: sum(w in markers for w in words) for code, markers in _MARKERS.items()}
    best = max(scores, key=scores.get)
    # Dutch and English share "de"/"is"/"op"; require a real margin before
    # calling a book not English rather than guessing on noise.
    if best == DEFAULT_LANGUAGE or scores[best] < 5:
        return DEFAULT_LANGUAGE
    return best if scores[best] > 1.5 * scores[DEFAULT_LANGUAGE] else DEFAULT_LANGUAGE
