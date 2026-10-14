# Changelog

All notable changes to Voxie. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

The first public release, 0.1.0, will be cut from this.

### Added

- The narration director: with GPU narration (CosyVoice 3) and an
  OpenRouter key, JEV chooses each paragraph's delivery from 16 (hushed,
  frantic, tender, shouted...) and the narrator follows it with the model's
  own instructions, speed and breaths. English gets instructions; other
  languages pace and breaths only. `NARRATION_DIRECTOR=auto|jev|off`. Needs a
  server image built from this repository (it advertises `direction`).
- Sync without Wi-Fi: the Kobo keeps each book's map (positions only, about
  55 KB for a 16-hour novel, checked by sha256 on every load) and the Voxie
  menu shows *Your audiobook position: chapter 12, 14:32* for the page you
  are on, and *I listened up to…* opens the page for a chapter and time. The
  landing rules are the gateway's, in Lua, tested against the same examples
  (`tools/landing_examples.json`). Back online, the page read offline is sent
  with when it was read, so newer listening is never overwritten. Plugin 1.6;
  `GET /librarian/map/<fp>` in the sync spec.
- Three steps to install: one install line (`install.sh`, macOS and
  Debian/Ubuntu), `voxie init`, and plugging in the Kobo. `voxie init` now
  sets up a fresh Linux server over SSH (Docker, the gateway, Audiobookshelf,
  your account), chooses the narration (RunPod or this computer), adds a
  voice, starts the watcher and installs the Kobo.
- The setup page: `voxie init` opens it in your browser (the install line
  does it by itself) and walks through the account, the server (with a
  Hetzner walk-through and the SSH key to paste), narration and a voice, and
  the Kobo, whose drawn screen warms as Voxie goes onto it. It runs on your
  own computer only. `voxie init --terminal` asks the same in the terminal.
- HTTPS with a domain of your own: an optional Caddy service fetches and
  renews a Let's Encrypt certificate (`voxie init` asks for the domain).
- A short README and DEPLOY.md; the long versions are `docs/GUIDE.md` and
  `docs/SERVER_REFERENCE.md`. The recommended paid services (Hetzner, RunPod)
  are linked with referral links, said so where they appear.
- Narration on your own computer's CPU (Kokoro-82M, `TTS_PROVIDER=kokoro`), the
  default in a copied `.env.example`: no GPU, no account, no API key.
- Narration on your own GPU (`--tts-url`), as an alternative to RunPod.
- Narrator voices from a YouTube link or an audio/video file, with the
  speaker's consent recorded (`voxie voice-add`, and the setup page).
- Each account can use its own Audiobookshelf server; its token is stored
  encrypted.
- A neutral warning when an EPUB looks DRM-protected. Voxie never refuses
  the book and never touches DRM.
- The map and sync protocol specification (`docs/SYNC_FORMAT.md`, CC-BY-4.0).
- Any KOReader device pairs by hand (`docs/KOREADER_DEVICES.md`,
  `voxie device-settings`); the server offers the plugin, set to itself, at
  `/librarian/plugin.zip`.
- The server can sit behind HTTPS (Tailscale Funnel, Caddy); the plugin
  checks the certificate against KOReader's own CA bundle.
- A guide for running the server at home, reached through Tailscale Funnel.
- Narration and the watcher run on Linux (a systemd user service).
- The Voxie logo, and a **Voxie** button in the Kobo's bottom bar.

### Changed

- One landing rule for every book: 3 s back, never across a chapter start,
  by the blocks' own times. A book without word timings used to step back a
  whole paragraph instead.
- The project is called Voxie (`voxie-sync`, command `voxie`).
- One folder per book: the ebook sits beside its audio, so Audiobookshelf
  shows each book once.
- A first drop with no narrator voice publishes the ebook and says how to add
  one, instead of failing.
- The one-click installer keeps an existing KOReader as it is (version,
  launcher, plugins, settings) and sets Binary document matching.
- The setup page fits one screen.
- The GPU image starts itself; a RunPod template needs no start command.

### Fixed

- French narration: "M." is said "Monsieur" (the voice read the letter M),
  "Mmes" "Mesdames", and regnal numbers in French ("Louis XVI" is "Louis
  Seize", not "seize romain").
- A book's printed table of contents is no longer read aloud.
- A French book's bare chapter numbers are said in French ("Deux.", not
  "Two."); other languages get the digit.
- In a Project Gutenberg file, the source edition's front matter before the
  first table-of-contents entry (a publisher's catalogue, transcriber's
  notes) is no longer read aloud.
- Chapters follow the table of contents, not the files: a Gutenberg book
  with all its chapters (or several stories) in one file used to become one
  chapter, or chapters named after the wrong story.
- Picture captions and lists of illustrations are no longer read aloud, nor
  the "The Project Gutenberg eBook, ..." line some editions put after the
  START marker.
- Text outside every paragraph (an epigraph in a table, loose text between
  blocks) is now narrated and mapped.
- Calibre's generated metadata page (rating, tags, store blurb) is no longer
  narrated.
- The junk filter works with no API key at all.
- Abbreviations, signs and numerals the narrator misread ("Lt. Col.",
  "p. 42", "Henry VIII", "6'2\"", "-40°") are said in words; a number after a
  plain hyphen is no longer dropped.
- The catalogue's download links work behind HTTPS.
