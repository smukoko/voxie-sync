# CLAUDE.md

The working instructions of the AI coding agent (Claude Code) that helps
build Voxie, public on purpose: what it must know, and the rules each
learned from a real failure. People start at the README.

Short on purpose. The measurements and stories behind every rule here are in
the commit messages. **Search
them before re-investigating anything on RunPod, MFA, FAT32, audio levels or
sync**: most "new" problems here have already been measured once.

## What this project is

Read a book on a Kobo, keep going as an AI-narrated audiobook from the same spot,
and come back to the page where the audio stopped. Two deliverables, nothing else
in scope without an explicit decision:

1. **Cross-format position sync.** EPUB in, audiobook out, and the Kobo (pages)
   and Audiobookshelf (seconds) positions mapped onto each other both ways.
2. **One-cable Kobo pairing.** One action while the Kobo is plugged in installs
   KOReader, the plugin, the account and the reading setup. Nothing manual after.

**Direction (user, final as of 2026-10-02):**

- **No money-making at launch** beyond the disclosed RunPod referral link below. The project is free and lives on donations
  and grants (NLnet first). The whole pricing analysis (hosted narration,
  hosted sync, open core, a relay, Apple apps, an installer fee) is archived in
  `docs/pricing/` with its tests. Every option failed or was rejected; do not
  reopen it unprompted.
- **Open source**: AGPL-3.0-or-later (user, 2026-10-05); the map/sync spec (`docs/SYNC_FORMAT.md`) is
  CC-BY-4.0; contributions under a **CLA** (`.github/CLA.md`, chosen so a commercial
  licence stays possible; it promises contributors their code stays open).
- **We host no books and no audio, never touch DRM, never charge for
  narration.** The user's lines. A DRM-protected file gets a neutral
  **warning, not a refusal**; never explain removal.
- **A referral link, said openly** (user, 2026-10-04): the recommended setup is
  a Hetzner server + RunPod narration. RunPod's link carries the project's
  referral code (`x88lhsfk`); Hetzner has no referral program any more, so
  its link is plain (`core/links.py`, repeated in README.md and DEPLOY.md;
  the export refuses the `REFERRAL_CODE_HERE` placeholder). The only income
  besides donations; the free routes stay documented and tested.
- **Every paid route has a free route** (CPU instead of RunPod, the local junk
  filter instead of paid APIs, your own server instead of a VPS). The Kobo
  installer is itself free. Any other KOReader device pairs by hand through
  KOReader's own Progress sync and catalogue (`docs/KOREADER_DEVICES.md`,
  `voxie device-settings`, the plugin from `/librarian/plugin.zip`): the user
  reversed "no manual guide" on 2026-10-03. `tools/test_free_routes.py` enforces the ones
  that exist.
- **Launch first** (user, 2026-10-04: "we really have to finish"). The
  ordered path is the top of `docs/LAUNCH_PLAN.md`; new ideas wait until after
  the launch or go into the NLnet milestones. Work the path, in order.
- **Cross-device by design**: plugin, installer, map format and protocol stay
  open and documented so the community can port to Windows and other
  e-readers. **No app before the launch** (the user rejected native apps).
  The one exception, decided 2026-10-04 for *after* the launch: fully
  automatic sync without internet, as a "local sync" feature in an existing
  open-source Audiobookshelf player (Android first), reached over the phone's
  hotspot as the Kobo's network gateway. It is NLnet milestone 9.
- **The setup page is local, never hosted** (2026-10-04): `voxie init` serves
  it on 127.0.0.1. A hosted site cannot write to a USB Kobo, narrate or watch
  a folder, and would hold every user's RunPod key and SSH access.
- **No voice is bundled**: users add one (YouTube link, MP3, video) and record
  consent.

## Name

**Voxie** (decided 2026-10-02): PyPI package `voxie-sync`, command `voxie`
(`libra-sync` stays as an alias). Everything a user sees says Voxie. These
internal names keep the old spelling **on purpose**, because devices, servers
and stored data depend on them: the `LBR-` book ids, `~/.librasync*` files and
caches, `LIBRASYNC_CACHE`, the `com.librasync.ghost` LaunchAgent, the KOReader
plugin folder `librarian_sync.koplugin` and the `librasync_profile_*` markers
on the device, the `X-Libra-Token` header, `LIBRA_SECRET_KEY`, the
`librarian_server` container, the `libra_cli` package and the source file
names. Renaming any of them needs a migration, never a search-and-replace.

