"""Does this EPUB look DRM-protected?

Voxie never touches DRM (CLAUDE.md, the user's line), and it does not refuse a
book either: a file that only *looks* protected may narrate fine, and refusing
it would be the tool deciding for the reader. So this is a neutral warning,
printed once, and the run carries on. It never says how protection works or
how to remove it.

What counts as a sign of protection, by the container's own files:

    META-INF/rights.xml          Adobe ADEPT
    META-INF/license.lcpl        Readium LCP
    META-INF/sinf.xml            Apple FairPlay
    META-INF/encryption.xml      only when it encrypts something other than fonts

encryption.xml alone is NOT a sign. The EPUB spec uses it for font
obfuscation (the IDPF and Adobe algorithms below), which plenty of DRM-free
books from ordinary publishers carry. Warning on those would teach people to
ignore the warning.
"""
import zipfile
from xml.etree import ElementTree

MARKERS = {
    "META-INF/rights.xml": "Adobe",
    "META-INF/license.lcpl": "Readium LCP",
    "META-INF/sinf.xml": "Apple",
}

# Font obfuscation, not protection: EPUB 3 (IDPF) and the older Adobe scheme.
FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

_ENC = "{http://www.w3.org/2001/04/xmlenc#}"

WARNING = ("This EPUB looks DRM-protected ({scheme}). Voxie only works with "
           "DRM-free books you own, so narration may fail or come out empty. "
           "Carrying on.")


def _encrypts_more_than_fonts(xml_bytes):
    try:
        root = ElementTree.fromstring(xml_bytes)  # nosec B314: stdlib expat fetches no external entities and caps expansion
    except ElementTree.ParseError:
        # A container that ships an unreadable encryption.xml is not a normal
        # book either; say so rather than guess it is fine.
        return True
    for data in root.iter(f"{_ENC}EncryptedData"):
        method = data.find(f"{_ENC}EncryptionMethod")
        algorithm = method.get("Algorithm") if method is not None else None
        if algorithm not in FONT_OBFUSCATION:
            return True
    return False


def detect(epub_path):
    """The protection scheme's name, or None for a book that looks DRM-free.

    Never raises on a bad file: a book that is not a zip fails loudly later,
    in the code that actually reads it, with an error about the book.
    """
    try:
        with zipfile.ZipFile(epub_path) as z:
            names = set(z.namelist())
            for path, scheme in MARKERS.items():
                if path in names:
                    return scheme
            if "META-INF/encryption.xml" in names and \
                    _encrypts_more_than_fonts(z.read("META-INF/encryption.xml")):
                return "encrypted content"
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return None


def warn_if_protected(epub_path):
    """Print the neutral warning when the book looks protected. Returns the scheme."""
    scheme = detect(epub_path)
    if scheme:
        print(f"\n⚠️  {WARNING.format(scheme=scheme)}")
    return scheme
