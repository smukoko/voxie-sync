# Voxie, the long version

The [README](../README.md) has the three-step install. This page is for the
curious: what happens to a book, what runs where, the free routes, and how to
install everything by hand.

**Read a book on your e-reader, keep going as an audiobook from the page you
were on, and come back to the page where the audio stopped.**

Voxie turns your DRM-free EPUBs into audiobooks, narrated on your own computer
by default, and keeps the reading position and the listening position in step,
both ways:

- **Read → listen.** Put the Kobo down mid-chapter and open the book in any
  [Audiobookshelf](https://www.audiobookshelf.org/) app: it plays from the
  start of the paragraph at the top of your page. That is on purpose, and a
  little early: the Kobo tells Voxie which page you are on, not how far down
  it you read, so the audio starts where the page starts rather than risk
  skipping what you have not read. Expect a few sentences you already know
  (10-15 seconds on average, more after a long paragraph).
- **Listen → read.** Stop listening, pick up the Kobo: it opens on the page
  where the audio stopped, never across a chapter start.
- **One click on a Kobo.** Plug it into a Mac and press Connect: KOReader,
  the sync plugin, your login, dictionaries and reading fonts, all installed.
  Any other device that runs KOReader pairs with a few settings.

Voxie is free and open source, and runs on machines you own: no account with
us, no subscription, no telemetry. It is built on
[KOReader](https://koreader.rocks), the open-source reader; on the Kobo it is
opened from a **Voxie** button in the bottom bar. It is not affiliated with the
KOReader project or with Rakuten Kobo.

## How it works

```text
your EPUB ──► ~/Kobo_Dropzone on your Mac or Linux machine
                 │  the watcher picks it up
                 ▼
              narrated (your CPU, your GPU, or a rented one), aligned word by
              word, and mapped: every paragraph to its second in the audio
                 │  uploaded
                 ▼
              your server: the Voxie gateway + Audiobookshelf
                 │
     ┌───────────┴────────────┐
 the Kobo (KOReader)    any Audiobookshelf app
  reads, by page          listens, by second
     └──── positions translated both ways ────┘
```

The ebook is in your reader's catalogue minutes after you drop it; the
audiobook follows when the narration is done.

## What you need

| | |
| --- | --- |
| A Mac or a Linux computer | for narration and the watcher; Python 3.10+, [Docker](https://www.docker.com/products/docker-desktop/), ffmpeg, espeak-ng, [kepubify](https://github.com/pgaskin/kepubify) |
| A machine that is always on | for the server (the gateway and Audiobookshelf, in Docker): a spare computer, a NAS, a Raspberry Pi, or a small VPS. It can be the same computer to try Voxie out |
| An e-reader with KOReader | a Kobo (installed for you from a Mac), or any device that runs KOReader |
| A phone | with any Audiobookshelf app |
| DRM-free EPUBs you own | Voxie does not touch DRM |

## Installing by hand

What `install.sh` and `voxie init` do, step by step, for a machine they do
not cover.

**1. Install Voxie on your computer.**

```bash
git clone https://github.com/smukoko/voxie-sync.git voxie && cd voxie
python3 -m venv .venv && . .venv/bin/activate
pip install -e . && pip install -r requirements.txt
cp .env.example .env          # as copied: narration on your CPU, no paid service
brew install ffmpeg espeak-ng kepubify    # on Linux: sudo apt install ffmpeg espeak-ng,
                                          # and kepubify from its releases into ~/.local/bin
```

Docker is needed too (Docker Desktop on a Mac): it aligns the narration with
the text, and checks the Kobo's filesystem before the installer writes to it.

**2. Start the server.** `voxie init` does it over SSH on a fresh Linux
server. By hand, at home behind Tailscale Funnel or anywhere else:
[docs/SERVER_REFERENCE.md](SERVER_REFERENCE.md). To try Voxie on one
computer, follow the same steps there.

**3. Connect your computer to it.** `voxie init` (the setup page; `voxie init
--terminal` asks in the terminal instead), option *I already run a Voxie server*:
it asks for the server's address and the Audiobookshelf key, and prints the
two lines the server needs (the upload token and your account).

**4. Pair your e-reader.**

- **A Kobo, from a Mac:** `voxie setup-kobo` with the Kobo plugged in, or
  the last step of the setup page (`voxie init`). Unplug when it says so; the
  Kobo restarts with a **Voxie** button. A Kobo that already runs KOReader
  keeps it, and its settings. **Updates come the same way:** when a new
  version changes the Kobo's part (the plugin, or your server's address),
  plug the Kobo in and run `voxie setup-kobo` again; books, positions and
  settings stay. KOReader itself can update over Wi-Fi from its own menu.
- **Any other KOReader device:** run `voxie device-settings` and follow
  [docs/KOREADER_DEVICES.md](KOREADER_DEVICES.md).

**5. Start the watcher:** `voxie start-ghost` (a launchd agent on a Mac, a
systemd user service on Linux).

**6. Drop a book** into `~/Kobo_Dropzone`. On the Kobo, open the **Voxie**
catalogue (Search → OPDS catalog) and download it; in your Audiobookshelf
app, play it. Drop it into `~/Kobo_Dropzone_NoAudio` instead to send only the
ebook. To give that book its audio later, drop the same file (from
`~/Kobo_Archive`) into `~/Kobo_Dropzone`: the same file is the same book, so
it keeps its place on the Kobo and gains the audiobook.

If a book cannot be finished (no narrator voice yet, or the narration stops
with an error), it is moved to `~/Kobo_Dropzone_Failed` and you get a
notification. Nothing is retried by itself, so a failing book is never paid
for twice; drop it into `~/Kobo_Dropzone` again when you want to retry.

## Your archive: `~/Kobo_Archive`

Every book you drop is moved to `~/Kobo_Archive` once it has reached your
server. It is your original EPUB, and the only copy Voxie keeps: the server
holds the Kobo's version of it (a KEPUB) and the audio, not the file you
dropped. A book is recognised by that exact file, so keep it to:

- **give an ebook-only book its audiobook**: drop the file from the archive
  into `~/Kobo_Dropzone`;
- **narrate a book again**: with a new voice, after an update that narrates
  better, or after deleting it from the server.

Emptying it is safe for everything already on your Kobo and in
Audiobookshelf: reading, listening and sync keep working. What you lose is
the above: the same book downloaded again is a different file, so Voxie
treats it as a new book, starting from page one and without its audio.
Each EPUB is a few megabytes; keeping them costs little.

## Where the narration runs

| Route | Set in `.env` | Voice | Cost |
| --- | --- | --- | --- |
| **Your computer** | `TTS_PROVIDER=kokoro` (the default) | Kokoro's preset English voices | Free. 3.7× realtime on an M1 CPU: a one-hour book in about 16 minutes |
| **Your own GPU** | `TTS_PROVIDER=cosyvoice` + `TTS_URL` | a voice you add | Free on hardware you have |
| **RunPod** | `TTS_PROVIDER=cosyvoice` + the RunPod settings | a voice you add | Under $1 for a 16-hour novel: only cards measured to keep that are rented (`VOXIE_BUDGET_16H` moves it) |

Alignment (Montreal Forced Aligner) runs locally in Docker on every route.

**A voice of your own (GPU routes).** No voice ships with Voxie. Add one from
an 8–15 second clip of someone who agreed to it, from a YouTube link or an
audio or video file: on the setup page (**Narrator voice**) or with
`voxie voice-add … --consent self`. The consent is recorded with the voice.

## Listening on your phone

The server reads and writes the listening position through Audiobookshelf's
own API, so **any Audiobookshelf app stays in step with your e-reader**:
there is no Voxie app. On iPhone, apps with real progress sync include
ShelfPlayer, Plappa, SoundLeaf and AudioBooth; on Android, the official
Audiobookshelf app.

## Other devices

Any device that runs KOReader works: a Kobo set up from Windows or Linux, a
PocketBook, a Kindle with KOReader, Android. Sync goes through KOReader's own
Progress sync and catalogue, so pairing is a few settings
([docs/KOREADER_DEVICES.md](KOREADER_DEVICES.md)). The map format and
the sync protocol are documented in [docs/SYNC_FORMAT.md](SYNC_FORMAT.md)
(CC-BY-4.0), so other readers can take part. Ports are welcome; see
[CONTRIBUTING.md](../.github/CONTRIBUTING.md).

## Privacy: what leaves your machine

Nothing is sent to the project: Voxie has no account with us, no analytics
and no telemetry. What goes where depends only on the options you choose.

| Part | Where it runs | What it sees |
| --- | --- | --- |
| Narration on your CPU (the default) | your computer | everything, and nothing leaves it |
| Narration on your own GPU | your machine | the book's text and your voice clip |
| Narration on RunPod (optional, paid) | a GPU rented from RunPod | the book's text, in chunks, and your voice clip, for the run; the pods are deleted after it |
| Junk filter, local (the default) | your computer | the text, locally |
| Groq (only if you add a key) | Groq | up to 1,500 characters of a section the local filter is unsure about, and a title plus a short excerpt per section |
| JEV via OpenRouter (only if you choose it) | OpenRouter / TypeSafe | a ~1,500-character excerpt per section, and paragraphs if paragraph filtering is on |
| The narration director (GPU narration with an OpenRouter key; `NARRATION_DIRECTOR=off` turns it off) | OpenRouter / TypeSafe | every paragraph, with the two before and after it |
| The gateway and Audiobookshelf | your server | the books and audio you upload, your reading and listening positions, your account; the sync maps hold positions only, never the book's text |
| The e-reader and your phone | your devices | talk only to your server |

First use downloads public files: the narration and alignment models (from
Hugging Face and GitHub), and KOReader and NickelMenu when you pair a Kobo.

## Before you use it

- Narrate only books you own, without DRM. A protected EPUB gets a warning;
  Voxie does not remove or work around DRM.
- Clone only a voice whose speaker agreed. `voxie voice-add` records that
  consent; no voice ships with Voxie.
- The audiobooks you make are for your own listening, as the books are.

## Limits, honestly

- The one-click installer runs on **macOS**; from Windows or Linux, pair by
  hand ([docs/KOREADER_DEVICES.md](KOREADER_DEVICES.md)). Narration, the
  watcher and the server run on macOS and Linux; Windows is not supported yet.
- **English only.** Voxie narrates English books; no other language has been
  tested end to end. A book in another language reaches the Kobo as an ebook,
  without an audiobook. If an English book is labelled as another language,
  narrate it with `python librerian_engine_v3.py <book.epub> --language en`.
- Tested on a **Kobo Clara 2E** (firmware 4.38) with KOReader 2026.07.1.
- Narration on a CPU is slower than on a GPU: plan on a novel overnight.

## License

AGPL-3.0-or-later ([LICENSE](../LICENSE)): use it, change it, run it for others, and share
your changes when you do. The map format spec is CC-BY-4.0. Contributions are
made under a [Contributor License Agreement](../.github/CLA.md): you keep your copyright,
and your work stays open source. What Voxie builds on, installs and downloads,
with each licence: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Security
reports: [SECURITY.md](../.github/SECURITY.md).

Kobo is a trademark of Rakuten Kobo Inc.; KOReader and Audiobookshelf belong
to their projects. Voxie is an independent project.
