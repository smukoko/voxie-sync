"""A DRM-protected EPUB gets a neutral warning; a DRM-free one gets nothing.

The case that matters most is the one that must NOT warn: font obfuscation
lives in META-INF/encryption.xml too, and ordinary DRM-free books carry it.
Test T12 of docs/LAUNCH_PLAN.md is the device-side half of this.
"""
import contextlib, io, os, sys, tempfile, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import drm  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


work = tempfile.mkdtemp()


def epub(name, extra):
    path = os.path.join(work, name)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("OEBPS/ch1.xhtml", "<html><body><p>Hello.</p></body></html>")
        for member, body in extra.items():
            z.writestr(member, body)
    return path


def encryption(*algorithms):
    data = "".join(
        f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{a}"/>'
        f'<enc:CipherData><enc:CipherReference URI="OEBPS/x"/></enc:CipherData>'
        f'</enc:EncryptedData>' for a in algorithms)
    return ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            f'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">{data}</encryption>')


AES = "http://www.w3.org/2001/04/xmlenc#aes128-cbc"

check("a plain book is not protected", drm.detect(epub("plain.epub", {})) is None)
check("IDPF font obfuscation is not DRM",
      drm.detect(epub("fonts.epub", {"META-INF/encryption.xml":
                                     encryption("http://www.idpf.org/2008/embedding")})) is None)
check("Adobe font obfuscation is not DRM",
      drm.detect(epub("afonts.epub", {"META-INF/encryption.xml":
                                      encryption("http://ns.adobe.com/pdf/enc#RC")})) is None)
check("Adobe ADEPT is detected", drm.detect(epub("adept.epub", {
    "META-INF/rights.xml": "<rights/>", "META-INF/encryption.xml": encryption(AES)})) == "Adobe")
check("Readium LCP is detected",
      drm.detect(epub("lcp.epub", {"META-INF/license.lcpl": "{}"})) == "Readium LCP")
check("Apple FairPlay is detected",
      drm.detect(epub("apple.epub", {"META-INF/sinf.xml": "<sinf/>"})) == "Apple")
check("content encryption without a rights file is detected",
      drm.detect(epub("aes.epub", {"META-INF/encryption.xml": encryption(AES)})) == "encrypted content")
check("fonts plus encrypted content is detected",
      drm.detect(epub("mixed.epub", {"META-INF/encryption.xml": encryption(
          "http://www.idpf.org/2008/embedding", AES)})) == "encrypted content")

bad = os.path.join(work, "notazip.epub")
open(bad, "w").write("not a zip")
check("a broken file does not raise here", drm.detect(bad) is None)

out = io.StringIO()
with contextlib.redirect_stdout(out):
    scheme = drm.warn_if_protected(epub("adept2.epub", {"META-INF/rights.xml": "<rights/>"}))
text = out.getvalue().lower()
check("the warning is printed and the run carries on", scheme == "Adobe" and "carrying on" in text)
check("the warning never hints at removal",
      not any(w in text for w in ("remove", "strip", "dedrm", "calibre", "crack", "bypass")),
      f"({text.strip()})")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    drm.warn_if_protected(epub("plain2.epub", {}))
check("a DRM-free book prints nothing", out.getvalue() == "")

# Both engine paths (audiobook and catalog-only) must warn, not only one.
engine = open(os.path.join(ROOT, "librerian_engine_v3.py"), encoding="utf-8").read()
check("the engine warns on both paths", engine.count("warn_if_protected(epub_path)") == 2)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
