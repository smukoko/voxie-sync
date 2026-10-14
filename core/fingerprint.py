"""The id KOReader gives a book file, computed without KOReader.

KOReader's progress sync names a document by `util.partialMD5`: the md5 of
1KB samples taken at `bit.lshift(1024, 2*i)` for i = -1..10. The first offset
is 0, not 256, because LuaJIT masks a negative shift count -- a detail that
made the first port of this function wrong. Verified 2026-09-26 three ways:
against the hashes the Kobo registered on the live gateway, against the
`partial_md5_checksum` KOReader itself stores in each book's .sdr folder
(5 of 5 equal), and against LuaJIT's `bit.lshift(1024, -2) == 0`.

Computing it here is what lets the gateway pair a device's book with its sync
map the moment the map arrives, with no handshake and no state shared between
requests.
"""
import hashlib
import io


def koreader_partial_md5(source):
    """`source` is a path, or the file's bytes when they are already in memory."""
    if isinstance(source, (bytes, bytearray)):
        return _digest(io.BytesIO(source))
    with open(source, "rb") as f:
        return _digest(f)


def _digest(f):
    h = hashlib.md5(usedforsecurity=False)   # KOReader's partialMD5: an identity, not a secret
    for i in range(-1, 11):
        f.seek(0 if i < 0 else 1024 << (2 * i))
        sample = f.read(1024)
        if not sample:
            break
        h.update(sample)
    return h.hexdigest()
