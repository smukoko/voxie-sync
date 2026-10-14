# Contributing

Thank you for helping. You do not need to program to make Voxie better.

## No code needed

- **Tell us when something sounds wrong.** A word said wrong, a page number
  or a note read aloud, a skipped line, a stutter, a chapter that starts in
  the wrong place, or your Kobo opening on the wrong page. Use the short
  form [Something sounded wrong](https://github.com/smukoko/voxie-sync/issues/new?template=narration.yml):
  the book's title, where it happened, and what you heard. That is all.
  No GitHub account? Email the same three things to s.mukoko@proton.me.
  Never send the book or the audio.
- **Try it on your e-reader** and tell us what happened, good or bad
  ([open an issue](https://github.com/smukoko/voxie-sync/issues/new/choose)).
- **Tell someone** who reads and listens to books.

## Code

The most valuable contributions right now:

- **Other devices.** Another e-reader (PocketBook, Kindle with KOReader,
  Boox, reMarkable), a Windows or Linux installer, a phone reader. The map
  and the sync protocol are in [docs/SYNC_FORMAT.md](../docs/SYNC_FORMAT.md); a
  device only needs its own position model translated to and from §3 there.
  Keep device code in its own folder (like `librarian_sync.koplugin/`) and add
  its tests to `tools/run_all_tests.sh`.
- **Narration fixes.** Each report above is a test case; text fixes are
  verified by reading every narrated line (`tools/narration_review.py`).
- **Other languages.** Voxie narrates English only for now (`core/languages.py`);
  French and Dutch are the next milestone. Reading rules for a language come
  with a whole-book read-through of its narration.

## Before a pull request

Read [docs/TESTING.md](../docs/TESTING.md): what every change has to pass, and why.


```bash
tools/run_all_tests.sh        # every offline suite and the Lua plugin; must stay green
```

- A change in behaviour comes with a test that fails on the old code.
- Comments explain *why*; `CLAUDE.md` holds the conventions and the rules
  that were expensive to learn.
- No credentials, IPs, hosts, book text or voice clips in the repository.
- Voices: never commit one. Anyone who adds a voice records the speaker's
  consent (`voxie voice-add`).

## Contributor License Agreement

Before your first pull request is merged you sign the [CLA](CLA.md), once:
the CLA Assistant bot asks you to reply with one sentence on the pull request.
You keep the copyright in your work.

Why: the CLA lets the maintainer also offer Voxie under a commercial
licence (for example to an e-reader maker that cannot ship AGPL code). That
is how the project pays for itself without ever charging readers. In return,
everything you contribute stays available to everyone under the AGPL (or
another open-source licence) for as long as the project is published. The
promise is written into the CLA itself.
