import hashlib
import os
import re
import subprocess
import zipfile
from lxml import etree

from core.languages import normalise as normalise_language

# The zip epoch. Any fixed value works; this is the earliest a zip can encode,
# so it is unmistakably synthetic rather than a plausible real mtime.
CANONICAL_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def canonicalise_epub(path):
    """Rewrite an EPUB so identical content always produces identical bytes.

    kepubify writes its members concurrently, so the *order* of entries inside
    the archive varies between runs even though every member's content is
    byte-identical (measured 2026-08-24: 10/10 members identical, order
    differing from index 6). KOReader identifies a book by hashing the file, so
    that ordering alone was enough to change a book's identity on every
    regeneration and break progress sync.

    Fixing it here rather than in the watermark step is deliberate: the
    watermark is already deterministic, and this is the only remaining source
    of drift between two runs over the same source.

    'mimetype' must stay first and stored uncompressed -- that is what makes an
    OCF archive recognisable as an EPUB from its first bytes.
    """
    tmp = path + ".canon"
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        ordered = (["mimetype"] if "mimetype" in names else []) + sorted(
            n for n in names if n != "mimetype"
        )
        payload = {n: zin.read(n) for n in ordered}

    with zipfile.ZipFile(tmp, "w") as zout:
        for name in ordered:
            stored = name == "mimetype"
            info = zipfile.ZipInfo(name, date_time=CANONICAL_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zout.writestr(info, payload[name])

    os.replace(tmp, path)
    return path


def content_identity(path):
    """The book's permanent id, derived from the file the user actually dropped.

    Derived rather than minted, so the same book resolves to the same identity
    on any machine with no coordination, and re-uploading is idempotent by
    construction instead of by dedup logic. Hashing the *dropped* file rather
    than the converted kepub is deliberate: it is what the user gave us, it
    distinguishes editions correctly, and it is known before any conversion.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return f"LBR-{digest.hexdigest()[:12]}"


def convert_to_kepub(input_epub):
    """Converts a standard EPUB to a KOReader-optimized Kepub using kepubify."""
    if input_epub.endswith('.kepub.epub'):
        print(f"   ⏩ Already a Kepub. Skipping conversion: {os.path.basename(input_epub)}")
        return input_epub

    print("   🔄 Converting to Kepub format...")
    output_kepub = input_epub.replace('.epub', '.kepub.epub')

    try:
        subprocess.run(
            ['kepubify', input_epub, '-o', output_kepub],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"   ✅ Successfully generated: {os.path.basename(output_kepub)}")
        return output_kepub
    except FileNotFoundError:
        print("\n❌ CRITICAL ERROR: 'kepubify' is not installed on your system.")
        from core.platform_support import install_hint
        print(f"👉 Install it: {install_hint('kepubify')}")
        exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ CRITICAL ERROR during Kepub conversion: {e}")
        exit(1)

def parse_xhtml(data):
    """Parse one EPUB content document, always as UTF-8.

    lxml's HTML parser falls back to Latin-1 when a document carries neither an
    XML declaration nor a <meta charset>, which is valid XHTML (UTF-8 is XML's
    default) -- so "café — naïve" reached the narrator as "cafÃ© â naÃ¯ve".
    Decoding to str first fails the other way, on documents that do declare an
    encoding. Telling the parser the encoding is the one form that works for
    both.
    """
    from lxml import html
    return html.fromstring(data, parser=html.HTMLParser(encoding="utf-8"))


def get_zip_path(base_path, relative_path):
    if '/' not in base_path: return relative_path
    base_folder = os.path.dirname(base_path)
    return os.path.normpath(os.path.join(base_folder, relative_path)).replace('\\', '/')

def get_koreader_xpath_lxml(doc_frag, element):
    # A core.structure.StrayRun is mapped at the element it is anchored to.
    element = getattr(element, "anchor", element)
    components = []
    current = element
    while current is not None and current.tag != 'html':
        parent = current.getparent()
        if parent is not None:
            siblings = [s for s in parent if s.tag == current.tag]
            if len(siblings) > 1:
                index = siblings.index(current) + 1
                components.append(f"{current.tag}[{index}]")
            else:
                components.append(current.tag)
        else:
            components.append(current.tag)
        current = parent
    components.reverse()
    return f"point(/1/body/DocFragment[{doc_frag}]/" + "/".join(components) + ")"

def extract_epub_metadata(epub_path):
    """Title, author and language.

    The language decides whether the book is narrated at all (English only,
    core/languages.py), so it is read from the book rather than configured.
    Publishers declare it in dc:language; when they do not, the caller falls
    back to detection (sample_text).
    Returns (title, author, language_code_or_None).
    """
    print("   🏷️  Extracting metadata from EPUB...")
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            container = etree.fromstring(z.read('META-INF/container.xml'))
            opf_path = container.xpath('//n:rootfile/@full-path', namespaces={'n': 'urn:oasis:names:tc:opendocument:xmlns:container'})[0]
            opf_tree = etree.fromstring(z.read(opf_path))

            title_node = opf_tree.xpath('//dc:title', namespaces={'dc': 'http://purl.org/dc/elements/1.1/'})
            author_node = opf_tree.xpath('//dc:creator', namespaces={'dc': 'http://purl.org/dc/elements/1.1/'})

            lang_node = opf_tree.xpath('//dc:language', namespaces={'dc': 'http://purl.org/dc/elements/1.1/'})

            # A missing title used to send every such book to one shared
            # "Unknown Title" folder. The file's own name is the next best name.
            title = (title_node[0].text or "").strip() if title_node else ""
            title = title or os.path.splitext(os.path.basename(epub_path))[0].replace(".kepub", "")
            author = author_node[0].text if author_node else "Unknown Author"
            language = normalise_language(lang_node[0].text) if lang_node else None

            # Sanitize for safe folder and file names
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_author = re.sub(r'[\\/*?:"<>|]', "", author).strip()
            return safe_title, safe_author, language
    except Exception as e:
        print(f"   ⚠️ Metadata extraction failed: {e}")
        return "Unknown_Book", "Unknown_Author", None

def sample_text(epub_path, chars=20000):
    """The first `chars` characters of the book's prose, for language detection."""
    out, total = [], 0
    try:
        with zipfile.ZipFile(epub_path) as z:
            for name in sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))):
                if "nav" in os.path.basename(name).lower() or "toc" in os.path.basename(name).lower():
                    continue
                body = parse_xhtml(z.read(name)).find(".//body")
                text = " ".join(body.itertext()) if body is not None else ""
                out.append(text)
                total += len(text)
                if total >= chars:
                    break
    except Exception as e:
        print(f"   ⚠️ Could not read the text to detect its language: {e}")
    return " ".join(out)[:chars]


