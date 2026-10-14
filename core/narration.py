"""The last step before the narrator: one block's text as the model will hear it.

The engine and tools/narration_review.py both call this, so a review of what
will be narrated reads exactly what the engine sends -- the only kind of review
worth doing. Everything upstream (Gutenberg markers, print artifacts, the junk
filter) has already read the book's own text; the rewrites here run last so
they can never move one of those verdicts.

Every rule below was found by reading a whole book's narration line by line
(Screwtape, then Redeeming Love), not by imagining what EPUBs might contain.
"""
import re

from core.parser import normalize_text
from core.structure import normalize_caps, roman_to_int, spell_number

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "header", "title")
TERMINAL = (".", "!", "?", '"', "'", "...", "…", "”", "’", ")", "]")

# A block that is only one of these is half of a phrase the typesetter split
# over two lines: "by" / "C. S. Lewis", "To" / "J. R. R. Tolkien".
CONNECTORS = {"by", "to", "for", "from", "with", "and", "of", "in", "or", "the"}

# Nouns a roman numeral counts after. "Number XVII appears" was sent to the
# narrator as letters; "Letter IX", "Part II", "Act III" are the same case.
# Regnal numbers ("Henry VIII") read as ordinals and are deliberately NOT here:
# they need "the Eighth", which _REGNAL_RE does after a monarch's name.
_COUNT_NOUNS = (r"Number|Letter|Chapter|Part|Book|Volume|Act|Scene|Canto|Section|Psalm|"
                r"Article|Appendix|Stage|Phase|Class|Plate|Figure|Table|World War")
# A numeral of two or more letters (II, IV, XVII) after a counting noun is
# unambiguous. A lone "I" is also the pronoun -- "Book I read last week" -- so
# it counts only when nothing like a verb follows: punctuation, the end, or a
# capital ("Part I, Chapter 2", "Letter I.").
_ROMAN_AFTER_NOUN = re.compile(
    rf"\b({_COUNT_NOUNS})\s+([IVXLCDM]{{2,7}}\b(?!['\u2019]\w)|I(?=\s*(?:[.,;:!?)\u2014]|$|[A-Z0-9])))")


# Scripture citations. Redeeming Love opens every chapter with one -- "Romans
# 7:19", "Matthew 18:21-22", "2 Corinthians 3:2 – 3", "Psalm 30:5B" -- 45 in
# all, and a colon between digits is read as a time or as "colon". Only after
# a real book name, so "Sunday 10:30" and "at 4:30" are never touched.
_BIBLE_BOOKS = (
    "Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|Ruth|Samuel|Kings|"
    "Chronicles|Ezra|Nehemiah|Esther|Job|Psalms?|Proverbs|Ecclesiastes|Song of Solomon|"
    "Song of Songs|Isaiah|Jeremiah|Lamentations|Ezekiel|Daniel|Hosea|Joel|Amos|Obadiah|"
    "Jonah|Micah|Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|Matthew|Mark|Luke|"
    "John|Acts|Romans|Corinthians|Galatians|Ephesians|Philippians|Colossians|"
    "Thessalonians|Timothy|Titus|Philemon|Hebrews|James|Peter|Jude|Revelation")
_CITATION = re.compile(
    rf"\b(?:([123])\s+)?({_BIBLE_BOOKS})\s+(\d{{1,3}}):(\d{{1,3}})[ab]?"
    rf"(?:\s?[-–]\s?(\d{{1,3}})[ab]?)?(?!\d)", re.I)
_ORDINAL_BOOK = {"1": "First", "2": "Second", "3": "Third"}


def say_citation(m):
    """'2 Corinthians 3:2 – 3' -> 'Second Corinthians 3, verses 2 to 3'."""
    number, book, chapter, verse, last = m.groups()
    book = f"{_ORDINAL_BOOK[number]} {book}" if number else book
    if last:
        return f"{book} {chapter}, verses {verse} to {last}"
    return f"{book} {chapter}, verse {verse}"


# --- What Kokoro read wrong (tested 2026-10-03 from its own phonemes) --------
# Each rule fires only where the reading cannot be anything else: a title
# before a capitalised name, a month before a day, a page before a number.

# Ranks and titles before a name. "Lt. Col. Moran" was said "L T, col Moran".
_TITLES = {"Lt": "Lieutenant", "Lieut": "Lieutenant", "Col": "Colonel", "Capt": "Captain", "Gen": "General",
           "Sgt": "Sergeant", "Maj": "Major", "Cpl": "Corporal", "Adm": "Admiral",
           "Prof": "Professor", "Rev": "Reverend", "Gov": "Governor", "Sen": "Senator"}
_TITLE_RE = re.compile(rf"\b({'|'.join(_TITLES)})\.\s+(?=[A-Z])")

