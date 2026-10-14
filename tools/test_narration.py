#!/usr/bin/env python3
"""What the narrator is sent, one rule at a time.

Every case here was found by reading a whole book's narration line by line --
Screwtape, then Redeeming Love -- and most of them are regressions of a rule
written a day earlier that "should have covered everything". Each check is
the real line, or the smallest piece of it that shows the defect, so a later
change that breaks one says which book and which line it would have ruined.

The second half checks what must NOT change: a rule that fixes one line and
damages another is how "No. I loved him" became "Number One loved him".

    python tools/test_narration.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.narration import (apply_errata, expand_for_speech, load_errata,  # noqa: E402
                            narrate_text)
from core.parser import repair_typography  # noqa: E402
from core.structure import (drop_repeated_title, is_dedication, normalize_caps, say_chapter_numbers,  # noqa: E402
                            opening_credit, trim_credits)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail!r})" if detail else ""))


def said(text, tag="p", next_text=None, language="en"):
    return narrate_text(tag, text, next_text=next_text, language=language)


# --- what Kokoro read wrong (its own phonemes, 2026-10-03) --------------------
def ex(t):
    return expand_for_speech(t)

check("ranks and titles before a name",
      ex("Lt. Col. Moran met Prof. Moriarty and Rev. Smith.") ==
      "Lieutenant Colonel Moran met Professor Moriarty and Reverend Smith.", ex("Lt. Col. Moran met Prof. Moriarty and Rev. Smith."))
# The Canterville Ghost (2026-10-12): "the Hon." was said "the Hun", "53d" "fifty-three D".
check("Honourable before a name, Right Honourable after Rt.",
      ex("the Hon. Thomas Horton and the Rt. Hon. W. E. Gladstone") ==
      "the Honourable Thomas Horton and the Right Honourable W. E. Gladstone")
check("but Hon. before an office is Honorary, left as printed",
      ex("the Hon. Secretary and Hon. Treasurer") == "the Hon. Secretary and Hon. Treasurer")
check("an old American ordinal before Street, Regiment, Lieutenant",
      ex("West 53d Street, the 22d Regiment, a 2d Lieutenant, the 13d Avenue") ==
      "West 53rd Street, the 22nd Regiment, a 2nd Lieutenant, the 13th Avenue")
check("but 3d elsewhere may be threepence: left as printed",
      ex("3d. a pound, he paid 2d for it, on the 3d of May") == "3d. a pound, he paid 2d for it, on the 3d of May")
check("a month before a day", ex("on Jan. 5, 1901 and Sept. 30") == "on January 5, 1901 and September 30")
check("page, pages, chapter, volume",
      ex("See ch. XII, p. 42 and pp. 10\u201312 of vol. 3.") == "See Chapter Twelve, page 42 and pages 10 to 12 of Volume 3.",
      ex("See ch. XII, p. 42 and pp. 10\u201312 of vol. 3."))
check("versus, approximately, compare, namely",
      ex("3 vs. 2 at approx. 10, cf. the rest, viz. all") == "3 versus 2 at approximately 10, compare the rest, namely all")
check("feet and inches", ex('He was 6\'2" tall.') == "He was 6 feet 2 tall.", ex('He was 6\'2" tall.'))
check("degrees keep their decimal point", ex("at 98.6\u00b0F and 20 \u00b0C") == "at 98.6 degrees Fahrenheit and 20 degrees Celsius")
check("units after a number", ex("ran 5 km at 4 mph with 2 kg") == "ran 5 kilometres at 4 miles per hour with 2 kilograms")
check("a unit's full stop still ends the sentence", ex("He ran 5 ft. Then he stopped.") == "He ran 5 feet. Then he stopped.",
      ex("He ran 5 ft. Then he stopped."))
check("a unit's full stop inside a sentence goes", ex("6 lbs. of pemmican and 2 oz., then") ==
      "6 pounds of pemmican and 2 ounces, then", ex("6 lbs. of pemmican and 2 oz., then"))
check("a sign before a number is said (Kokoro drops '-3' entirely)",
      ex("a loss of -3 points at \u221240\u00b0 F and +2.5\u00b0 F") ==
      "a loss of minus 3 points at minus 40 degrees Fahrenheit and plus 2.5 degrees Fahrenheit",
      ex("a loss of -3 points at \u221240\u00b0 F and +2.5\u00b0 F"))
check("footnote volumes in lower-case numerals", ex("Expedition, vol. i, p. 23.") == "Expedition, Volume One, page 23.",
      ex("Expedition, vol. i, p. 23."))
check("front-matter pages and parts in lower-case numerals",
      ex("Discovery, vol. ii, part i, p. vii, by Lieut. Bowers") ==
      "Discovery, Volume Two, part One, page Seven, by Lieutenant Bowers",
      ex("Discovery, vol. ii, part i, p. vii, by Lieut. Bowers"))
check("a typo'd range keeps its hyphen, never 'minus'", ex("pp. 94.-96") == "pages 94.-96")
check("common fractions", ex("half is 1/2 and 3/4 of it") == "half is one half and three quarters of it")
check("a year range set with an en dash", ex("1914\u20131918") == "1914 to 1918")
check("regnal numbers", ex("Henry VIII, Louis XIV, Henry V and Elizabeth I.") ==
      "Henry the Eighth, Louis the Fourteenth, Henry the Fifth and Elizabeth the First.",
      ex("Henry VIII, Louis XIV, Henry V and Elizabeth I."))
check("a pope", ex("John Paul II spoke.") == "John Paul the Second spoke.")

# ...and what those rules must NOT touch.
for keep in ("Mary I think so.", "William I was there.", "a 3-2 win", "open 24/7",
             "on 12/03/1887", "at 10 p.m. sharp", "Gen. 3:16", "Rev. 21:4", "the col. of figures",
             "Mar. was cold", "He said p. as a joke", "the fig. tree", "in 5 minutes",
             "Mr. and Mrs. Hudson", "Dr. Watson", "say 'yes' or \"no\"", "Bob and Mary I met",
             "Henry, I said", "No. I loved him", "scored 3-2", "call 555-1234",
             "a well-known 3rd-rate", "the vol. is high", "a part I liked",
             "part in the play", "p. is a letter"):
    check(f"unchanged: {keep!r}", ex(keep) == keep, ex(keep))

# --- punctuation at the end of a block ------------------------------------
check("a heading gets a pause", said("Preface", tag="h2") == "Preface...", said("Preface", tag="h2"))
check("a paragraph gets a full stop", said("July 5, 1941") == "July 5, 1941.")
check("a salutation keeps its comma, no ',.'", said("My dear Wormwood,") == "My dear Wormwood,")
check("a page-split paragraph is not cut in half",
      said("If any present", next_text="self-indulgence on your part") == "If any present")
check("'by' on its own line is not closed off", said("by") == "by")
check("a quotation with its own stop is left alone", said('"Stop!"') == '"Stop!"')
check("a quotation with no stop gets one outside it",
      said('all that is not music is silence"') == 'all that is not music is silence".',
      said('all that is not music is silence"'))
check("nested quotes are not doubled up", said("'Hello.'\"") == "'Hello.'\"")
check("a one-letter block is skipped", said("R") is None)

# --- speech expansions ----------------------------------------------------
check("Number XVII -> Number Seventeen",
      expand_for_speech("Number XVII appears to have been composed") ==
      "Number Seventeen appears to have been composed")
check("Letter IX is said", expand_for_speech("in Letter IX he") == "in Letter Nine he")
check("No. 73 -> Number 73", expand_for_speech("a No. 73 bus") == "a Number 73 bus")
check("'No. I loved him' is an answer, not a number",
      expand_for_speech('"No. I loved him"') == '"No. I loved him"')
check("'Book I read' keeps the pronoun", expand_for_speech("the Book I read") == "the Book I read")
check("Part I, is a numeral", expand_for_speech("Part I, Chapter 2") == "Part One, Chapter 2")
check("Fr. Spike is a priest", expand_for_speech("Fr. Spike said") == "Father Spike said")
check("St. Paul is a saint", expand_for_speech("St. Paul wrote") == "Saint Paul wrote")
check("Baker St. is a street", expand_for_speech("Baker St. The house") == "Baker St. The house")
check("the MS. is a manuscript",
      expand_for_speech("Here the MS. breaks off") == "Here the manuscript breaks off")
check("Ms. is left for the narrator", expand_for_speech("Ms. Smith came") == "Ms. Smith came")
check("a ligature is spelled out", expand_for_speech("out-manœuvred fool") == "out-manoeuvred fool")
check("a scripture citation is read as chapter and verse",
      expand_for_speech("Jesus, John 8:7.") == "Jesus, John 8, verse 7.")
check("a verse range", expand_for_speech("Matthew 18:21-22.") == "Matthew 18, verses 21 to 22.")
check("a numbered book, spaced range",
      expand_for_speech("2 Corinthians 3:2 – 3.") == "Second Corinthians 3, verses 2 to 3.")
check("a half-verse letter is dropped", expand_for_speech("Psalm 30:5B.") == "Psalm 30, verse 5.")
check("a multi-word book", expand_for_speech("Song of Solomon 4:7.") == "Song of Solomon 4, verse 7.")
check("a time after a day is not scripture", expand_for_speech("Sunday 10:30") == "Sunday 10:30")
check("a time after 'at' is not scripture", expand_for_speech("at 4:30 sharp") == "at 4:30 sharp")
check("a spaced citation colon is repaired first, then read",
      said("ROMANS 8 : 28") == "Romans 8, verse 28.", said("ROMANS 8 : 28"))

# --- scan and typesetting damage ------------------------------------------
check("a stray OCR dot", repair_typography("eternity. .It is far") == "eternity. It is far")
check("a spaced ellipsis", repair_typography("devill . . the prowde") == "devill... the prowde")
check("a detached comma", repair_typography("about himself ,which are") == "about himself, which are")
check("a citation colon", repair_typography("ROMANS 7 : 19") == "ROMANS 7:19")
check("a scan speck read as '!'", repair_typography("you tell me !with glee") == "you tell me with glee")
check("a doubled article", repair_typography("lift the the cares") == "lift the cares")
check("'had had' is kept", repair_typography("he had had enough") == "he had had enough")
check("1,000 is kept", repair_typography("1,000 men") == "1,000 men")

# --- capitals -------------------------------------------------------------
check("CALIFORNIA is said", normalize_caps("to CALIFORNIA,") == "to California,")
check("a small-caps opening", normalize_caps("I HAVE no intention") == "I have no intention",
      normalize_caps("I HAVE no intention"))
check("emphasis", normalize_caps("who'd find ME funny") == "who'd find me funny",
      normalize_caps("who'd find ME funny"))
check("a signature", normalize_caps("(Signed) TOADPIPE") == "(Signed) Toadpipe")
check("the Bible's LORD", normalize_caps("the LORD is my shepherd") == "the Lord is my shepherd",
      normalize_caps("the LORD is my shepherd"))
check("a scripture citation", normalize_caps("1 KINGS 19:12") == "1 Kings 19:12",
      normalize_caps("1 KINGS 19:12"))
check("'I AM' is not a time", normalize_caps("I AM that I AM") == "I am that I am",
      normalize_caps("I AM that I AM"))
check("9 AM is a time", normalize_caps("at 9 AM sharp") == "at 9 AM sharp")
check("dotted initials survive", normalize_caps("SCREWTAPE, T.E., B.S., ETC.").startswith("Screwtape, T.E., B.S.,"),
      normalize_caps("SCREWTAPE, T.E., B.S., ETC."))
check("an acronym in prose survives", normalize_caps("He said OK to the FBI.") == "He said OK to the FBI.")
check("USSR is not a word", normalize_caps("the USSR fell") == "the USSR fell")
check("a roman numeral survives", normalize_caps("CHAPTER XIV") == "Chapter XIV",
      normalize_caps("CHAPTER XIV"))

# --- chapter numbers ------------------------------------------------------
check("a bare numeral heading is said as a number, with no label guessed",
      say_chapter_numbers(["XXV", "12", "Chapter 3", "Henry V"]) ==
      ["Twenty-Five.", "Twelve.", "Chapter 3", "Henry V"],
      say_chapter_numbers(["XXV", "12", "Chapter 3", "Henry V"]))
check("a label can still be asked for", say_chapter_numbers(["IX"], label="Letter") == ["Letter Nine."])

# --- the title, the author, the credits -----------------------------------
blocks = [(None, "The Screwtape Letters"), (None, "by"), (None, "C. S. Lewis")]
state = {}
kept, removed, says = drop_repeated_title(blocks, "The Screwtape Letters", state)
check("the title is said the first time", says and len(kept) == 3 and not removed)
check("saying it is the caller's decision, not a side effect", not state.get("title_said"))
state["title_said"] = True
kept, removed, _ = drop_repeated_title([(None, "THE SCREWTAPE LETTERS"), (None, "My dear Wormwood,")],
                                       "The Screwtape Letters", state)
check("a later repeat of the title is dropped", len(removed) == 1 and len(kept) == 1)
kept, removed, _ = drop_repeated_title([(None, "Screwtape Letters, as the title says, is about")],
                                       "The Screwtape Letters", state)
check("a sentence that contains the title stays", not removed)
check("'by' is part of the opening credit", opening_credit("by", "The Screwtape Letters", "C. S. Lewis"))
check("the author is part of the opening credit",
      opening_credit("C. S. LEWIS", "The Screwtape Letters", "C. S. Lewis"))
check("prose is not an opening credit",
      not opening_credit("My dear Wormwood,", "The Screwtape Letters", "C. S. Lewis"))

check("a short dedication document is the author's", is_dedication(["To those who hurt and hunger."]))
check("a two-line dedication", is_dedication(["For my husband,", "who never stopped believing."]))
check("in memory of", is_dedication(["In loving memory of my father"]))
check("a chapter that opens with 'To' is not a dedication",
      not is_dedication(["To the reader who has come this far, " + "word " * 60]))
check("a bare title page is not a dedication", not is_dedication(["The Screwtape Letters"]))

prose = [(None, f"paragraph {i}") for i in range(20)]
kept, removed = trim_credits(prose + [(None, "Scanned by someone"), (None, "Version 1.0")])
check("a scan's sign-off at the end is dropped", len(removed) == 2 and len(kept) == 20)
kept, removed = trim_credits([(None, "Scanned by someone")] + prose)
check("a credits line early in a document takes nothing", not removed)

# --- errata ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "errata.tsv"), "w", encoding="utf-8") as f:
        f.write("# comment\n\nI do riot mean\tI do not mean\nprimarily good; our aim\tprimarily food; our aim\n")
    errata = load_errata(d)
    check("errata load, comments and blanks skipped", len(errata) == 2, errata)
    used = set()
    fixed = apply_errata("To us a human is primarily good; our aim is", errata, used)
    check("an erratum is applied", fixed == "To us a human is primarily food; our aim is", fixed)
    check("the ones that matched are recorded", used == {"primarily good; our aim"})
    check("no file, no errata", load_errata(os.path.join(d, "missing")) == [])

check("English ellipses stay as written", expand_for_speech("I ... I ... I don't know....") == "I ... I ... I don't know....")
check("a French name in an English book is left as written", expand_for_speech("He met M. Dupont") == "He met M. Dupont")

print("=" * 60)
failed = results.count(False)
print(f"{len(results)} checks: {len(results) - failed} passed, {failed} FAILED")
sys.exit(1 if failed else 0)
