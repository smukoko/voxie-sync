# The Voxie map and sync protocol, v1

*This document is licensed under CC-BY-4.0 (`docs/LICENSE-CC-BY-4.0.txt`). The
code is AGPL-3.0. You may implement this format and protocol in anything,
under any licence.*

This is what a reader on another device needs to take part in sync: another
e-reader, a Windows or Linux installer, a phone app. Nothing here is specific
to the Kobo. The Kobo is simply the first device, through the KOReader plugin
in `librarian_sync.koplugin/`.

## 1. The idea

An audiobook position is a number of seconds. A reading position is a place in
a document. The **sync map** is the list of pairs that joins them: *this block
of text is spoken from second A to second B*. The gateway holds the map and
answers two questions:

- a reader says where it is → the gateway moves the audiobook there;
- a reader asks where to open → the gateway answers with the place the audio
  reached (or the reader's own place, when that is newer).

The map holds **positions only**: block paths and times, never the book's text.

## 2. Identifying a book

A device names a book by the fingerprint KOReader already computes,
`partialMD5` of the file it reads (`core/fingerprint.py`):

```
md5 over 1024-byte samples at offsets 0, 1024, 4096, 16384, ... (1024 << 2i for i = 0..10),
stopping at the first offset past the end of the file
```

The first offset is **0** (LuaJIT masks the negative shift in KOReader's
`bit.lshift(1024, -2)`). A device that reads a *different file* for the same
book (another format, another conversion) gets another fingerprint, and the
gateway must be told about it: see §5.

The gateway's own id for a book is `lib_id = "LBR-" + sha256(epub the user dropped)[:12]`.

## 3. Positions in the text

A block is addressed by a **KOReader XPointer** into the file the device reads
(the KEPUB for Kobo):

```
/body/DocFragment[3]/body/p[10]
```

`DocFragment[n]` is the n-th spine item (1-based); the rest is the element
path inside that XHTML document, with 1-based sibling indices counted per tag
name. A position the device *sends* may be deeper (`.../p[10]/span[2]/text().9`);
the gateway resolves it to the enclosing mapped block. A position the gateway
*returns* is currently always a block (no `text().N`).

Porting to a reader with another position model (CFI, Kindle locations, page
numbers) means translating to and from this path on the device side. The map
stores it wrapped as `point(/1<path>)`.

## 4. The map (manifest.json)

A **Sync Pack** is a zip whose members all live under `library/`:

```
library/audiobooks/<title>/<title>.kepub.epub   the book as the device reads it
library/audiobooks/<title>/NNN_*.mp3            the audio, in playing order (optional)
library/audiobooks/<title>/manifest.json        the map
```

(Packs made before 2026-09-28 put the kepub and manifest under
`library/ebooks/<title>/`; the gateway still accepts them.)

```jsonc
{
  "book": {
    "lib_id": "LBR-3f9c0a1b2c4d",
    "epub_path": "library/audiobooks/Title/Title.kepub.epub",
    "title": "Title", "author": "Author",
    "koreader_md5": "…",             // §2, of the file above
    "audio_state": "pending"          // optional: "pending" while narration runs
  },
  "chapters": [                       // one row per audio segment, in order; [] = ebook only
    {"chapter_id": 7, "chapter_index": 1, "audio_filename": "001_…mp3", "duration": 812.4}
  ],
  "sync_nodes": [                     // one row per mapped block
    {"node_id": 90, "chapter_id": 7,
     "xpath_string": "point(/1/body/DocFragment[3]/body/p[10])",
     "start_time": 50.0, "end_time": 61.2}
  ],
  "word_timings": [                   // optional, finer than blocks
    {"node_id": 90, "word_index": 0, "char_start": 0, "char_end": 4,
     "start_time": 50.0, "end_time": 50.4}
  ]
}
```

- Times are **seconds from the start of the whole audiobook**, not of a file.
- `chapter_id` and `node_id` are local to the pack; the gateway re-numbers them.
- `char_start`/`char_end` count characters of the block's narrated text.
- The gateway deletes `manifest.json` after reading it and keeps no text.

## 5. The sync endpoints

KOReader's own kosync protocol, so any kosync client already speaks it.
Every request carries the account:

```
x-auth-user: <username>
x-auth-key:  <md5 of the password>
```

| Request | Meaning |
| --- | --- |
| `PUT /syncs/progress` `{"document": fp, "progress": xpointer, "percentage": 0.42, "device": …, "device_id": …}` | "I am here." The gateway records it and moves the audiobook to the matching time. Optional `"read_at"` (Unix seconds): when the page was read, for a page read offline and sent later; see below. |
| `GET /syncs/progress/<fp>` | "Where should I open?" Answers `{"document", "progress", "percentage", "device", "device_id"}`, or 404. |
| `GET /librarian/audio/<fp>` | "Where is the audio?" Same shape; always the audio's place (for a "jump to audio" menu). |
| `GET /librarian/map/<fp>` | The book's map for the device to keep, so it can translate with no network (below). `?have=<sha256>` answers 304 when the device's copy is current. 404: no audiobook. |
| `GET /users/auth` | Checks the login (200 / 401). |

What a pull answers, in order:

1. the audio has not moved since the reader last pushed → the reader's own place;
2. the reader is newer than the listener → the reader's place;
3. the audiobook server is unreachable → the stored reading place;
4. otherwise the audio time translated through the map, stepping back 3
   seconds for context but **never back across the start of an audio chapter**
   (the exact rule is under *The device's map*, below).

Where a push lands: the page's first word, two words early, when the map has
word timings (`core/page_audio.py` measures how far into its block the
xpointer falls, in the KEPUB, and takes the narrated word that far in); the
same share of the block's time, 4 seconds early, when it has none; the block's
start when either cannot be worked out. Never before the block's start.

A push never moves the audio if it is already inside the pushed block, nor
when its `read_at` is older than the audiobook's own last update (the listening
is newer: the next pull brings the reader to it). Without `read_at`, a push
counts as read when it arrives. A `read_at` in the future counts as now; one
older than four weeks is ignored.

### The device's map

`GET /librarian/map/<fp>` answers the map in a compact form, compressed with
zlib (RFC 1950; not gzip), with two headers:

```
X-Voxie-Map-Sha256: <sha256 of the compressed bytes, hex>
X-Voxie-Map-Length: <length of the JSON once inflated, bytes>
```

Inflated, it is UTF-8 JSON, positions only:

```json
{"v": 1,
 "chapters": [["Chapter Six", 10681.0, 11521.6], …],
 "nodes":    [["/body/DocFragment[16]/body/div/div/p[12]", 10871.26, 10890.48], …]}
```

- `chapters`: the audiobook's chapters as a player shows them (title, start,
  end in seconds), one per audio file, in order. Chapter numbers count from 1.