# "the Hon. Thomas Horton" was said "the Hun" (The Canterville Ghost,
# 2026-10-12). Before a name it is Honourable; before an office ("Hon.
# Secretary", "Hon. Treasurer") it is Honorary, and those stay as printed.
_HON_RE = re.compile(r"\b(?:(Rt)\.\s+)?Hon\.\s+(?=[A-Z])(?!(?:Sec|Secretary|Treasurer|President|Vice|"
                     r"Chairman|Fellow|Editor|Director|Curator|Auditor|Solicitor|Physician|Surgeon)\b)")

# Old American ordinals: "West 53d Street" was said "fifty-three D". Only
# before a word that makes the number an ordinal: in a British book "3d" is
# threepence ("3d. a pound").
_OLD_ORDINAL_RE = re.compile(r"\b(\d*[23])d\b(?=\s+(?:Street|St\.|Avenue|Ave\.|Regiment|Division|Brigade|"
                             r"Corps|Battalion|Infantry|Cavalry|Artillery|Army|Congress|Lieutenant|Precinct|"
                             r"Ward|Floor|Class|Edition|Session|Battery|Company|Squadron|Fleet)\b)")


def _say_old_ordinal(m):
    n = m.group(1)
    return n + ("th" if n[-2:-1] == "1" else "nd" if n[-1] == "2" else "rd")


# A month before a day: "Jan. 5, 1901" was said "Jan five".
_MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
           "Jun": "June", "Jul": "July", "Aug": "August", "Sep": "September",
           "Sept": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
_MONTH_RE = re.compile(rf"\b({'|'.join(_MONTHS)})\.\s+(?=\d)")

# References before a number or a numeral: "p. 42" was "P forty-two", "ch. XII"
# "C H, X I I". Capitalised where a roman numeral can follow, so the counting
# rule below turns "Chapter XII" into "Chapter Twelve".
_REFS = [(r"pp", "pages"), (r"p", "page"), (r"ch", "Chapter"), (r"chap", "Chapter"),
         (r"vols", "volumes"), (r"vol", "Volume"), (r"fig", "Figure"), (r"ca", "circa")]
_REF_RE = re.compile(rf"\b({'|'.join(a for a, _ in _REFS)})\.\s+(?=\d|[IVXLC]+\b)")

# Latin and shorthand that is never anything else. "vs." was said "vees",
# "approx." "a prox".
_SHORTHAND = [(r"\bvs\.", "versus"), (r"\bapprox\.", "approximately"),
              (r"\bcf\.", "compare"), (r"\bviz\.", "namely"), (r"\besp\.", "especially")]

# Units after a number. "5 km" was "five K M".
_UNITS = {"km": "kilometres", "kg": "kilograms", "cm": "centimetres", "mm": "millimetres",
          "mph": "miles per hour", "oz": "ounces", "ft": "feet", "lbs": "pounds", "lb": "pounds"}
# The unit's own full stop goes ("6 lbs. of pemmican" read as two sentences)
# unless a new sentence starts after it ("He ran 5 ft. Then...").
_UNIT_RE = re.compile(rf"(\d)\s?({'|'.join(sorted(_UNITS, key=len, reverse=True))})"
                      r"(\.(?!\s*(?:[A-Z\"\u201c'\u2018]|$)))?(?![\w])")

# 6'2" was "sixty-two"; 98.6°F lost its "point".
_FEET_INCHES = re.compile(r"\b(\d)\s?['\u2032\u2019](\d{1,2})\s?[\"\u2033\u201d]")
_DEGREES = re.compile(r"(\d)\s?°\s?([CF])\b")
_FRACTIONS = {"1/2": "one half", "1/3": "one third", "2/3": "two thirds",
              "1/4": "one quarter", "3/4": "three quarters"}
_FRACTION_RE = re.compile(r"(?<![\d/])(1/2|1/3|2/3|1/4|3/4)(?![\d/])")

# A sign before a number. Kokoro drops "-3" entirely ("a loss of points") and
# drops the decimal point after "+" ("+2.5" was "plus two five"). Only a sign
# that starts a number: "3-2", "555-1234" and "pp. 94.-96." keep theirs.
_SIGN_RE = re.compile(r"(?<![\w.])([-\u2212+])(?=\d)")

# "vol. i, p. vii, part ii": footnotes number volumes, front-matter pages and
# parts in lower-case numerals, which the capital-only rule above cannot see.
# Kokoro said "vol eye", "P, Roman seven", "part Roman two".
_LOWER_ROMAN_RE = re.compile(r"\b(vol\.|ch\.|chap\.|p\.|pp\.|part)\s+([ivxlc]{1,6})\b(?!['\u2019]\w)")
_LOWER_ROMAN_WORD = {"vol.": "Volume", "ch.": "Chapter", "chap.": "Chapter", "p.": "page",
                     "pp.": "pages", "part": "part"}

