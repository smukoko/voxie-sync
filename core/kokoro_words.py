"""Words Kokoro's lexicon lacks and eSpeak NG gets wrong, said like a word it knows.

"By Wilde", the first words of The Canterville Ghost, was heard "By Wilda"
(2026-10-12): Kokoro's lexicon has no "Wilde", and its eSpeak NG fallback says
wˈIldə. server/cpu_tts.py teaches the narrator these at load; the text and the
sync map are untouched. The words are part of Kokoro's cache identity
(core/tts_providers.KokoroProvider), so a change here re-renders exactly the
audio it affects instead of serving the old pronunciation from the cache.
"""
import hashlib
import json

SAY_LIKE = {"Wilde": "wild"}


def teach(g2p):
    """Add SAY_LIKE to an English G2P's lexicon, borrowing the known word's phonemes."""
    golds = getattr(getattr(g2p, "lexicon", None), "golds", None)
    if golds is None:
        return
    for word, like in SAY_LIKE.items():
        if like in golds:
            golds.setdefault(word, golds[like])


def fingerprint():
    """A short hash of SAY_LIKE, for the cache key."""
    return hashlib.sha256(json.dumps(SAY_LIKE, sort_keys=True).encode()).hexdigest()[:10]
