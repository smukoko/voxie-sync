"""Voxie narrates English only (user, 2026-10-07): the gate, end to end in the engine.

A book in another language must reach the Kobo as an ebook and never be
narrated: no conversion for narration, no plan, no pod. The gate runs in the
real engine main() on small generated EPUBs; everything after it is faked so
that reaching it is the signal.
"""
import asyncio
import os
import re
import sys
import tempfile
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import librerian_engine_v3 as engine  # noqa: E402
from core import languages, voices  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


FRENCH = ("Le soir tombait sur la ville et les gens rentraient dans leurs maisons. Il ne savait pas "
          "pour qui elle attendait, mais elle était dans la rue des Lilas, une lettre à la main. ") * 30
ENGLISH = ("The evening fell over the town and the people went back to their houses. He did not know "
           "for whom she was waiting, but she was in the street with a letter in her hand. ") * 30
DUTCH = ("De avond viel over de stad en de mensen gingen terug naar hun huizen. Hij wist niet voor "
         "wie zij wachtte, maar het was niet de eerste keer dat zij op de hoek van de straat stond. ") * 30
GERMAN = ("Der Abend fiel über die Stadt und die Leute gingen in ihre Häuser zurück. Er wusste nicht, "
          "auf wen sie wartete, aber sie stand mit einem Brief in der Hand an der Ecke. ") * 30

check("English prose is English", languages.detect(ENGLISH) == "en")
check("French, Dutch and German prose are not",
      [languages.detect(t) for t in (FRENCH, DUTCH, GERMAN)] == ["fr", "nl", "de"])
check("English with a French phrase in it stays English",
      languages.detect(ENGLISH + " C'est la vie, said Anne. ") == "en")
check("no text is taken as English", languages.detect("") == "en")
check("declared codes in every spelling",
      [languages.is_english(c) for c in ("en", "en-GB", "EN", "eng", "fr-FR", "nl", None)]
      == [True, True, True, True, False, False, False])
check("a voice is English", voices.SUPPORTED_LANGUAGES == {"en"})

tmp = tempfile.mkdtemp()


def epub(name, language, text):
    path = os.path.join(tmp, name)
    lang = f"<dc:language>{language}</dc:language>" if language else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
                   f'<dc:creator>A</dc:creator>{lang}</metadata>'
                   '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/c1.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{text}</p></body></html>')
    return path


class ReachedNarration(Exception):
    """Raised by the first step after the gate: the book went on to be narrated."""


def run(path, override=None):
    published = []

    def publish(p):
        published.append(p)
        return 0

    def converted(p):
        raise ReachedNarration()

    with mock.patch.object(engine, "check_prerequisites", lambda *a, **k: None), \
            mock.patch.object(engine, "publish_ebook_only", publish), \
            mock.patch.object(engine, "convert_to_kepub", converted), \
            mock.patch.object(engine.drm, "warn_if_protected", lambda p: None):
        try:
            asyncio.run(engine.main(path, language_override=override))
            outcome = "returned"
        except SystemExit as e:
            outcome = e.code
        except ReachedNarration:
            outcome = "narrated"
    return outcome, published


outcome, published = run(epub("fr.epub", "fr", FRENCH))
check("a French book stops before anything is converted or billed",
      outcome == engine.NOT_ENGLISH_EXIT, f"({outcome})")
check("and is published as an ebook only", len(published) == 1)
outcome, published = run(epub("undeclared.epub", None, FRENCH))
check("an undeclared French book is detected and stops the same way",
      outcome == engine.NOT_ENGLISH_EXIT and len(published) == 1, f"({outcome})")
outcome, _ = run(epub("mislabelled.epub", "fr", ENGLISH), override="en")
check("--language en narrates an English book that says it is French", outcome == "narrated", f"({outcome})")
outcome, published = run(epub("en.epub", "en-GB", ENGLISH))
check("an English book goes on to be narrated", outcome == "narrated" and not published, f"({outcome})")
outcome, _ = run(epub("undeclared-en.epub", None, ENGLISH))
check("so does an undeclared English one", outcome == "narrated", f"({outcome})")

# The watcher: the same exit code, and it uploads the ebook instead of stopping.
ghost = open(os.path.join(ROOT, "mac_ghost.py"), encoding="utf-8").read()
code = re.search(r"^NOT_ENGLISH_EXIT = (\d+)", ghost, re.M)
check("the watcher knows the engine's exit code", code and int(code.group(1)) == engine.NOT_ENGLISH_EXIT)
branch = ghost[ghost.index("if engine_proc.returncode == NOT_ENGLISH_EXIT:"):ghost.index("elif engine_proc.returncode != 0:")]
check("and goes on to upload the ebook, telling the reader why",
      not re.search(r"^\s*return\b", branch, re.M) and "notify(" in branch and "English only" in branch)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