# A range between numbers set with an en dash: "pp. 10–12" was "ten twelve".
# Only the en dash: a hyphen is also a score ("3-2") or a phone number.
_RANGE_RE = re.compile(r"(\d)\s?\u2013\s?(\d)")

# Regnal numbers: "Henry VIII" was spelled "V I I I". Only after a monarch's
# or a pope's name, and a lone "I" only when nothing like a verb follows
# ("Mary I think" keeps its pronoun).
_REGNAL_NAMES = (r"Henry|Edward|George|William|Richard|Charles|James|John|Louis|Philip|"
                 r"Elizabeth|Mary|Anne|Victoria|Frederick|Alexander|Nicholas|Peter|Ivan|"
                 r"Catherine|Napoleon|Pius|Leo|Gregory|Benedict|Innocent|Clement|Urban|"
                 r"Paul|Boniface|Alfonso|Ferdinand|Isabella|Gustav|Gustavus|Wilhelm|"
                 r"Ludwig|Otto|Constantine|Ptolemy|Ramesses|Rameses|Darius|Xerxes|Cyrus|Robert|David|Malcolm|Harold")
_REGNAL_RE = re.compile(
    rf"\b({_REGNAL_NAMES})\s+((?:[IVX]{{2,6}}|V|X)\b(?!['\u2019]\w)|I(?=\s*(?:[.,;:!?)\u2014]|$)))")
_ORDINALS = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth", 6: "Sixth",
             7: "Seventh", 8: "Eighth", 9: "Ninth", 10: "Tenth", 11: "Eleventh",
             12: "Twelfth", 13: "Thirteenth", 14: "Fourteenth", 15: "Fifteenth",
             16: "Sixteenth", 17: "Seventeenth", 18: "Eighteenth", 19: "Nineteenth",
             20: "Twentieth", 21: "Twenty-First", 22: "Twenty-Second", 23: "Twenty-Third"}


def _say_regnal(m):
    n = roman_to_int(m.group(2))
    return f"{m.group(1)} the {_ORDINALS[n]}" if n in _ORDINALS else m.group(0)


def _say_lower_roman(m):
    n = roman_to_int(m.group(2).upper())
    if not n:
        return m.group(0)
    return f"{_LOWER_ROMAN_WORD[m.group(1)]} {spell_number(n)}"


def _say_shorthand(text):
    text = _LOWER_ROMAN_RE.sub(_say_lower_roman, text)
    text = _HON_RE.sub(lambda m: ("Right " if m.group(1) else "") + "Honourable ", text)
    text = _TITLE_RE.sub(lambda m: _TITLES[m.group(1)] + " ", text)
    text = _OLD_ORDINAL_RE.sub(_say_old_ordinal, text)
    text = _MONTH_RE.sub(lambda m: _MONTHS[m.group(1)] + " ", text)
    refs = dict(_REFS)
    text = _REF_RE.sub(lambda m: refs[m.group(1)] + " ", text)
    for pattern, word in _SHORTHAND:
        text = re.sub(pattern, word, text)
    text = _FEET_INCHES.sub(lambda m: f"{m.group(1)} feet {m.group(2)}", text)
    text = _DEGREES.sub(lambda m: f"{m.group(1)} degrees " +
                        ("Fahrenheit" if m.group(2) == "F" else "Celsius"), text)
    text = _UNIT_RE.sub(lambda m: f"{m.group(1)} {_UNITS[m.group(2)]}", text)
    text = _SIGN_RE.sub(lambda m: "plus " if m.group(1) == "+" else "minus ", text)
    text = _FRACTION_RE.sub(lambda m: _FRACTIONS[m.group(1)], text)
    text = _RANGE_RE.sub(r"\1 to \2", text)
    return _REGNAL_RE.sub(_say_regnal, text)