def stamp_epub_watermark(input_epub, librarian_id):
    print(f"   🧬 Stamping EPUB DNA Watermark: {librarian_id}...")
    temp_epub = input_epub + ".tmp"

    with zipfile.ZipFile(input_epub, 'r') as zin, zipfile.ZipFile(temp_epub, 'w') as zout:
        container_xml = zin.read('META-INF/container.xml')
        container_tree = etree.fromstring(container_xml)
        opf_path = container_tree.xpath('//n:rootfile/@full-path', namespaces={'n': 'urn:oasis:names:tc:opendocument:xmlns:container'})[0]

        for item in zin.infolist():
            if item.filename == opf_path:
                opf_tree = etree.fromstring(zin.read(item.filename))
                metadata_node = opf_tree.find('.//opf:metadata', namespaces={'opf': 'http://www.idpf.org/2007/opf'})

                new_id_tag = etree.Element("{http://purl.org/dc/elements/1.1/}identifier")
                new_id_tag.set("{http://www.idpf.org/2007/opf}scheme", "LibrarianID")
                new_id_tag.text = librarian_id
                metadata_node.append(new_id_tag)

                zout.writestr(item, etree.tostring(opf_tree, encoding='utf-8', xml_declaration=True))
            else:
                zout.writestr(item, zin.read(item.filename))

    os.replace(temp_epub, input_epub)

