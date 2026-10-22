"""The text a person reads to make their narrator, and how to read it.

A narrator copies how its clip is read, not only whose voice it is. On
2026-10-22 a first install's clip left 0.34 s between two sentences, and the
narrator then started every sentence that quickly after a full stop, through a
whole book. Typing out "exactly what is said" from a clip of one's own choosing
also asks a lot (the clip, its start and end, every word right). So the setup
offers a text to read, written to be easy to read well:

  - three short sentences of seven or eight words, about ten seconds read calmly
    (the model's best window is 8 to 15 s, thirteen if the reader is slow);
  - only everyday words of one or two syllables, no names, numbers or hard
    letters (nothing to stumble on, so nothing to do again);
  - two full stops to pause at, which is what the narrator copies.

The checker (libra_cli/clip_check.py) holds the reader to it: it hears the
clip, finds the gap after each full stop, and says to read it again when the
next sentence started in under half a second.
"""
SENTENCES = (
    "The sun rose slowly over the hills.",
    "Birds began to sing in the trees.",
    "It was going to be a good day.",
)
TEXT = " ".join(SENTENCES)

HOW = (
    "Read it slowly, as if you were reading to a friend.",
    "After each full stop, stop for a full second: count “one” in your head.",
    "Sit somewhere quiet, with your mouth a hand's width from the microphone.",
    "Only your voice: no laughing, sighing or music, which the narrator would copy into every sentence.",
)


def as_dict():
    return {"text": TEXT, "sentences": list(SENTENCES), "how": list(HOW)}