def expand_for_speech(text):
    """English only: the abbreviations and numerals a narrator cannot guess.

    Mr., Mrs. and Dr. are left alone: every narrator reads them. These are the
    ones that were found going wrong or were ambiguous in the books read, and
    (_say_shorthand) the ones Kokoro read wrong in a test passage.
    """
    # "No. 73 bus" -- "No." before digits is always "Number". NOT before a
    # roman numeral: the second read-through found '"No. I loved him"' turned
    # into '"Number One loved him"' -- in dialogue "No." is an answer and "I"
    # is the speaker, four times in Redeeming Love alone.
    text = re.sub(r"\bNo\.\s+(?=\d)", "Number ", text)
    # "Fr. Spike" -- a priest, never read correctly as two letters.
    text = re.sub(r"\bFr\.\s+(?=[A-Z])", "Father ", text)
    # "St. Paul" is a saint; "Baker St. The" is a street, told apart by the
    # capitalised word in front of it.
    def saint_or_street(m):
        before = text[:m.start()].rstrip()
        prev = before.split()[-1] if before.split() else ""
        return m.group(0) if prev[:1].isupper() and prev[-1:].isalpha() else "Saint "
    text = re.sub(r"\bSt\.\s+(?=[A-Z][a-z])", saint_or_street, text)
    # "[Here the MS. breaks off" -- a manuscript, not two letters. Upper case
    # only, so the title "Ms." is untouched.
    text = re.sub(r"\bMSS\.(?=\s)", "manuscripts", text)
    text = re.sub(r"\bMS\.(?=\s)", "manuscript", text)
    # "out-manœuvred": ligatures are a typesetter's choice, not a sound, and a
    # tokenizer may not know them.
    text = text.replace("œ", "oe").replace("Œ", "Oe").replace("æ", "ae").replace("Æ", "Ae")
    text = _CITATION.sub(say_citation, text)
    # After the citations, which read their own en dashes as "verses 2 to 3".
    text = _say_shorthand(text)

    def say_roman(m):
        value = roman_to_int(m.group(2))
        if not value or not (0 < value < 1000):
            return m.group(0)
        return f"{m.group(1)} {spell_number(value)}"

    return _ROMAN_AFTER_NOUN.sub(say_roman, text)


def narrate_text(tag, text, next_text=None, language="en"):
    """The string sent to the narrator for one block, or None to skip it.

    `next_text` is the following block's text, because whether this one needs
    a full stop depends on how the next one begins.
    """
    # English only (core/languages.py): `language` stays in the signature for
    # the callers, and the narrator never sees another language.
    clean = expand_for_speech(normalize_text(normalize_caps(text)))
    if not clean or len(clean) < 2 and clean.lower() not in CONNECTORS:
        return None
    # A closing quote ends a sentence only when the quotation carries its own
    # stop. '...all that is not music is silence"' does not, and the narrator
    # ran it straight into the next paragraph.
    inside = clean.rstrip("\"'”’")
    closes_bare_quote = inside != clean and inside[-1:] not in tuple(".!?…,;:—–-")
    if clean.endswith(TERMINAL) and not closes_bare_quote:
        return clean

    # The author is still mid-sentence: a salutation ("My dear Wormwood,"), a
    # letter's last line running into "Your affectionate uncle", a colon before
    # a list. A forced full stop here was narrated as ",."
    if clean.endswith((",", ";", ":", "—", "–", "-")):
        return clean
    # A paragraph a page break split in two: "If any present" / "self-indulgence
    # on your part...". The lowercase start of the next block is the tell, and
    # a full stop would cut the sentence in half.
    if next_text and next_text.lstrip()[:1].islower():
        return clean
    if clean.lower() in CONNECTORS:
        return clean

    # Force punctuation. The model reads a bare heading as if the sentence
    # continued into whatever follows it.
    return clean + ("..." if tag in HEADING_TAGS else ".")


# --- Per-book errata -----------------------------------------------------------
#
# Some defects no rule can find: an OCR scan reads "not" as "riot" and "modern"
# as "modem", and both are real words. Screwtape's source carried 45 such
# errors, found only by reading every narrated line. An errata file beside the
# book lists exact corrections, one "wrong<TAB>right" per line, applied to the
# narration only: the text on the Kobo is untouched, and sync is positional so
# it does not notice. It lives in library/ebooks/<book>/errata.tsv -- beside
# the book, out of git, since it is made of the book's own words.

ERRATA_FILE = "errata.tsv"


def load_errata(book_dir):
    """[(wrong, right)] from <book_dir>/errata.tsv, or [] when there is none."""
    import os
    path = os.path.join(book_dir, ERRATA_FILE)
    if not os.path.exists(path):
        return []
    pairs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            wrong, sep, right = line.partition("\t")
            if sep and wrong:
                pairs.append((wrong, right))
    return pairs


def apply_errata(text, errata, used=None):
    """Replace each wrong phrase with its correction; record which ones matched.

    Only whole words: an entry that starts or ends with a letter must not match
    inside a longer word, or "riot -> not" would also turn "patriotic" into
    "panotic". An edge that is punctuation matches as written.
    """
    import re
    for wrong, right in errata:
        pattern = (r"(?<!\w)" if wrong[:1].isalnum() else "") + re.escape(wrong) + \
                  (r"(?!\w)" if wrong[-1:].isalnum() else "")
        new = re.sub(pattern, lambda m: right, text)
        if new != text:
            text = new
            if used is not None:
                used.add(wrong)
    return text