def normalize_text(text):
    """Tidy text for TTS without destroying what the model needs to perform it.

    This used to scrub aggressively -- written for a fragile older engine, and
    it was quietly ruining every audiobook:

      - `'"' -> ' " '` put spaces around every quotation mark, so dialogue
        arrived as ` " Hello? "  he called` and the model lost any cue that a
        character was speaking.
      - em dash became a comma and an ellipsis became a full stop, flattening
        exactly the pauses that carry hesitation and drama.
      - `[^\x00-\x7F]` deleted every non-ASCII character, so
        "Le café était très près" was sent as "Le caf tait tr s pr s". Any book
        not in plain English was narrated as gibberish.

    Voxtral handles punctuation and Unicode natively -- that is why its own web
    demo sounds so much better than this pipeline did. So: normalise the shapes
    of quotes and dashes, collapse whitespace, and otherwise leave the author's
    punctuation alone.
    """
    # Straight quotes are easier for TTS front-ends to tokenise than their
    # typographic cousins, and carry the same meaning.
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")

    # The em dash is NOT normalised to a hyphen. It used to become " - ", which
    # is a lone U+002D HYPHEN-MINUS between spaces -- and a hyphen standing on
    # its own as a token is read aloud, so the narration says "minus" where the
    # author wrote a pause. Heard in The Yellow Wallpaper, which has 64 of them
    # in a nine-thousand-word story.
    #
    # Leaving it alone is also what the rest of this function already argues
    # for: the model handles Unicode natively, and an em dash carries a pause
    # no other mark does. A hyphen inside a word ("well-being") is untouched
    # either way -- it is only the free-standing one that gets verbalised.
    # Keep the ellipsis, but as three dots rather than a single glyph.
    text = text.replace("\u2026", "...")
    # Soft hyphens and zero-width characters are invisible and confuse tokenisers.
    text = re.sub(r'[\u00ad\u200b-\u200d\ufeff]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return repair_typography(text)


def repair_typography(text):
    """Undo what scanning and typesetting did to the punctuation.

    Found by reading every narrated line of two books, not by guessing:

      "eternity. .It is far better"     a stray dot an OCR left mid-page
      "devill . . the prowde spirite"   an ellipsis typeset as spaced dots
      "about himself ,which are"        the comma on the wrong side of a space
      "ROMANS 7 : 19"                   a citation colon typeset with spaces
      "you tell me !with glee"          a speck the scanner read as "!"
      "lift the the cares of man"       a doubled function word

    Each rule is narrower than it could be: a model reads a pause at every
    punctuation mark, so a wrong repair costs as much as the defect.
    """
    # The stray dot first, or the ellipsis rule below would read ". .It" as
    # an ellipsis and glue two sentences into "eternity...It".
    text = re.sub(r'\.\s+\.(?=[A-Z])', '. ', text)
    # Two or more dots separated by single spaces are an ellipsis.
    text = re.sub(r'\s?\.(?: \.)+', '...', text)
    # A citation's colon, "7 : 19" -> "7:19".
    text = re.sub(r'(\d)\s*:\s*(\d)', r'\1:\2', text)
    # A lone "!" glued to the front of a lowercase word is a scan speck. Before
    # the next rule, which would otherwise pull it onto the previous word.
    text = re.sub(r'\s!(?=[a-z])', ' ', text)
    # Punctuation detached from its word: "himself ,which" / "planned ." / "What ?"
    text = re.sub(r'(?<=\w)\s+([,;:!?])(?=\s|$|[A-Za-z])', r'\1', text)
    text = re.sub(r'(?<=\w)\s+\.(?=\s|$)', '.', text)
    # ...and a comma run into the next word: ",which" -> ", which". Digits are
    # left alone so "1,000" survives.
    text = re.sub(r',(?=[A-Za-z])', ', ', text)
    # Doubled articles and prepositions are never meant; "had had" and
    # "that that" can be, so only these.
    text = re.sub(r'\b(the|a|an|of|to|in|and)\s+\1\b', r'\1', text, flags=re.I)
    return text


def block_text(block):
    """The text of one leaf block, as the author wrote it.

    Inline runs are concatenated with NO separator, because there is no
    whitespace between them in the source. Joining on a space instead splits
    words, and publishers' faux small caps put the split in the worst possible
    place: Redeeming Love's headings are marked up as

        <h1>A N<span class="small">OTE FROM</span> F<span class="small">RANCINE</span>...

    so a space-join produced "A N OTE FROM F RANCINE R IVERS" and the model
    spelled the orphaned capitals out loud. 75 tokens in that one book were
    decapitated the same way -- "ESUS", "ATTHEW", "SALM", "HAKESPEARE",
    "ORINTHIANS" -- every one of them a scripture citation in an epigraph.
    The same space also lands in front of punctuation, which is where
    "planned ." and "And she lives there , he thought" came from; 19% of
    A Doll's House's blocks carry that mark.

    <br> is the one child element that genuinely IS whitespace, so it
    contributes the space that concatenation otherwise removes. Without it
    verse and addresses run their lines together -- 199 of them in Redeeming
    Love alone.

    Both the planning pass and the narration pass call this, because they were
    two separate expressions that disagreed, and the one the listener heard was
    the wrong one.
    """
    def walk(el):
        out = []
        if el.tag == "br":
            out.append(" ")
        if el.text:
            out.append(el.text)
        for child in el:
            out.extend(walk(child))
        if el is not block and el.tail:
            out.append(el.tail)
        return out

    return re.sub(r'[ \t]+', ' ', "".join(walk(block))).strip()