- `nodes`: every block, as `[path, start, end]`, sorted by start. `path` is
  the block's KOReader xpointer without offsets; a block split across audio
  chunks appears once per chunk.

A client keeps it and checks the sha256 every time it loads it: a map that
changed or is damaged is never used, it is downloaded again. With it, a client
applies the same rules as the gateway, with no network:

- **page → audio**: cut the position's `/span[N]…` and `/text()…` tail; the
  earliest node with that path gives the time. A path no node has walks up
  one element at a time (a page that starts inside `<em>`), down to three
  slashes. Not found: that fraction of the last chapter's end.
- **audio → page**: the chapter holding time T starts at F. Aim at
  max(T − 3 s, F): the last node starting at or before it. If that node starts
  more than 0.5 s before F, it belongs to the previous chapter; use the
  chapter's own first node instead, if it has started by T.

[`tools/landing_examples.json`](../tools/landing_examples.json) is the set of
examples the gateway's Python and the KOReader plugin's Lua are both tested
against. A port that passes them lands where Voxie does.

A device fingerprint the gateway does not know answers 404 on a pull and is
ignored on a push. Fingerprints are learned from `koreader_md5` in the pack and
from the kepub's own bytes at ingest.

## 6. Books over OPDS

`GET /opds` (HTTP Basic, same account) is an OPDS 1.x catalogue of the
account's books; each entry's acquisition link is `GET /download/<lib_id>`.
A device that downloads the kepub from here has the exact bytes the map was
made for, so its fingerprint already matches.

## 7. Versioning

This is v1. Additive fields may appear in the manifest and in responses;
clients must ignore what they do not know. A change that breaks a v1 client
will get a new version number here and a new endpoint prefix.