## Map

| Path | Role |
| --- | --- |
| `librerian_engine_v3.py` | The audiobook factory: plan → TTS (Kokoro on the CPU by default, your GPU via `--tts-url`, or a RunPod swarm) → MFA → stitch → manifest. |
| `core/tts_worker.py` | Chunk worker, `map_timestamps`/`inject_chunk_map` (the map), stitcher. |
| `core/tts_providers.py` | The TTS seam. `CosyVoiceProvider` (live), `KokoroProvider` (CPU, the default). Voxtral was removed 2026-10-07 (English only). |
| `core/languages.py` | **English only** (user, 2026-10-07): a book in another language (declared, or detected) is published as an ebook, never narrated (`NOT_ENGLISH_EXIT`, the watcher notifies). French and Dutch are a later milestone; their rules are in git history. |
| `core/director.py` | **The narration director** (2026-10-06): JEV picks each paragraph's delivery from 16 and it is composed from CosyVoice 3's own trained instructions, speed and vocal tags. `NARRATION_DIRECTOR=auto\|jev\|off`. Never Kokoro. |
| `core/pod_pool.py` | Pods per book, and a registry that survives `kill -9`. Counts connection failures per pod: a lost pod (`POD_LOST_AFTER` failures and `POD_LOST_SECONDS` of silence) is terminated and replaced (#61). RunPod's addresses (`graphql_url`, `rest_url`; `RUNPOD_GRAPHQL_URL`/`RUNPOD_REST_URL` only for tests). |
| `core/cost_plan.py` | **The dollar** (#57): which cards may be rented, at what ceiling, how many pods. Only cards measured by `tools/gpu_bench.py` (`SPEED`; results in `docs/measurements/`); the whole bill counts, OpenRouter included; `VOXIE_BUDGET_16H`. |
| `server/idle_watchdog.py` | **The pod's dead man's switch** (#59, image `:4.2`): a pod with no client request for `POD_IDLE_MINUTES` (15) terminates itself with its own RunPod key. |
| `core/library.py` | One folder per book (`library/audiobooks/<title>/`: kepub + audio), so ABS makes one item. `library/ebooks/` is legacy, read only. |
| `core/parser.py`, `core/structure.py`, `core/narration.py`, `core/chunking.py` | Text: `parse_xhtml` (always UTF-8), leaf blocks, `block_text`, what the narrator says. |
| `core/junk_provider.py`, `core/block_filter.py`, `core/ai_brain.py` | Junk filtering. Local `ai_brain` by default (works with no key); JEV via OpenRouter optional (the user's live `.env` uses it). |
| `core/accounts.py`, `core/fingerprint.py`, `core/database.py` | Gateway accounts (kosync login), KOReader's `partialMD5`, the schema and its migrations. |
| `core/abs_settings.py` | Each account's own ABS: token Fernet-encrypted with `LIBRA_SECRET_KEY` (server `.env`), public addresses only for non-owners. |
| `librerian_gateway_v3.py` | Gateway: kosync, OPDS, Sync Pack ingest, ABS progress and chapter write-back. |
| `librarian_sync.koplugin/` | The KOReader plugin (v1.6, HTTPS-capable, offline translation). `main.lua` is the **single template** (`__GATEWAY_URL__`; `setup_kobo` substitutes it in every `.lua`); `voxie_landing.lua` is `core/landing.py` in Lua. |
| `libra_cli/kobo_look.py`, `koreader_patches/` | **The look** (user, 2026-10-14): the install's last step (`--look kobo\|koreader`, the setup page, `voxie look`) dresses KOReader as the Kobo (Simple UI 2.7.5, MIT, pinned; `2-kobo-look.lua`: header, footer, centre-tap toolbar; icons `assets/kobo_look/` → `.adds/voxie/look/`) and takes it off again key for key (`librasync_look_saved`). `2-wifi-in-background.lua` goes on every install: an automatic progress sync offline never holds the screen. |
| `core/gateway_address.py` | The gateway's address for every client: `GATEWAY_URL` (https) or `http://GATEWAY_IP:3000`. |
| `core/platform_support.py` | Every macOS/Linux difference: sleep inhibitor, notifications, PATH, logs, install hints, MFA as the user, the systemd unit. |
| `server/cpu_tts.py` | The CPU narration server (Kokoro), started and stopped by the engine on 127.0.0.1. |
| `docker/cosyvoice/start.sh` | The GPU image's own start (log page on 8001, weights, API); no template start command needed. |
| `mac_ghost.py` | Watcher: `~/Kobo_Dropzone` (narrate), `~/Kobo_Dropzone_NoAudio`, resumable upload. |
| `libra_cli/` | `voxie` CLI: `setup_kobo`, reading kit, FAT gate, reading profile, `luatable` (KOReader settings I/O), `voice.py` (`voice-add`: clip + transcript + consent record), `clip_check.py` (Whisper trims the clip to the typed words, refuses a wrong transcript, warns on clipping/quiet/noise; optional). |
| `assets/logo/` | The Voxie mark (SVG master, white variant, 512 px PNG) and the Kobo bottom-bar icons the installer copies to `.adds/voxie/`. |
| `libra_cli/setup_web/` | **The setup page** (user, 2026-10-04: "the best UX/UI"): `voxie init` serves it on 127.0.0.1 and opens the browser; the install line ends by opening it. Flask + plain HTML/CSS/JS, no build, no Node, fonts bundled (IBM Plex, OFL). Every `/api` call needs the session key from the URL #fragment and a local Host (`tools/test_setup_page.py`). Same functions as the terminal wizard (`voxie init --terminal`). Design: the old page's brand (instrument white/graphite, amber = the Kobo's front-light); the one bold moment is the drawn Kobo whose screen warms as Voxie installs. |
| (`kobosync-web`) | **Removed 2026-10-04** (user): the old Next.js setup page, replaced by `libra_cli/setup_web/`. Its repository `smukoko/kobosync-web` stays on GitHub as history. |
| `server/`, `docker/` | The CosyVoice server (`/clone`, `/align`) and its image. |
| `tools/` | `test_*.py` (run all: `run_all_tests.sh`), `healthcheck.py`, `gateway_admin.py` (also `abs-repair`, run by `start.sh` at every container start), `rebuild_map.py`, `repair_book.py`, `narration_review.py`, `kosim/`, `gpu_bench.py` (a card's real speed and cost), `verify_book.py` (a narrated book's claims, read-only), `verify_sync.py` (page and second point at the same words: KEPUB + Whisper), `kosim/sync_proof.py` (the same through the real KOReader), `narration_scan.py` (every chunk heard by Whisper: skips, babble, repeats). |
| `docs/TESTING.md`, `tools/integration/`, `tools/chaos/` | **The four-layer harness**: unit + gates (coverage, regression proof, mutation), layer 2 (`free_route.py`: real ABS, gateway, watcher, engine, Kokoro, MFA), layer 3 (`run.py`: the real engine against a fake RunPod whose pods fail on cue), layer 4 (`security.yml`, `test_security_gateway.py`). 2 and 3 run nightly and on a labelled PR (`integration`, `chaos`). |
| `install.sh` | The one install line (macOS via Homebrew, Debian/Ubuntu via apt; kepubify pinned by sha256). Never reads stdin: it is piped into bash. |
| `libra_cli/server_setup.py`, `server/install_server.py` | `voxie init` sets up a server over SSH: `git archive` to `/opt/voxie`, then the installer (stdlib only) with the secrets on stdin. Refuses a host whose `librarian_server` runs from another folder. |
| `core/envfile.py`, `core/links.py` | `.env` edited in place (comments kept); the recommended links (RunPod's with the referral code). |
| `server/Caddyfile` | HTTPS when the server has a domain: compose profile `https`, gateway on `https://<domain>`, ABS on `:13378`. |
| `README.md`, `DEPLOY.md`, `docs/` | Short by request (user, 2026-10-04): three install steps; the long versions are `docs/GUIDE.md` and `docs/SERVER_REFERENCE.md` (the old runbook). `SYNC_FORMAT.md` (public map + protocol spec, CC-BY), LESSONS, COMPETITION, dated `history/`. |
| `docs/LAUNCH_PLAN.md` | **The launch checklist** (phases 0-8, owner per item, tests T1-T15). Work from it. |
| `docs/KOREADER_DEVICES.md` | Pairing any KOReader device by hand (KOReader's own menu wording). |
| `docs/THIRD_PARTY_NOTICES.md` | Every bundled, installed or downloaded asset with its licence, checked at the source. |
| `docs/grants/NLNET_RESTACK.md` | Notes and budget for the NLnet proposal; the user writes the prose (NLnet rejects AI-written proposals and requires disclosure). |
| `docs/pricing/` | Archived pricing analysis + `model.py` stress tests. |
| `tools/test_free_routes.py`, `tools/leak_scan.py` | The free-route rule as a test; the export's leak check. |
| `LICENSE`, `.github/` | AGPL-3.0-or-later; `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` and the CLA (`CLA.md`, signed via CLA Assistant); how to port a device. `tools/export_public.sh` builds the public repo (fresh history, leak-checked, never pushes). |

`training_lab/` holds plaintext keys
(gitignored). `whisper_dump/` is abandoned: never resurrect it.

## Build, run, test

```bash
pip install -e . && pip install -r requirements.txt && cp .env.example .env
tools/run_all_tests.sh              # every offline suite + the Lua plugin. Must stay green.
python tools/healthcheck.py         # tests the WORLD (Groq, JEV, RunPod, ABS, gateway logins). Daily.
python -u librerian_engine_v3.py <book.epub> [--no-audio] [--voice NAME] [--language en] [--tts-url URL]
voxie voice-add --url "https://youtu.be/ID?t=90" --end 1:42 --text "…" --name NAME --consent self
voxie voice-add --file memo.m4a --start 0:05 --end 0:17 --text "…" --name NAME --consent self   # MP3/MP4/MOV too
python mac_ghost.py [--sweep-pods | --retry-uploads]
voxie init && voxie setup-kobo   # account, then the one-cable install
docker exec librarian_server python tools/gateway_admin.py add-user <name> [--userkey <md5>]
python tools/narration_review.py --title <book> --jev   # read what the narrator will say
python tools/rebuild_map.py --lib-id LBR-... --pack      # re-map from alignments, no GPU, no audio
tools/kosim/setup.sh "<title>" <lib_id>                  # the real KOReader against a local gateway
```

After every run assert: **the ABS item's duration equals the manifest's**, and
its chapter markers start where its files start (the gateway rewrites them).

## Rules that cost real money to learn

One line each; the evidence is in the commit that made the rule.

### GPU / RunPod

- Every pod is registered **at deploy time**. To stop a run: kill the engine, then the pods, then check. The sweep keeps pods whose process is alive, and treats a RunPod API failure as "unknown", never "none".
- GraphQL reports failure inside HTTP 200: check `errors`, never only the status.
- No network volume (`volumeInGb: 0`). A capacity sweep that finds nothing is a sample, not a verdict.
- RunPod keeps no container log. Bind a log server **first** on a second port.
- Live server: vLLM CosyVoice (`:4.3`, built from `voxie-sync`, in the public template once the user repoints it; `:4.2` added the pod's dead man's switch), `POD_API_WORKERS=1`, **8 callers per pod**, TCP, `TTS_API_TOKEN` required on both transports. On TCP a pod's `proxy.runpod.net` address never answers: resolve the port mapping as the engine does (`resolve_pod_base_url`); a script that waited on the proxy paid 45 min for a healthy pod (2026-10-06).
- The engine sends the user's `TTS_API_TOKEN` in the deploy's `env` (`core/pod_pool.pod_env`); RunPod merges it into the template's and it **wins** (one live pod, 2026-10-04). No template needs a token, and the pod check refuses a pod that enforces another one.
- The image starts itself (`docker/cosyvoice/start.sh`) from tag **4.1** (built 2026-10-06 from `main`, before the public repo; model loaded 5 min after the rent, `/health` lists `direction`); **4.2** adds the idle watchdog (built 2026-10-08 from #59's branch, canary-checked on an L4). It bakes the vLLM settings in (`LOAD_VLLM=1`, `GEN_CONCURRENCY=16`, `VLLM_GPU_UTIL=0.5`, `tools/test_image_defaults.py`): a template with no settings narrates. 3.4 (cu121) not built. Never overwrite a tag a template runs: new tag, then repoint on purpose.
- `.env` is loaded before any `core` import; provider classes read it at import (launchd once ran 4 workers instead of 8).
- Models from Hugging Face load from the local cache first (`ai_brain.load_embedder`): asking the hub on every run failed inside its own client and lost a whole book (#58); a junk filter that fails keeps the chapter.
- Cost = audio × $/hr ÷ realtime multiple, **plus every boot**. A 16-hour novel must cost under $1 in all, the director's OpenRouter calls included (user, 2026-10-08): `core/cost_plan.py` rents only measured cards under their ceiling, cuts `POD_COUNT`, returns a pod billed above its ceiling. Twisted on an H100 + secure `ALL` cost $1.70 for 13.5 h. Retry on a different pod; never race duplicates.
- **A pod cannot tell a paused client from a gone one.** On 2026-10-08 a laptop at 1% slept 4 h mid-book while three pods billed ($3.63). Pods end themselves after `POD_IDLE_MINUTES` (image `:4.2`, canary-checked: gone 143 s after a 2-min limit); the engine holds off sleep and says when it rents on battery. A long run of your own: `caffeinate -i`, on mains.
- A pod that dies is terminated and replaced (two at most); a refusal waiting cannot fix (no funds, no template, a server ignoring the token) ends the run in its own words.
- Measure a card with `tools/gpu_bench.py` before giving it a speed: an A6000 and a 4090 rented on 2026-10-07 served `/health` and never finished the workload (about $1 for nothing). Community pods on TCP never served (2026-09-21, twice on 2026-10-07): `SECURE`, named.
- CosyVoice returns float32 WAVs: measure with `probe_duration` / `wav_duration_from_bytes`.
- On `cosyvoice-tts:4.0`, **`LOAD_VLLM=1` always**: with 0 it babbles (no words at all; its transformers is re-pinned for vLLM). Before a paid batch, render one line and check it with Whisper (a canary).
- A script that uses `pod_pool` must `load_dotenv` into `os.environ`: `terminate()` reads the key from there and otherwise does nothing, silently (2026-10-06). After any run, list the account's pods.
- Secure-cloud pods pulled `:4.0` in ~6.5 min; a community host in Taiwan took 30+ (the `:4.0` image also downloads its weights at boot).

### Audio and alignment

- Lossless until one encode per chapter; never `-c copy` MP3s. Loudness per chapter (`loudnorm I=-19`).
- A chunk's audio is placed whether or not MFA aligned it; its duration comes from the file.
- A chunk's dead air after its last word is cut to 0.5 s, losslessly, fresh or cached (`trim_trailing_silence`; CosyVoice ended one line in 6.9 s of silence, twice). Only the end: pod timings count from a chunk's start. A long *leading* silence (up to 2.3 s seen) is still kept.
- MFA is pinned by digest (3.3.10), the English models pinned at matching versions. It splits "e-mails"; `map_timestamps` aligns sequences, never 1:1.
- `mfa_workspace/` holds the **only** copy of the narration and is wiped each run. It is also what `rebuild_map.py` re-maps from.
- Stitched chapters are consecutive same-title runs, tagged `TRCK`. ABS keeps stale chapter markers when files are replaced; the gateway rewrites them after every audio pack.
- A book needs ~2x its audio in free disk.
- **A chunk MFA refuses at its default beam is a narrator skip** (Twisted: 8 of 8, confirmed by Whisper). The engine retakes it on the pods before releasing them (`retake_refused`, `CHUNK_RETAKES`=2; no cache, own run id); on 2026-10-11 all 8 aligned within two retakes and Whisper heard each one right. A remainder MFA aligns none of exits 1: that is a skip, not a crash (`mfa_unready` decides).
- **ABS (on the server) lists a file twice when a re-upload keeps its name** (Twisted 2026-10-07 and 2026-10-11; not reproducible with Docker on the Mac, even through the real gateway). A rescan rebuilds the tracks from that list, so never rescan such an item (#70). The cure is to forget the item (never `?hard=1`) and rescan the library; the gateway finds the new id by watermark (#73), and does this itself when a pack lands, carrying the uploader's position and bookmarks to the new item (`_forget_doubled_item`). ABS drops a forgotten item's progress but keeps its bookmarks, orphaned and impossible to delete afterwards: take them off first (2.36.0 in Docker, 2026-10-12).

### Text

- Text reaches the model as the author wrote it. EPUB XHTML is parsed with `parse_xhtml` (UTF-8), never bare `html.fromstring`.
- `block_text` is the one extraction; narration is verified by **reading every line** (`narration_review.py`, which also reports text outside every block). Errata are whole-word, narration-only.
- Junk filtering is layered, deterministic first, and tested on what it must **not** delete. Block threshold 0.9. An outage costs the verdict, never the book.
- `ai_brain` reads a long document at its start, middle and end, and Groq is asked per window: a chapter that opens on footnotes or a table is not notes (The Worst Journey lost ~120,000 words to one window, #37).
- The narrator never says a label the page does not print.
- A document the TOC names nothing in **joins the chapter before it** (user, 2026-10-11, `untitled_document_title`); a book with no TOC, or a document before the first chapter, keeps its own name.
- A chapter is a **TOC entry**, not a file (`core/structure.chapter_starts/chapter_segments`): one file may hold many (Gutenberg). Entries under 40 words are headings and fold into a neighbour; the file's first entry names its first block. Picture captions and lists of illustrations are not narrated (`drop_picture_text`), tested on what they must keep.
- Text above a file's first TOC entry ends the **previous** chapter (Gutenberg splits files by size). A Gutenberg title page (before the first entry with 40+ words) says only its credit: title, "by", author, dedication.
- Text outside every leaf block is narrated as a `StrayRun`, anchored to an element the KEPUB resolves (never a container that holds blocks). Calibre's jacket is declared junk.
- `core/narration._say_shorthand` (English only) says ranks, months, page/volume references, units, signs, fractions, en-dash ranges and regnal numbers in words, each only where unambiguous. **Kokoro drops a number after a plain hyphen** ("-40" → nothing). A new rule is verified by diffing whole-book narration before/after (The Worst Journey, Walden, the library) and reading every changed line.

### Narration director (`core/director.py`, 2026-10-06)

- Only CosyVoice 3's **own** instruction strings (upstream `instruct_list`, verbatim, Chinese included) and its tokenizer's breath tags. **The baseline's 8 deliveries, one native instruction each, no speed change** (user, 2026-10-12: the 16 composed with speeds, sighs and laughs sounded less natural in the user's voice). Any further option comes back only after its own listening test. JEV writes no text.
- English only, like all narration (in French an instruction cost the accent: Whisper words 92% -> 83%).
- A **registered speaker silently drops the instruction** (`frontend_instruct2` reuses the stored transcript): an instructed chunk uploads the clip.
- Tags never reach alignment (server `_TAG_RE`) or the map; the provider directs only a server whose `/health` lists `direction`. One delivery per chunk; the cache key includes it.
- No sigh or laugh tags (not in the baseline); lines under 4 words plain. Kokoro is never directed (Kokoro by speed/volume made no audible difference).
- The reference clip's sounds go into every sentence (a test clip's trailing sigh): clips are trimmed to speech.
- yt-dlp's `--download-sections` cuts where the stream allows, seconds off (asked 1:01.7, got ~1:08): `voice-add` passes `--force-keyframes-at-cuts`, measured exact against the whole video (2026-10-12).

### Identity, accounts and sync

- `lib_id = "LBR-" + sha256(dropped epub)[:12]`; the kepub is canonicalised so its KOReader hash is stable.
- Pairing is by **fingerprint**: `core/fingerprint.py` computes KOReader's `partialMD5` (first offset **0**, LuaJIT masks the negative shift) at ingest and from the manifest's `koreader_md5`. Verified against the device's own `.sdr` checksums.
- Every book, pairing and position belongs to an account (`user_id`; 0 = pre-account, claimed by the first account). kosync auth is `x-auth-user` + `x-auth-key` (md5 of password); OPDS is HTTP Basic.
- An account's own ABS token is stored only encrypted (`fernet:` prefix), is never in a response or argv, and needs `LIBRA_SECRET_KEY`; non-owner ABS addresses must resolve public at set time **and** at use; no ABS call follows a redirect.
- The gateway stores maps as **positions only**: no paragraph or word text.
- Every client gets the gateway's address from `core/gateway_address.gateway_url` (`GATEWAY_URL` when https, else `http://GATEWAY_IP:3000`). OPDS download links are **relative** (KOReader resolves them against the catalogue's address). The plugin verifies TLS against KOReader's `data/ca-bundle.crt`; KOReader's own Progress sync verifies nothing. The gateway's public address for `/librarian/plugin.zip`: `GATEWAY_PUBLIC_URL`, else `X-Forwarded-Proto/Host` (Tailscale Funnel sends them).
- **Read → listen starts at the page's first word**, two words early (`core/page_audio.py`: the KEPUB says how far into its paragraph the page starts, `word_timings` which word that is; no words: the same share of the paragraph, 4 s early). It used to start at the paragraph, 10-16 s early on average ("five sentences back", user, 2026-10-14).
- Pull rules: audio unchanged since the reader's push → the reader's exact place; reading newer than listening → the reader's place; ABS down → the stored place; else translate the audio time, rolling back 3s but **never across a chapter start**. A push never rewinds audio already inside the pushed paragraph. The plugin's menu uses `/librarian/audio/<hash>`.
- The reader reads the **KEPUB**; only element paths resolve there. Keep `EMIT_TEXT_OFFSETS = False` until offsets map to `koboSpan`s and back from narrated text.
- **Offline translation** (built 2026-10-04, LAUNCH_PLAN 2.7): `GET /librarian/map/<fp>` hands the device the map (positions + chapters, zlib, ~55 KB for 16 h) with its sha256; the plugin keeps it in `<KOReader data>/.voxie_maps/`, never beside the books, checks the sha256 on **every** load and deletes a bad copy. The landing rules live in `core/landing.py` and `voxie_landing.lua`, both tested against `tools/landing_examples.json`: change both or the tests fail. One rule for every book (3 s back by node times, never across a chapter start); word timings no longer steer it. Back online the plugin pushes the page read offline with `read_at`, before KOSync's own pull, and drops KOSync's queued older page for that book (drained, it would overwrite newer listening).
- Lifecycle is stated (`audio_state`), deletion is a tombstone, nothing destructive on a GET, an ebook-only pack never touches a map, and a failed ingest answers 500.
- Library folders are keyed by **title**, which is not an identity. One folder per book: the kepub sits beside its audio (`core/library.py`); a regeneration clears **audio only**; deleting a book also forgets its ABS item (never `?hard=1`).

### Deploying and running unattended

- The gateway follows a **branch**: deploy with `git fetch && git checkout <branch>` (a `git pull` on an unchanged branch deploys nothing), then `docker compose up -d --build librarian`, then verify (container md5 against the commit; `/librarian/plugin.zip` answering 401 means the 2026-10-03 code). Pull as `libra`. Compose recreates ABS too when its definition changed.
- A drop that does not finish moves to `~/Kobo_Dropzone_Failed` (notification; never retried by itself); after a failed upload the book is archived and the kept pack is sent at the next start (#78).
- Commands inside the live server, and moving its secrets, are the **user's**: Claude Code's auto mode blocks them. Hand over the exact commands.
- A loaded LaunchAgent runs the old code: re-run `voxie start-ghost` after changes.
- Gateway image = `requirements-gateway.txt` + `tools/gateway_admin.py` only. `ufw` does not protect Docker ports.
- Always `python -u` the engine. Measure the instrument before believing an alarm.
- Never print a secret: capture tokens and passwords straight into their file.
- A CLAUDE.md line ending in `<!-- private -->` is this machine's and is never exported (`tools/export_public.sh`). The rule for the export (user, 2026-10-12): what does not have to be there, or whose removal costs no understanding, goes.

### Kobo

- Detect by `.kobo/`, never volume name. Extract KOReader with `ditto` into `.adds/`; KOReader and NickelMenu are pinned by sha256; the FAT gate must be seen to fail.
- Edit KOReader settings through `libra_cli/luatable.py` (round-trips every real file to LuaJIT-identical output), in place: **never create new files** beside existing ones on the device (8.3 name collisions).
- The reading profile changes only values still at KOReader's defaults, once per version (v2: Literata 20, 120%, margins 15/20/15, weight +0.5, word expansion 5, History home). Books keep typography in their `.sdr`; `restyle_books` updates those at the root only.
- Test device behaviour in `tools/kosim` (real KOReader 2026.07.1 at 300dpi) before touching the Kobo.
- **Never touch the reader's own books** (`KEbooks`) without explicit consent.
- NickelMenu: the installer writes `.adds/nm/koreader` (bottom-bar label **Voxie**, icons `.adds/voxie/nav.png`/`navon.png`, entry "Voxie (KOReader)") and rewrites it on a device only when every line in it is one Voxie wrote; a user's own NickelMenu or KFMon is never touched. Firmware 4.23.15505+ shows it in the bottom bar; a change may need a reboot.
- An existing KOReader is kept (version, launcher, plugins, settings): the installer warns when it is older than the pinned one, forces **Binary** document matching (file-name matching never pairs), and the reading profile skips the plugin tuck-away.
- The installer is **macOS-only** (it needs `ditto`); narration, the watcher and the server run on macOS and Linux (`core/platform_support.py`; CI is Ubuntu).
- **Never open a KOReader database from the Mac.** `bookinfo_cache.sqlite3` is WAL: a read-only `sqlite3` query left `-wal`/`-shm` beside it on the FAT volume (2026-10-14). The Kobo look sets Cover Browser's options in memory (`2-kobo-look.lua`), never in the file.
- KOReader patches live in `.adds/koreader/patches/` (`2-` = after the UI is up) and go through the FAT gate like every payload. Plugins are keyed on the reader by folder name (`ui.librarian_sync`, `ui.kosync`).
- KOSync's automatic pull (book open, wake) offline ran KOReader's interactive connect: "Scanning for networks…" for up to 2 min. The Wi-Fi patch restores Wi-Fi in the background instead and queues the push at sleep; the plugin's reconnect push (0.1 s) still beats KOSync's pull (0.5 s).

## Coding style

- `subprocess.run([...])` with a list; check the return code.
- No credentials, IPs or hosts in source; `.env` or `~/.librasync.json`.
- One source of truth: the plugin template, `block_text`, `parse_xhtml`, `inject_chunk_map`, `core/languages.py`, `server/custom_api.py`.
- No import-time side effects. TTS stays behind the provider seam; keep asyncio + httpx.
- Fail loudly on expensive paths; a test that cannot fail on the old code is not a test.
- Comments explain *why*.

## Git

- Never commit to `main`; branches `feat/…`, `fix/…`, `chore/…`, `docs/…`; semantic messages with a *why* body.
- **Never push without asking.** No history rewrites, force-pushes or branch deletion without confirmation.
- **The public repo's commits carry the GitHub noreply address, both author and committer** (`<id>+smukoko@users.noreply.github.com`; `gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'`). Setting only the `GIT_*_NAME` variables for the export left the machine's gmail address on a public commit (2026-10-16, found 10-22): set all four `GIT_*` variables, and check `git log --format='%ae %ce'` before the push.
- `tools/export_public.sh` refuses while the tree has *any* untracked file: move `my_transcript.txt`, `interface/` and `launching_issues/` aside first.
- `.claude/hooks/block_dangerous.py` blocks a force-push to `main` even with the user's permission. For a public-history rewrite Claude does it locally (`filter-branch`, drop `refs/original`, check the tree is identical) and hands over the `--force-with-lease=main:<old sha>` push line: the user runs it.
- Secrets never get committed. `.claude/hooks/` gate commits (main branch, syntax, sync contract, boot logic).
